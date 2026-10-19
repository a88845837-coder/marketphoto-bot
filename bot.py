import asyncio
import datetime
import json
import os
import time
from aiohttp import web
from aiogram import Bot, Dispatcher, F, types
from aiogram.filters import Command, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
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
DAILY_FREE_LIMIT = 5


# ================= Стан для вибору виду фото =================
class PhotoGenerationState(StatesGroup):
    waiting_for_photo = State()


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
    today_str = datetime.date.today().isoformat()

    if user_id_str not in db:
        db[user_id_str] = {
            "credits": 0,
            "stars_spent": 0,
            "sub_until": 0,
            "joined_at": int(time.time()),
            "daily_count": 0,
            "last_date": today_str,
        }
        save_db(db)
    else:
        user_data = db[user_id_str]
        if user_data.get("last_date") != today_str:
            user_data["daily_count"] = 0
            user_data["last_date"] = today_str
            save_db(db)

    return db[user_id_str]


def update_user_purchase(
    user_id: int, credits_to_add: int, stars_spent: int = 0, days_sub: int = 0
):
    db = load_db()
    user_id_str = str(user_id)
    today_str = datetime.date.today().isoformat()

    if user_id_str not in db:
        db[user_id_str] = {
            "credits": 0,
            "stars_spent": 0,
            "sub_until": 0,
            "joined_at": int(time.time()),
            "daily_count": 0,
            "last_date": today_str,
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


def use_generation(user_id: int):
    db = load_db()
    user_id_str = str(user_id)
    today_str = datetime.date.today().isoformat()

    if user_id_str not in db:
        get_user_data(user_id)
        db = load_db()

    user_data = db[user_id_str]

    if user_data.get("last_date") != today_str:
        user_data["daily_count"] = 0
        user_data["last_date"] = today_str

    if user_id == ADMIN_ID:
        return True, "admin", user_data["credits"]

    if user_data["daily_count"] < DAILY_FREE_LIMIT:
        user_data["daily_count"] += 1
        save_db(db)
        return True, "free", DAILY_FREE_LIMIT - user_data["daily_count"]

    if user_data["credits"] > 0:
        user_data["credits"] -= 1
        save_db(db)
        return True, "credit", user_data["credits"]

    return False, "none", 0


# ================= Клавіатури =================
def main_keyboard():
    kb = [
        [
            KeyboardButton(text="👤 Профіль"),
            KeyboardButton(text="🛒 Магазин зірок"),
        ],
        [
            KeyboardButton(text="🎨 Створити фото"),
            KeyboardButton(text="⚙️ Послуги"),
        ],
        [
            KeyboardButton(text="ℹ️ Допомога"),
        ],
    ]
    return ReplyKeyboardMarkup(keyboard=kb, resize_keyboard=True)


def photo_type_keyboard():
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="👤 Стильна Аватарка", callback_data="type_avatar"
                )
            ],
            [
                InlineKeyboardButton(
                    text="🖼️ Заставка / Шпалери", callback_data="type_wallpaper"
                )
            ],
            [
                InlineKeyboardButton(
                    text="✨ Художнє фото", callback_data="type_art"
                )
            ],
        ]
    )


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


def user_result_keyboard():
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="⚠️ Брак (повернути спробу)",
                    callback_data="user_report_defect",
                )
            ]
        ]
    )


# ================= Хендлери =================

@dp.message(CommandStart())
async def cmd_start(message: Message):
    get_user_data(message.from_user.id)
    text = (
        f"Привіт, {message.from_user.first_name}! 👋\n\n"
        f"🤖 Ласкаво просимо до бота!\n"
        f"🎁 Кожен день вам доступно **{DAILY_FREE_LIMIT} безкоштовних генерацій**.\n"
        f"Обирайте стиль або тисніть «🎨 Створити фото»."
    )
    await message.answer(text, reply_markup=main_keyboard(), parse_mode="Markdown")


@dp.message(Command("profile"))
@dp.message(F.text == "👤 Профіль")
async def cmd_profile(message: Message):
    user_id = message.from_user.id
    user_data = get_user_data(user_id)
    credits = user_data.get("credits", 0)
    stars_spent = user_data.get("stars_spent", 0)
    sub_until = user_data.get("sub_until", 0)

    if user_id == ADMIN_ID:
        free_left = "♾️ Нескінченно (Адмін)"
    else:
        free_left = f"{max(0, DAILY_FREE_LIMIT - user_data.get('daily_count', 0))} з {DAILY_FREE_LIMIT}"

    sub_status = "Немає"
    if sub_until > time.time():
        time_left = int((sub_until - time.time()) / (24 * 3600))
        sub_status = f"Активна (залишилось {time_left} дн.)"

    profile_text = (
        f"👤 **Ваш особистий профіль:**\n\n"
        f"🆔 ID: `{user_id}`\n"
        f"🎁 Безкоштовних на сьогодні: **{free_left}**\n"
        f"💎 Доступно кредитів: **{credits}**\n"
        f"⭐ Витрачено зірок (XTR): **{stars_spent}**\n"
        f"👑 VIP-Підписка: **{sub_status}**"
    )
    await message.answer(profile_text, parse_mode="Markdown")


