import asyncio
import json
import os
import time
from aiohttp import web
from aiogram import Bot, Dispatcher, F, types
from aiogram.filters import Command, CommandStart
from aiogram.types import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    KeyboardButton,
    LabeledPrice,
    Message,
    PreCheckoutQuery,
    ReplyKeyboardMarkup,
)

# ================= Налаштування =================
BOT_TOKEN = "8616836203:AAGImAKF-vFvJOQ0Qo1VSQ8mZFoPJ8nkzLs"
ADMIN_ID = 5694967765

bot = Bot(token=BOT_TOKEN)
dp = Dispatcher()

DB_FILE = "users_db.json"


# ================= Робота з базою даних =================
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


def get_user_data(user_id: int):
    db = load_db()
    user_id_str = str(user_id)
    if user_id_str not in db:
        db[user_id_str] = {
            "credits": 0,
            "stars_spent": 0,
            "sub_until": 0,
            "joined_at": int(time.time()),
        }
        save_db(db)
    return db[user_id_str]


def update_user_purchase(
    user_id: int, credits_to_add: int, stars_spent: int = 0, days_sub: int = 0
):
    db = load_db()
    user_id_str = str(user_id)

    if user_id_str not in db:
        db[user_id_str] = {
            "credits": 0,
            "stars_spent": 0,
            "sub_until": 0,
            "joined_at": int(time.time()),
        }

    db[user_id_str]["credits"] += credits_to_add
    db[user_id_str]["stars_spent"] = (
        db[user_id_str].get("stars_spent", 0) + stars_spent
    )

    if days_sub > 0:
        current_time = time.time()
        active_until = db[user_id_str].get("sub_until", 0)
        base_time = max(current_time, active_until)
        db[user_id_str]["sub_until"] = base_time + (days_sub * 24 * 60 * 60)

    save_db(db)
    return db[user_id_str]


# ================= Клавіатури =================
def main_keyboard():
    kb = [
        [
            KeyboardButton(text="👤 Профіль"),
            KeyboardButton(text="🛒 Магазин зірок"),
        ],
        [
            KeyboardButton(text="⚙️ Послуги / Функції"),
            KeyboardButton(text="ℹ️ Допомога"),
        ],
    ]
    return ReplyKeyboardMarkup(keyboard=kb, resize_keyboard=True)


def shop_keyboard():
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="💎 50 кредитів — 25 Stars",
                    callback_data="buy_50_credits",
                )
            ],
            [
                InlineKeyboardButton(
                    text="💎 100 кредитів — 45 Stars",
                    callback_data="buy_100_credits",
                )
            ],
            [
                InlineKeyboardButton(
                    text="⭐ VIP підписка (30 днів) — 100 Stars",
                    callback_data="buy_vip_sub",
                )
            ],
        ]
    )


# ================= Хендлери =================

@dp.message(CommandStart())
async def cmd_start(message: Message):
    get_user_data(message.from_user.id)
    text = (
        f"Привіт, {message.from_user.first_name}! 👋\n\n"
        f"Ласкаво просимо до бота! Обирай необхідні розділи в меню нижче."
    )
    await message.answer(text, reply_markup=main_keyboard())


@dp.message(Command("profile"))
@dp.message(F.text == "👤 Профіль")
async def cmd_profile(message: Message):
    user_data = get_user_data(message.from_user.id)
    credits = user_data.get("credits", 0)
    stars_spent = user_data.get("stars_spent", 0)
    sub_until = user_data.get("sub_until", 0)

    sub_status = "Немає"
    if sub_until > time.time():
        time_left = int((sub_until - time.time()) / (24 * 3600))
        sub_status = f"Активна (залишилось {time_left} дн.)"

    profile_text = (
        f"👤 **Ваш особистий профіль:**\n\n"
        f"🆔 ID: `{message.from_user.id}`\n"
        f"💎 Доступно кредитів: **{credits}**\n"
        f"⭐ Витрачено зірок (XTR): **{stars_spent}**\n"
        f"👑 VIP-Підписка: **{sub_status}**"
    )
    await message.answer(profile_text, parse_mode="Markdown")


@dp.message(Command("shop", "buy"))
@dp.message(F.text == "🛒 Магазин зірок")
async def cmd_shop(message: Message):
    await message.answer(
        "🛒 **Магазин покупок за Telegram Stars (XTR)**\n\n"
        "Оберіть потрібний пакет. Оплата проходить миттєво й абсолютно безпечно:",
        reply_markup=shop_keyboard(),
        parse_mode="Markdown",
    )


