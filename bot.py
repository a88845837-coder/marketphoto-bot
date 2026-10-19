from aiogram import Bot, Dispatcher, F, types
from aiogram.filters import Command
from aiogram.types import LabeledPrice, PreCheckoutQuery, Message
import json
import os

BOT_TOKEN = "ТВІЙ_ТОКЕН_БОТА"  # Твій токен
ADMIN_ID = 5694967765        # Твій Admin ID

bot = Bot(token=BOT_TOKEN)
dp = Dispatcher()

DB_FILE = "users_db.json"

# Функції для роботи з базою даних
def load_db():
    if not os.path.exists(DB_FILE):
        return {}
    try:
        with open(DB_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except json.JSONDecodeError:
        return {}

def save_db(data):
    with open(DB_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=4)

def add_credits(user_id: int, amount: int):
    db = load_db()
    user_id_str = str(user_id)
    if user_id_str not in db:
        db[user_id_str] = {"credits": 0}
    db[user_id_str]["credits"] += amount
    save_db(db)
    return db[user_id_str]["credits"]


# Команда /buy — покупка кредитів виключно за Telegram Stars
@dp.message(Command("buy"))
async def cmd_buy(message: types.Message):
    # Наприклад: 100 кредитів коштують 50 Telegram Stars (XTR)
    prices = [LabeledPrice(label="100 Кредитів", amount=50)]

    await bot.send_invoice(
        chat_id=message.chat.id,
        title="Купівля кредитів",
        description="Отримайте 100 кредитів для використання функцій бота.",
        payload="buy_100_credits",
        provider_token="",          # Для Telegram Stars поле завжди залишається порожнім!
        currency="XTR",             # Офіційна валюта Telegram Stars
        prices=prices,
        start_parameter="buy-credits"
    )


# Етап перевірки перед оплатою (обов'язковий крок)
@dp.pre_checkout_query()
async def process_pre_checkout(pre_checkout_query: PreCheckoutQuery):
    await bot.answer_pre_checkout_query(pre_checkout_query.id, ok=True)


# Етап успішної оплати зірками — автоматичне зарахування кредитів
@dp.message(F.successful_payment)
async def process_successful_payment(message: Message):
    payment_info = message.successful_payment
    
    if payment_info.invoice_payload == "buy_100_credits":
        added_credits = 100
        new_balance = add_credits(message.from_user.id, added_credits)
        
        # Повідомлення користувачу
        await message.answer(
            f"🎉 **Оплата пройшла успішно!**\n\n"
            f"➕ Нараховано: **{added_credits} кредитів**\n"
            f"💰 Ваш новий баланс: **{new_balance} кредитів**"
        )
        
        # Повідомлення тобі як адміну про покупку
        await bot.send_message(
            ADMIN_ID, 
            f"🔔 Нова покупка у боті!\n"
            f"Користувач: @{message.from_user.username} (ID: `{message.from_user.id}`)\n"
            f"Купив: 100 кредитів за {payment_info.total_amount} Stars."
        )


if __name__ == "__main__":
    import asyncio
    asyncio.run(dp.start_polling(bot))