@dp.message(F.text == "🎨 Створити фото")
@dp.message(F.text == "⚙️ Послуги")
async def cmd_choose_type(message: Message):
    await message.answer(
        "📸 **Оберіть вид генерації:**\n\n"
        "1️⃣ **Стильна Аватарка** — ідеальні пропорції та чітке обличчя.\n"
        "2️⃣ **Заставка / Шпалери** — висока деталізація для робочого столу або телефону.\n"
        "3️⃣ **Художнє фото** — креативний стиль.",
        reply_markup=photo_type_keyboard(),
        parse_mode="Markdown",
    )


@dp.callback_query(F.data.startswith("type_"))
async def process_type_selection(callback: types.CallbackQuery, state: FSMContext):
    photo_type = callback.data.replace("type_", "")
    type_names = {
        "avatar": "Стильна Аватарка",
        "wallpaper": "Заставка / Шпалери",
        "art": "Художнє фото",
    }
    selected_name = type_names.get(photo_type, "Стандартне фото")
    await state.update_data(photo_type=selected_name)
    await state.set_state(PhotoGenerationState.waiting_for_photo)

    await callback.message.edit_text(
        f"🎯 Обрано стиль: **{selected_name}**\n\n"
        f"Тепер надішліть фотографію сюди в чат:",
        parse_mode="Markdown",
    )
    await callback.answer()


@dp.message(Command("shop", "buy"))
@dp.message(F.text == "🛒 Магазин зірок")
async def cmd_shop(message: Message):
    await message.answer(
        "🛒 **Магазин покупок за Telegram Stars (XTR)**\n\n"
        "Оберіть потрібний пакет. Оплата проходить миттєво й безпечно:",
        reply_markup=shop_keyboard(),
        parse_mode="Markdown",
    )


@dp.message(F.text == "ℹ️ Допомога")
async def cmd_help(message: Message):
    await message.answer(
        "ℹ️ **Довідка:**\n\n"
        f"• Кожен користувач має **{DAILY_FREE_LIMIT} безкоштовних генерацій** щодня.\n"
        "• Можна вибирати між стилями: Аватарка, Заставка, Художнє фото.\n"
        "• Якщо згенероване фото виявилось некоректним, натисніть кнопку **«⚠️ Брак»** під фото — адміністратор розгляне заявку та поверне спробу.",
        parse_mode="Markdown",
    )


# ================= Обробка фото =================

@dp.message(F.photo)
async def process_user_photo(message: Message, state: FSMContext):
    user_id = message.from_user.id
    state_data = await state.get_data()
    style_name = state_data.get("photo_type", "Загальний стиль")

    success, source_type, remainder = use_generation(user_id)

    if not success:
        await message.answer(
            "❌ У вас закінчилися як безкоштовні спроби на сьогодні, так і кредити!\n"
            "Поповніть баланс у меню «🛒 Магазин зірок» або зачекайте до завтра."
        )
        return

    if source_type == "admin":
        info_text = "👑 [Адмін-режим: безліміт]"
    elif source_type == "free":
        info_text = f"🎁 Безкоштовна спроба (залишилось на сьогодні: {remainder})"
    else:
        info_text = f"💎 Витрачено 1 кредит (залишок: {remainder})"

    photo_file_id = message.photo[-1].file_id

    await message.answer(f"📸 Ваше фото обробляється ({style_name})...\n{info_text}")
    
    await message.answer_photo(
        photo_file_id,
        caption=f"✨ **Готово!** Стиль: _{style_name}_\n\nЯкщо результатом незадоволені, натисніть кнопку нижче:",
        reply_markup=user_result_keyboard(),
        parse_mode="Markdown",
    )

    await state.clear()


# ================= Повідомлення про Брак від юзера =================