@dp.message(F.text == "⚙️ Послуги / Функції")
async def cmd_services(message: Message):
    user_data = get_user_data(message.from_user.id)
    if user_data["credits"] <= 0:
        await message.answer(
            "❌ У вас закінчилися кредити! Поповніть їх у меню «🛒 Магазин зірок»."
        )
    else:
        await message.answer(
            f"⚙️ Ви можете використати функцію бота.\nНадішліть фото для обробки. Кожна обробка коштує **1 кредит**.\n\nУ вас є: **{user_data['credits']}** кредитів.",
            parse_mode="Markdown",
        )


@dp.message(F.text == "ℹ️ Допомога")
async def cmd_help(message: Message):
    await message.answer(
        "ℹ️ **Довідка:**\n\n"
        "• Оплата здійснюється виключно за допомогою Telegram Stars.\n"
        "• Баланс поповнюється автоматично після успішної транзакції.\n"
        "• Якщо фото вийшло з помилкою або неналежної якості, адресована система автоматично поверне вам кредити після перевірки адміном."
    )


# ================= Обробка фото користувача та Модерація Адміном =================

@dp.message(F.photo)
async def process_user_photo(message: Message):
    user_id = message.from_user.id
    user_data = get_user_data(user_id)

    if user_data["credits"] <= 0:
        await message.answer("❌ Недостатньо кредитів! Поповніть їх у «🛒 Магазин зірок».")
        return

    # Списуємо 1 кредит за обробку
    update_user_purchase(user_id, credits_to_add=-1)
    
    # Беремо останнє (найкраще за якістю) фото
    photo_file_id = message.photo[-1].file_id

    # Взяття фото в роботу і надсилання результату користувачеві
    await message.answer("📸 Ваше фото успішно прийнято та опрацьовано!")
    await message.answer_photo(photo_file_id, caption="✨ Ось ваш результат!")

    # Надсилаємо сповіщення адміну для перевірки якості
    admin_kb = InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="✅ Все добре", callback_data=f"mod_ok_{user_id}"),
                InlineKeyboardButton(text="🔄 Повернути 1 кредит", callback_data=f"mod_refund_{user_id}_1")
            ]
        ]
    )

    try:
        await bot.send_photo(
            ADMIN_ID,
            photo=photo_file_id,
            caption=(
                f"📊 **Нове оброблене фото!**\n\n"
                f"👤 Користувач: @{message.from_user.username or 'немає'} (ID: `{user_id}`)\n"
                f"💎 Залишок кредитів у юзера: **{user_data['credits'] - 1}**\n\n"
                f"Оцініть якість фото нижче:"
            ),
            reply_markup=admin_kb,
            parse_mode="Markdown"
        )
    except Exception as e:
        print(f"Помилка надсилання сповіщення адміну: {e}")


# Колбек обробки дій модерації адміністратором
@dp.callback_query(F.data.startswith("mod_"))
async def process_moderation(callback: types.CallbackQuery):
    if callback.from_user.id != ADMIN_ID:
        await callback.answer("❌ У вас немає прав модератора.", show_alert=True)
        return

    data = callback.data.split("_")
    action = data[1]
    target_user_id = int(data[2])

    if action == "ok":
        await callback.message.edit_caption(
            caption=callback.message.caption + "\n\n✅ **Прийнято (без повернення).**",
            parse_mode="Markdown"
        )
        await callback.answer("Збережено!")

    elif action == "refund":
        credits_to_return = int(data[3])
        # Повертаємо кредити юзеру
        updated_data = update_user_purchase(target_user_id, credits_to_add=credits_to_return)

        await callback.message.edit_caption(
            caption=callback.message.caption + f"\n\n🔄 **Повернено {credits_to_return} кредит(ів).**",
            parse_mode="Markdown"
        )
        await callback.answer(f"Повернено {credits_to_return} кредит(ів)!")

        # Сповіщаємо користувача про повернення
        try:
            await bot.send_message(
                target_user_id,
                f"🔄 **Компенсація кредитів!**\n\n"
                f"Адміністратор перевірив останнє фото і повернув вам **{credits_to_return} кредит(ів)**.\n"
                f"💰 Ваш поточний баланс: **{updated_data['credits']}**"
            )
        except Exception:
            pass


# ================= Оплата через Telegram Stars =================

