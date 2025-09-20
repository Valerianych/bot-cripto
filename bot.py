import asyncio
import json
import logging
import os
import re
import shlex
import sqlite3
import uuid
from dataclasses import dataclass
from datetime import date, datetime
from io import BytesIO
from typing import Dict, Iterable, List, Optional, Tuple
from urllib.parse import parse_qs, urlparse

from telegram import BotCommand, KeyboardButton, ReplyKeyboardMarkup, Update
from telegram.ext import (Application, CommandHandler, ContextTypes,
                          MessageHandler, filters)


logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)


DATE_INPUT_FORMATS = [
    "%Y-%m-%d",
    "%d.%m.%Y",
    "%d/%m/%Y",
    "%Y/%m/%d",
]
REMINDER_DAYS_BEFORE = int(os.getenv("EXPIRY_REMINDER_DAYS", "2"))
CHECK_INTERVAL_SECONDS = int(os.getenv("EXPIRY_CHECK_INTERVAL", "3600"))
DATABASE_PATH = os.getenv("PRODUCT_DB_PATH", "products.db")


@dataclass
class ProductPayload:
    """Normalized payload extracted from a QR code or manual input."""

    name: str
    expiry_date: date
    product_id: str
    raw_payload: str


def _first_value(keys: Iterable[str], values: Dict[str, str]) -> Optional[str]:
    for key in keys:
        if key in values and values[key]:
            return values[key]
    return None


def _normalize_dict_keys(source: Dict[str, str]) -> Dict[str, str]:
    return {str(k).strip().lower(): str(v).strip() for k, v in source.items()}


def parse_expiry_date(raw_value: str) -> date:
    """Parse different date string formats into a :class:`datetime.date`."""

    value = raw_value.strip()
    if "T" in value:
        value = value.split("T", 1)[0]
    if re.fullmatch(r"\d{8}", value):
        return date(int(value[0:4]), int(value[4:6]), int(value[6:8]))

    for fmt in DATE_INPUT_FORMATS:
        try:
            return datetime.strptime(value, fmt).date()
        except ValueError:
            continue

    raise ValueError(f"Не удалось распознать дату истечения: {raw_value}")


def parse_qr_payload(payload: str) -> ProductPayload:
    """Attempt to parse QR payload into a :class:`ProductPayload`."""

    if not payload or not payload.strip():
        raise ValueError("Полученные данные пустые")

    raw_text = payload.strip()

    # Strategy 1: JSON object
    try:
        data = json.loads(raw_text)
    except json.JSONDecodeError:
        data = None

    if isinstance(data, dict):
        normalized = _normalize_dict_keys(data)
        name = _first_value(["name", "product", "title"], normalized)
        expiry_raw = _first_value(
            ["expiry", "expires", "expiration", "expiration_date", "best_before"],
            normalized,
        )
        product_id = _first_value(["product_id", "id", "sku", "code"], normalized)
        if not name:
            raise ValueError("В QR-коде отсутствует название товара")
        if not expiry_raw:
            raise ValueError("В QR-коде отсутствует дата истечения срока годности")
        expiry_date = parse_expiry_date(expiry_raw)
        if not product_id:
            product_id = normalized.get("ean") or normalized.get("barcode")
        if not product_id:
            product_id = uuid.uuid4().hex
        return ProductPayload(name=name, expiry_date=expiry_date, product_id=product_id, raw_payload=raw_text)

    # Strategy 2: key=value pairs separated by delimiters
    def _parse_pairs(text: str) -> Dict[str, str]:
        result: Dict[str, str] = {}
        for fragment in re.split(r"[;,\n]\s*", text):
            if "=" in fragment:
                key, value = fragment.split("=", 1)
                if key and value:
                    result[key.strip().lower()] = value.strip()
        return result

    kv_pairs = _parse_pairs(raw_text)
    if kv_pairs:
        name = _first_value(["name", "product", "title"], kv_pairs)
        expiry_raw = _first_value(
            ["expiry", "expires", "expiration", "expiration_date", "best_before"],
            kv_pairs,
        )
        product_id = _first_value(["product_id", "id", "sku", "code"], kv_pairs)
        if name and expiry_raw:
            expiry_date = parse_expiry_date(expiry_raw)
            if not product_id:
                product_id = kv_pairs.get("ean") or kv_pairs.get("barcode") or uuid.uuid4().hex
            return ProductPayload(name=name, expiry_date=expiry_date, product_id=product_id, raw_payload=raw_text)

    # Strategy 3: URL with query parameters
    parsed_url = urlparse(raw_text)
    if parsed_url.scheme in {"http", "https"}:
        params = {k.lower(): v[0] for k, v in parse_qs(parsed_url.query).items() if v}
        name = _first_value(["name", "product", "title"], params)
        expiry_raw = _first_value(
            ["expiry", "expires", "expiration", "expiration_date", "best_before"],
            params,
        )
        product_id = _first_value(["product_id", "id", "sku", "code"], params)
        if not product_id and parsed_url.path:
            product_id = parsed_url.path.strip("/").split("/")[-1]
        if name and expiry_raw:
            expiry_date = parse_expiry_date(expiry_raw)
            if not product_id:
                product_id = uuid.uuid4().hex
            return ProductPayload(name=name, expiry_date=expiry_date, product_id=product_id, raw_payload=raw_text)

    # Strategy 4: simple pipe or tab separated values containing a date
    parts = [segment.strip() for segment in re.split(r"[|\t]", raw_text) if segment.strip()]
    if len(parts) >= 2:
        # look for a parsable date among the segments
        expiry_date = None
        expiry_index = None
        for idx, segment in enumerate(parts):
            try:
                expiry_date = parse_expiry_date(segment)
                expiry_index = idx
                break
            except ValueError:
                continue
        if expiry_date is not None and expiry_index is not None:
            name_candidates = parts[:expiry_index] + parts[expiry_index + 1 :]
            name = name_candidates[0] if name_candidates else ""
            if not name:
                raise ValueError("Не удалось определить название товара")
            product_id = uuid.uuid4().hex
            return ProductPayload(name=name, expiry_date=expiry_date, product_id=product_id, raw_payload=raw_text)

    raise ValueError(
        "Не удалось распознать данные QR-кода. Убедитесь, что в коде есть название товара и дата истечения срока годности."
    )