@dp.callback_query(F.data == "user_report_defect")
async def process_user_defect_report(callback: types.CallbackQuery):
    user_id = callback.from_user.id
    photo_file_id = callback.message.photo[-1].file_id if callback.message.photo else None

    await callback.answer("📩 Заявка про брак надіслана адміністратору!", show_alert=True)
    await callback.message.edit_caption(
        caption=callback.message.caption + "\n\n⏳ **Заявку про брак відправлено на перевірку адміну.**",
        parse_mode="Markdown",
    )

    admin_kb = InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="🔄 Повернути спробу / кредит",
                    callback_data=f"mod_refund_{user_id}",
                ),
                InlineKeyboardButton(
                    text="❌ Відхилити",
                    callback_data=f"mod_reject_{user_id}",
                ),
            ]
        ]
    )

    try:
        caption_text = (
            f"⚠️ **Скарги на брак від користувача!**\n\n"
            f"👤 Користувач: @{callback.from_user.username or 'немає'} (ID: `{user_id}`)\n"
            f"Перевірте фото та поверніть спробу за потреби:"
        )

        if photo_file_id:
            await bot.send_photo(
                ADMIN_ID,
                photo=photo_file_id,
                caption=caption_text,
                reply_markup=admin_kb,
                parse_mode="Markdown",
            )
        else:
            await bot.send_message(
                ADMIN_ID,
                text=caption_text,
                reply_markup=admin_kb,
                parse_mode="Markdown",
            )
    except Exception as e:
        print(f"Помилка відправки адміну: {e}")


# ================= Колбеки модератора =================

@dp.callback_query(F.data.startswith("mod_"))
async def process_moderation(callback: types.CallbackQuery):
    if callback.from_user.id != ADMIN_ID:
        await callback.answer("❌ Немає прав.", show_alert=True)
        return

    data = callback.data.split("_")
    action = data[1]
    target_user_id = int(data[2])

    if action == "reject":
        await callback.message.edit_caption(
            caption=callback.message.caption + "\n\n❌ **Скаргу відхилено.**",
            parse_mode="Markdown",
        )
        await callback.answer("Відхилено.")

    elif action == "refund":
        db = load_db()
        user_id_str = str(target_user_id)
        if user_id_str in db:
            if db[user_id_str].get("daily_count", 0) > 0:
                db[user_id_str]["daily_count"] -= 1
            else:
                db[user_id_str]["credits"] += 1
            save_db(db)

        await callback.message.edit_caption(
            caption=callback.message.caption + "\n\n✅ **Спробу/кредит повернуто користувачеві.**",
            parse_mode="Markdown",
        )
        await callback.answer("Компенсацію надано!")

        try:
            await bot.send_message(
                target_user_id,
                "🎁 **Компенсація!**\nВашу скаргу на брак розглянуто. Вам повернено 1 спробу/кредит на баланс.",
            )
        except Exception:
            pass


# ================= Оплата Telegram Stars =================

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


# ================= Адмін-команди =================

@dp.message(Command("stats"))
async def cmd_stats(message: Message):
    if message.from_user.id == ADMIN_ID:
        db = load_db()
        total_users = len(db)
        await message.answer(
            f"📊 **Статистика бота:**\n\n👥 Всього користувачів у базі: **{total_users}**",
            parse_mode="Markdown",
        )
    else:
        await message.answer("❌ У вас немає прав на цю команду.")


@dp.message(Command("addcredits"))
async def cmd_add_credits(message: Message):
    if message.from_user.id == ADMIN_ID:
        parts = message.text.split()
        if len(parts) >= 3:
            target_user_id = int(parts[1])
            credits_amount = int(parts[2])

            updated_data = update_user_purchase(
                target_user_id, credits_to_add=credits_amount
            )
            await message.answer(
                f"✅ Нараховано **{credits_amount}** кредитів юзеру `{target_user_id}`."
            )

            try:
                await bot.send_message(
                    target_user_id,
                    f"🎁 **Адміністратор нарахував вам {credits_amount} кредит(ів)!**\n"
                    f"💰 Поточний баланс: **{updated_data['credits']}**",
                )
            except Exception:
                pass
        else:
            await message.answer(
                "ℹ️ Формат: `/addcredits [user_id] [кількість]`",
                parse_mode="Markdown",
            )
    else:
        await message.answer("❌ У вас немає прав на цю команду.")


@dp.message(Command("refund"))
async def cmd_refund_manual(message: Message):
    if message.from_user.id == ADMIN_ID:
        parts = message.text.split()
        if len(parts) >= 3:
            target_user_id = int(parts[1])
            credits_to_return = int(parts[2])

            updated_data = update_user_purchase(
                target_user_id, credits_to_add=credits_to_return
            )
            await message.answer(
                f"✅ Успішно повернуто **{credits_to_return}** кредитів юзеру `{target_user_id}`."
            )

            try:
                await bot.send_message(
                    target_user_id,
                    f"🎁 **Адміністратор повернув вам {credits_to_return} кредит(ів)!**\n"
                    f"💰 Ваш поточний баланс: **{updated_data['credits']}**",
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
