import logging
import requests
from telegram import Update, ReplyKeyboardMarkup, KeyboardButton
from telegram.ext import Application, CommandHandler, MessageHandler, filters, CallbackContext

# Вставьте сюда свой Telegram API токен
TELEGRAM_API_TOKEN = '7663925781:AAEqt25BVaBKc_IdyyFgNRdom4on2WbZ-jo'

# Функция для получения цены криптовалюты
def get_crypto_price(crypto):
    url = f"https://api.coingecko.com/api/v3/simple/price?ids={crypto}&vs_currencies=usd"
    response = requests.get(url)
    data = response.json()
    if crypto in data:
        return data[crypto]['usd']
    else:
        return None

# Функция для начала работы с ботом
async def start(update: Update, context: CallbackContext) -> None:
    user = update.message.from_user
    # Создаем клавиатуру с несколькими криптовалютами
    keyboard = [
        [KeyboardButton("Bitcoin"), KeyboardButton("Ethereum")],
        [KeyboardButton("Dogecoin"), KeyboardButton("Litecoin")],
        [KeyboardButton("Ripple")]
    ]
    reply_markup = ReplyKeyboardMarkup(keyboard, one_time_keyboard=True)

    # Отправляем приветственное сообщение с кнопками
    await update.message.reply_text(
        f'Привет, {user.first_name}! Я бот для мониторинга криптовалют.\n'
        'Выберите криптовалюту для отслеживания, или используйте команду /help для получения списка доступных команд.',
        reply_markup=reply_markup
    )

# Функция для обработки запросов о криптовалюте
async def crypto(update: Update, context: CallbackContext) -> None:
    crypto_name = update.message.text.lower().strip()

    # Получаем цену криптовалюты
    price = get_crypto_price(crypto_name)
    
    if price:
        await update.message.reply_text(f'Текущая цена {crypto_name.capitalize()} составляет ${price} USD.')
    else:
        await update.message.reply_text('Извините, не удалось найти такую криптовалюту. Попробуйте снова.')

# Функция для обработки команды конвертации
async def convert(update: Update, context: CallbackContext) -> None:
    if len(context.args) != 3:
        await update.message.reply_text("Используйте команду в формате: /convert <amount> <from_currency> <to_currency>")
        return
    
    amount, from_currency, to_currency = context.args

    # Получаем цены криптовалют
    from_price = get_crypto_price(from_currency.lower())
    to_price = get_crypto_price(to_currency.lower())

    if from_price and to_price:
        # Рассчитываем конвертацию
        result = (float(amount) * from_price) / to_price
        await update.message.reply_text(f'{amount} {from_currency.capitalize()} = {result:.4f} {to_currency.capitalize()}')
    else:
        await update.message.reply_text("Не удалось получить цену для одной или обеих криптовалют. Проверьте правильность написания.")

# Функция для обработки команды Help (список доступных команд)
async def help_command(update: Update, context: CallbackContext) -> None:
    help_text = (
        "Доступные команды бота:\n\n"
        "/start - Начать работу с ботом и выбрать криптовалюту\n"
        "/help - Получить список доступных команд\n"
        "/convert <amount> <from_currency> <to_currency> - Конвертировать криптовалюту\n"
        "Пример: /convert 1 bitcoin ethereum - Конвертирует 1 Bitcoin в Ethereum\n"
    )
    await update.message.reply_text(help_text)

# Основная функция для запуска бота
def main():
    # Включаем логирование
    logging.basicConfig(format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
                        level=logging.INFO)
    logger = logging.getLogger(__name__)

    # Создаем экземпляр бота
    application = Application.builder().token(TELEGRAM_API_TOKEN).build()

    # Обработчик команды /start
    application.add_handler(CommandHandler("start", start))

    # Обработчик текстовых сообщений (для криптовалют)
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, crypto))

    # Обработчик команды /convert для конвертации
    application.add_handler(CommandHandler("convert", convert))

    # Обработчик команды /help для списка команд
    application.add_handler(CommandHandler("help", help_command))

    # Запуск бота
    application.run_polling()

if __name__ == '__main__':
    main()