@dp.callback_query(
    F.data.in_(["buy_50_credits", "buy_100_credits", "buy_vip_sub"])
)
async def process_buy_callback(callback: types.CallbackQuery):
    if callback.data == "buy_50_credits":
        title = "Пакет 50 кредитів"
        desc = "Поповнення балансу бота на 50 кредитів."
        price = 25
        payload = "payload_50_credits"
    elif callback.data == "buy_100_credits":
        title = "Пакет 100 кредитів"
        desc = "Поповнення балансу бота на 100 кредитів."
        price = 45
        payload = "payload_100_credits"
    elif callback.data == "buy_vip_sub":
        title = "VIP Підписка на 30 днів"
        desc = "Отримання VIP-статусу + 50 бонусних кредитів."
        price = 100
        payload = "payload_vip_sub"

    prices = [LabeledPrice(label=title, amount=price)]

    await bot.send_invoice(
        chat_id=callback.message.chat.id,
        title=title,
        description=desc,
        payload=payload,
        provider_token="",
        currency="XTR",
        prices=prices,
        start_parameter="buy-stars",
    )
    await callback.answer()


@dp.pre_checkout_query()
async def process_pre_checkout(pre_checkout_query: PreCheckoutQuery):
    await bot.answer_pre_checkout_query(pre_checkout_query.id, ok=True)


@dp.message(F.successful_payment)
async def process_successful_payment(message: Message):
    payment_info = message.successful_payment
    payload = payment_info.invoice_payload
    user_id = message.from_user.id
    stars = payment_info.total_amount

    if payload == "payload_50_credits":
        user_data = update_user_purchase(
            user_id, credits_to_add=50, stars_spent=stars
        )
        msg_text = f"🎉 **Дякуємо за покупку!**\n\n➕ Зараховано: **50 кредитів**\n💰 Поточний баланс: **{user_data['credits']}**"
    elif payload == "payload_100_credits":
        user_data = update_user_purchase(
            user_id, credits_to_add=100, stars_spent=stars
        )
        msg_text = f"🎉 **Дякуємо за покупку!**\n\n➕ Зараховано: **100 кредитів**\n💰 Поточний баланс: **{user_data['credits']}**"
    elif payload == "payload_vip_sub":
        user_data = update_user_purchase(
            user_id, credits_to_add=50, stars_spent=stars, days_sub=30
        )
        msg_text = (
            "👑 **VIP Підписку активовано!**\n\n"
            "📅 Термін: **30 днів**\n"
            "➕ Додано: **50 бонусних кредитів**"
        )

    await message.answer(msg_text, parse_mode="Markdown")

    try:
        await bot.send_message(
            ADMIN_ID,
            f"🔔 **Нова оплата зірками!**\n\n"
            f"Користувач: @{message.from_user.username or 'немає'} (ID: `{user_id}`)\n"
            f"Сума: **{stars} Stars (XTR)**\n"
            f"Товар: `{payload}`",
            parse_mode="Markdown",
        )
    except Exception:
        pass


# ================= Команди Адміністратора =================

@dp.message(Command("refund"))
async def cmd_refund_manual(message: Message):
    if message.from_user.id == ADMIN_ID:
        parts = message.text.split()
        if len(parts) >= 3:
            target_user_id = int(parts[1])
            credits_to_return = int(parts[2])

            updated_data = update_user_purchase(target_user_id, credits_to_add=credits_to_return)
            await message.answer(f"✅ Успішно повернуто **{credits_to_return}** кредитів юзеру `{target_user_id}`.")

            try:
                await bot.send_message(
                    target_user_id,
                    f"🎁 **Адміністратор повернув вам {credits_to_return} кредит(ів)!**\n"
                    f"💰 Ваш поточний баланс: **{updated_data['credits']}**"
                )
            except Exception:
                pass
        else:
            await message.answer("ℹ️ Формат: `/refund [user_id] [кредити]`")
    else:
        await message.answer("❌ У вас немає прав на цю команду.")


@dp.message(Command("stop"))
async def cmd_stop(message: Message):
    if message.from_user.id == ADMIN_ID:
        await message.answer("🛑 Бот зупинений.")
        await dp.stop_polling()
    else:
        await message.answer("❌ У вас немає прав на цю команду.")


# ================= Вебсервер для Render =================
async def handle_ping(request):
    return web.Response(text="Bot is running!")


async def start_web_server():
    app = web.Application()
    app.router.add_get("/", handle_ping)
    runner = web.AppRunner(app)
    await runner.setup()
    port = int(os.environ.get("PORT", 10000))
    site = web.TCPSite(runner, "0.0.0.0", port)
    await site.start()
    print(f"🌐 Вебсервер запущено на порту {port}")


# ================= Запуск =================
async def main():
    await start_web_server()
    print("🚀 Бот успішно запущений і працює!")
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