class ProductRepository:
    """Simple SQLite-backed storage for user products."""

    def __init__(self, path: str) -> None:
        self._path = path
        self._ensure_schema()

    def _get_connection(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self._path)
        conn.row_factory = sqlite3.Row
        return conn

    def _ensure_schema(self) -> None:
        with self._get_connection() as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS products (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id INTEGER NOT NULL,
                    chat_id INTEGER NOT NULL,
                    product_id TEXT NOT NULL,
                    name TEXT NOT NULL,
                    expiry_date TEXT NOT NULL,
                    raw_payload TEXT NOT NULL,
                    reminder_sent INTEGER NOT NULL DEFAULT 0,
                    expired_notified INTEGER NOT NULL DEFAULT 0,
                    added_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                )
                """
            )
            conn.execute(
                """
                CREATE UNIQUE INDEX IF NOT EXISTS idx_products_user_product
                ON products(user_id, product_id)
                """
            )
            conn.commit()

    def add_or_update_product(self, user_id: int, chat_id: int, payload: ProductPayload) -> Tuple[Dict[str, str], bool]:
        now_iso = datetime.utcnow().isoformat()
        with self._get_connection() as conn:
            cur = conn.cursor()
            cur.execute(
                "SELECT id FROM products WHERE user_id = ? AND product_id = ?",
                (user_id, payload.product_id),
            )
            row = cur.fetchone()
            if row:
                product_id = row["id"]
                cur.execute(
                    """
                    UPDATE products
                    SET name = ?, expiry_date = ?, raw_payload = ?, reminder_sent = 0,
                        expired_notified = 0, updated_at = ?, chat_id = ?
                    WHERE id = ?
                    """,
                    (
                        payload.name,
                        payload.expiry_date.isoformat(),
                        payload.raw_payload,
                        now_iso,
                        chat_id,
                        product_id,
                    ),
                )
                conn.commit()
                updated = True
                target_id = product_id
            else:
                cur.execute(
                    """
                    INSERT INTO products (user_id, chat_id, product_id, name, expiry_date, raw_payload,
                                           reminder_sent, expired_notified, added_at, updated_at)
                    VALUES (?, ?, ?, ?, ?, ?, 0, 0, ?, ?)
                    """,
                    (
                        user_id,
                        chat_id,
                        payload.product_id,
                        payload.name,
                        payload.expiry_date.isoformat(),
                        payload.raw_payload,
                        now_iso,
                        now_iso,
                    ),
                )
                conn.commit()
                updated = False
                target_id = cur.lastrowid

            cur.execute(
                "SELECT * FROM products WHERE id = ?",
                (target_id,),
            )
            product_row = cur.fetchone()

        if not product_row:
            raise RuntimeError("Не удалось сохранить данные продукта")

        return dict(product_row), updated

    def list_products(self, user_id: int) -> List[Dict[str, str]]:
        with self._get_connection() as conn:
            cur = conn.execute(
                "SELECT * FROM products WHERE user_id = ? ORDER BY expiry_date ASC, name ASC",
                (user_id,),
            )
            rows = cur.fetchall()
        return [dict(row) for row in rows]

    def remove_product(self, user_id: int, identifier: str) -> bool:
        with self._get_connection() as conn:
            if identifier.isdigit():
                cur = conn.execute(
                    "DELETE FROM products WHERE user_id = ? AND id = ?",
                    (user_id, int(identifier)),
                )
                if cur.rowcount:
                    conn.commit()
                    return True
            cur = conn.execute(
                "DELETE FROM products WHERE user_id = ? AND product_id = ?",
                (user_id, identifier),
            )
            removed = cur.rowcount > 0
            if removed:
                conn.commit()
            return removed

    def clear_products(self, user_id: int) -> int:
        with self._get_connection() as conn:
            cur = conn.execute(
                "DELETE FROM products WHERE user_id = ?",
                (user_id,),
            )
            deleted = cur.rowcount
            conn.commit()
        return deleted

    def fetch_all_for_notifications(self) -> List[Dict[str, str]]:
        with self._get_connection() as conn:
            cur = conn.execute("SELECT * FROM products")
            rows = cur.fetchall()
        return [dict(row) for row in rows]

    def mark_reminder_sent(self, product_id: int) -> None:
        with self._get_connection() as conn:
            conn.execute(
                "UPDATE products SET reminder_sent = 1, updated_at = ? WHERE id = ?",
                (datetime.utcnow().isoformat(), product_id),
            )
            conn.commit()

    def mark_expired_notified(self, product_id: int) -> None:
        with self._get_connection() as conn:
            conn.execute(
                "UPDATE products SET expired_notified = 1, updated_at = ? WHERE id = ?",
                (datetime.utcnow().isoformat(), product_id),
            )
            conn.commit()


def format_days_left(expiry: date) -> str:
    today = date.today()
    delta = (expiry - today).days
    if delta > 1:
        return f"осталось {delta} дней"
    if delta == 1:
        return "остался 1 день"
    if delta == 0:
        return "истекает сегодня"
    if delta == -1:
        return "просрочено на 1 день"
    return f"просрочено на {abs(delta)} дней"


def format_product_line(position: int, record: Dict[str, str]) -> str:
    expiry = date.fromisoformat(record["expiry_date"])
    status = format_days_left(expiry)
    return (
        f"{position}. {record['name']} (ID: {record['product_id']}, до {expiry.strftime('%d.%m.%Y')})\n"
        f"   └─ {status}"
    )


async def send_product_summary(update: Update, entry: Dict[str, str], updated: bool) -> None:
    expiry = date.fromisoformat(entry["expiry_date"])
    status = format_days_left(expiry)
    action = "обновлен" if updated else "добавлен"
    await update.message.reply_text(
        "\n".join(
            [
                f"Товар {action}: {entry['name']}",
                f"Срок годности до {expiry.strftime('%d.%m.%Y')} ({status})",
                f"Идентификатор товара: {entry['product_id']}",
                "Используйте /list для просмотра всех товаров или /remove <ID> для удаления.",
            ]
        )
    )


async def process_payload_text(update: Update, context: ContextTypes.DEFAULT_TYPE, payload_text: str) -> None:
    try:
        payload = parse_qr_payload(payload_text)
    except ValueError as exc:
        await update.message.reply_text(str(exc))
        return

    repo: ProductRepository = context.application.bot_data["repo"]
    user = update.effective_user
    chat = update.effective_chat
    entry, updated = await asyncio.to_thread(
        repo.add_or_update_product,
        user.id,
        chat.id,
        payload,
    )
    await send_product_summary(update, entry, updated)


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    keyboard = ReplyKeyboardMarkup(
        [[KeyboardButton("📷 Отсканировать QR"), KeyboardButton("📝 Добавить вручную")]],
        resize_keyboard=True,
    )
    greeting_lines = [
        "Привет! Я помогу следить за сроком годности продуктов.",
        "\n",
        "📸 Отсканируйте QR-код на упаковке через встроенный сканер Telegram и просто отправьте полученный текст.",
        "🖼️ Можно отправить фотографию QR-кода — бот попробует прочитать его автоматически (потребуются библиотеки pillow и pyzbar).",
        "📝 Для ручного добавления используйте команду /manual \"Название\" YYYY-MM-DD.",
        "ℹ️ Команда /help расскажет обо всех возможностях бота, включая добавление в раздел приложений Telegram.",
    ]
    if update.message:
        await update.message.reply_text("\n".join(greeting_lines), reply_markup=keyboard)


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    help_text = (
        "Доступные команды:\n\n"
        "/start — показать приветственное сообщение и клавиатуру.\n"
        "/help — вывести эту справку.\n"
        "/manual \"Название\" YYYY-MM-DD — добавить товар вручную (дату можно указать в формате ДД.ММ.ГГГГ).\n"
        "/list — показать список сохранённых товаров.\n"
        "/remove <ID> — удалить товар по идентификатору или номеру записи.\n"
        "/clear — удалить все товары.\n\n"
        "Как сканировать QR-код:\n"
        "1. Откройте кнопку 📷 Отсканировать QR в нижней клавиатуре или используйте сканер в меню вложений.\n"
        "2. Наведите камеру на QR-код — Telegram автоматически отправит расшифрованный текст боту.\n"
        "3. Бот сохранит товар и напомнит о сроке годности.\n\n"
        "Чтобы закрепить бота как приложение Telegram, откройте @BotFather → Bot Settings → Menu Button и укажите ссылку на веб-приложение,"
        " либо добавьте бота в раздел Приложения через настройки чата. Бот уже умеет работать с web_app-кнопками, если вы свяжете его с мини-приложением."
    )
    if update.message:
        await update.message.reply_text(help_text)


async def manual_add(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not context.args:
        await update.message.reply_text(
            "Использование: /manual \"Название продукта\" YYYY-MM-DD\nНапример: /manual \"Молоко\" 2024-05-01"
        )
        return

    try:
        parts = shlex.split(" ".join(context.args))
    except ValueError as exc:
        await update.message.reply_text(f"Не удалось прочитать аргументы: {exc}")
        return

    if len(parts) < 2:
        await update.message.reply_text(
            "Нужно передать название и дату. Пример: /manual \"Масло сливочное\" 01.05.2024"
        )
        return

    expiry_raw = parts[-1]
    name = " ".join(parts[:-1]).strip()
    if not name:
        await update.message.reply_text("Укажите название товара перед датой")
        return

    try:
        expiry_date = parse_expiry_date(expiry_raw)
    except ValueError as exc:
        await update.message.reply_text(str(exc))
        return

    payload = ProductPayload(
        name=name,
        expiry_date=expiry_date,
        product_id=uuid.uuid4().hex,
        raw_payload=f"manual:{name}:{expiry_date.isoformat()}",
    )
    repo: ProductRepository = context.application.bot_data["repo"]
    user = update.effective_user
    chat = update.effective_chat
    entry, updated = await asyncio.to_thread(
        repo.add_or_update_product,
        user.id,
        chat.id,
        payload,
    )
    await send_product_summary(update, entry, updated)


async def list_products(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    repo: ProductRepository = context.application.bot_data["repo"]
    user = update.effective_user
    records = await asyncio.to_thread(repo.list_products, user.id)
    if not records:
        await update.message.reply_text("Список пуст. Отсканируйте QR-код или добавьте товар командой /manual.")
        return

    lines = ["Ваши товары:"]
    for idx, record in enumerate(records, start=1):
        lines.append(format_product_line(idx, record))
    await update.message.reply_text("\n".join(lines))


async def remove_product(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not context.args:
        await update.message.reply_text("Использование: /remove <ID или номер из списка>")
        return

    identifier = context.args[0].strip()
    repo: ProductRepository = context.application.bot_data["repo"]
    user = update.effective_user
    removed = await asyncio.to_thread(repo.remove_product, user.id, identifier)
    if removed:
        await update.message.reply_text("Товар удалён.")
    else:
        await update.message.reply_text("Товар с таким идентификатором не найден.")


async def clear_products(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    repo: ProductRepository = context.application.bot_data["repo"]
    user = update.effective_user
    deleted = await asyncio.to_thread(repo.clear_products, user.id)
    await update.message.reply_text(f"Удалено записей: {deleted}")


async def handle_text_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.message:
        return

    text = update.message.text.strip()
    if text == "📷 Отсканировать QR":
        await update.message.reply_text(
            "Нажмите на скрепку → QR-код в Telegram и наведите камеру на упаковку. Полученный текст отправьте боту."
        )
        return
    if text == "📝 Добавить вручную":
        await update.message.reply_text(
            "Используйте команду /manual \"Название\" YYYY-MM-DD для добавления товара без QR-кода."
        )
        return

    await process_payload_text(update, context, text)


async def handle_photo(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.message or not update.message.photo:
        return

    try:
        from PIL import Image
        from pyzbar.pyzbar import decode
    except ImportError:
        await update.message.reply_text(
            "Чтобы распознавать QR-коды с фотографий, установите зависимости: pip install pillow pyzbar"
        )
        return

    photo = update.message.photo[-1]
    file = await photo.get_file()
    buffer = BytesIO()
    await file.download_to_memory(out=buffer)
    buffer.seek(0)

    try:
        image = Image.open(buffer)
    except Exception as exc:  # pylint: disable=broad-except
        logger.exception("Failed to open image for QR decoding: %s", exc)
        await update.message.reply_text("Не удалось обработать изображение. Попробуйте ещё раз или отправьте текст из QR-кода.")
        return

    decoded_objects = decode(image)
    if not decoded_objects:
        await update.message.reply_text(
            "Не удалось найти QR-код на изображении. Убедитесь, что код хорошо освещён и занимает большую часть кадра."
        )
        return

    for obj in decoded_objects:
        try:
            data = obj.data.decode("utf-8")
        except UnicodeDecodeError:
            continue
        if data:
            await process_payload_text(update, context, data)
            return

    await update.message.reply_text("QR-код не содержит текстовых данных, которые можно обработать.")


async def check_expirations(context: ContextTypes.DEFAULT_TYPE) -> None:
    repo: ProductRepository = context.application.bot_data["repo"]
    records = await asyncio.to_thread(repo.fetch_all_for_notifications)
    today = date.today()

    for record in records:
        expiry = date.fromisoformat(record["expiry_date"])
        days_left = (expiry - today).days
        chat_id = record["chat_id"]
        entry_id = record["id"]

        if days_left < 0 and not record["expired_notified"]:
            message = (
                f"⚠️ {record['name']} просрочен на {abs(days_left)} дн." if days_left < -1 else f"⚠️ {record['name']} просрочен."
            )
            await context.bot.send_message(chat_id=chat_id, text=message)
            await asyncio.to_thread(repo.mark_expired_notified, entry_id)
        elif 0 <= days_left <= REMINDER_DAYS_BEFORE and not record["reminder_sent"]:
            if days_left == 0:
                message = f"⏳ {record['name']} истекает сегодня!"
            elif days_left == 1:
                message = f"⏳ {record['name']} истекает завтра."
            else:
                message = f"⏳ {record['name']} истекает через {days_left} дней."
            await context.bot.send_message(chat_id=chat_id, text=message)
            await asyncio.to_thread(repo.mark_reminder_sent, entry_id)


async def post_init(application: Application) -> None:
    commands = [
        BotCommand("start", "Начать работу"),
        BotCommand("help", "Показать помощь"),
        BotCommand("manual", "Добавить товар вручную"),
        BotCommand("list", "Показать список товаров"),
        BotCommand("remove", "Удалить товар"),
        BotCommand("clear", "Очистить список"),
    ]
    await application.bot.set_my_commands(commands)


def main() -> None:
    token = os.getenv("TELEGRAM_API_TOKEN")
    if not token:
        raise RuntimeError(
            "Не найден токен Telegram. Установите переменную окружения TELEGRAM_API_TOKEN перед запуском."
        )

    repo = ProductRepository(DATABASE_PATH)

    application = Application.builder().token(token).post_init(post_init).build()
    application.bot_data["repo"] = repo

    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("help", help_command))
    application.add_handler(CommandHandler("manual", manual_add))
    application.add_handler(CommandHandler("list", list_products))
    application.add_handler(CommandHandler("remove", remove_product))
    application.add_handler(CommandHandler("clear", clear_products))
    application.add_handler(MessageHandler(filters.PHOTO, handle_photo))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text_message))

    application.job_queue.run_repeating(check_expirations, interval=CHECK_INTERVAL_SECONDS, first=60)

    application.run_polling()


if __name__ == "__main__":
    main()
