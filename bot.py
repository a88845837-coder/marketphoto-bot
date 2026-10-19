import asyncio
import datetime
import json
import os
import time
import urllib.parse
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

BOT_TOKEN = "8616836203:AAGImAKF-vFvJOQ0Qo1VSQ8mZFoPJ8nkzLs"
ADMIN_ID = 5694967765

bot = Bot(token=BOT_TOKEN)
dp = Dispatcher()

DB_FILE = "users_db.json"
DAILY_FREE_LIMIT = 5


class PhotoGenerationState(StatesGroup):
    waiting_for_text = State()
    waiting_for_photo = State()


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
    uid = str(user_id)
    today = datetime.date.today().isoformat()
    if uid not in db:
        db[uid] = {"credits": 0, "stars_spent": 0, "sub_until": 0, "daily_count": 0, "last_date": today}
        save_db(db)
    elif db[uid].get("last_date") != today:
        db[uid]["daily_count"] = 0
        db[uid]["last_date"] = today
        save_db(db)
    return db[uid]


def update_user_purchase(user_id: int, credits_add: int, stars: int = 0, days: int = 0):
    db = load_db()
    uid = str(user_id)
    if uid not in db:
        get_user_data(user_id)
        db = load_db()
    db[uid]["credits"] += credits_add
    db[uid]["stars_spent"] = db[uid].get("stars_spent", 0) + stars
    if days > 0:
        base = max(time.time(), db[uid].get("sub_until", 0))
        db[uid]["sub_until"] = base + (days * 86400)
    save_db(db)
    return db[uid]


def use_generation(user_id: int):
    db = load_db()
    uid = str(user_id)
    today = datetime.date.today().isoformat()
    if uid not in db:
        get_user_data(user_id)
        db = load_db()
    if db[uid].get("last_date") != today:
        db[uid]["daily_count"] = 0
        db[uid]["last_date"] = today

    if user_id == ADMIN_ID:
        return True, "admin", db[uid]["credits"]
    if db[uid]["daily_count"] < DAILY_FREE_LIMIT:
        db[uid]["daily_count"] += 1
        save_db(db)
        return True, "free", DAILY_FREE_LIMIT - db[uid]["daily_count"]
    if db[uid]["credits"] > 0:
        db[uid]["credits"] -= 1
        save_db(db)
        return True, "credit", db[uid]["credits"]
    return False, "none", 0


def enhance_prompt(user_prompt: str, style: str) -> str:
    p = user_prompt.strip().lower()
    
    # Словник точних перекладів
    dictionary = {
        "полуниця": "fresh juicy red strawberry, macro shot, 4k",
        "полуниці": "fresh juicy red strawberries, macro shot, 4k",
        "банани": "bunch of fresh yellow bananas, realistic, 4k",
        "банан": "fresh yellow banana, realistic",
        "бетмен": "Batman superhero dark knight, cinematic lighting",
        "людина-павук": "Spider-Man superhero dynamic action pose",
        "людина павук": "Spider-Man superhero dynamic action pose",
        "рік і морті": "Rick and Morty cartoon style, 2D animation",
        "бмв": "luxury BMW car, cinematic lighting",
        "автомобіль": "modern luxury car, street background",
        "кіберпанк": "cyberpunk city, neon lights, futuristic",
        "кіт": "cute fluffy cat, detailed fur",
        "собака": "cute happy dog",
        "космос": "outer space, nebula, stars",
    }

    translated = user_prompt
    for k, v in dictionary.items():
        if k in p:
            translated = v
            break

    if style == "Стильна Аватарка":
        extra = "high quality, masterpiece, vivid colors"
    elif style == "Заставка / Шпалери":
        extra = "epic wallpaper, ultra detailed, 8k resolution, cinematic lighting"
    else:
        extra = "digital art illustration, artistic style, masterpiece"

    return f"{translated}, {extra}"


def main_keyboard():
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text="👤 Профіль"), KeyboardButton(text="🛒 Магазин зірок")],
            [KeyboardButton(text="⚙️ Послуги / Функції"), KeyboardButton(text="🎨 Створити фото")],
            [KeyboardButton(text="ℹ️ Допомога")],
        ],
        resize_keyboard=True,
    )


def generation_mode_keyboard():
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="✨ Генерація з нуля", callback_data="gen_text")],
            [InlineKeyboardButton(text="📸 Обробка фото", callback_data="gen_photo")],
        ]
    )


def photo_type_keyboard():
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="👤 Стильна Аватарка", callback_data="type_avatar")],
            [InlineKeyboardButton(text="🖼️ Заставка / Шпалери", callback_data="type_wallpaper")],
            [InlineKeyboardButton(text="✨ Художнє фото", callback_data="type_art")],
        ]
    )


def shop_keyboard():
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="💎 50 кредитів — 25 Stars", callback_data="buy_50")],
            [InlineKeyboardButton(text="💎 100 кредитів — 45 Stars", callback_data="buy_100")],
            [InlineKeyboardButton(text="⭐ VIP підписка — 100 Stars", callback_data="buy_vip")],
        ]
    )


def user_result_keyboard():
    return InlineKeyboardMarkup(
        inline_keyboard=[[InlineKeyboardButton(text="⚠️ Брак (повернути спробу)", callback_data="user_report_defect")]]
    )


@dp.message(CommandStart())
async def cmd_start(message: Message):
    get_user_data(message.from_user.id)
    await message.answer(
        f"Привіт, {message.from_user.first_name}! 👋\n\n🤖 Ласкаво просимо до **MarketPhoto AI**!\n🎁 Вам доступно **{DAILY_FREE_LIMIT} безкоштовних генерацій** щодня.",
        reply_markup=main_keyboard(),
        parse_mode="Markdown",
    )


@dp.message(Command("profile"))
@dp.message(F.text == "👤 Профіль")
async def cmd_profile(message: Message):
    uid = message.from_user.id
    ud = get_user_data(uid)
    free = "♾️ Нескінченно" if uid == ADMIN_ID else f"{max(0, DAILY_FREE_LIMIT - ud.get('daily_count', 0))} з {DAILY_FREE_LIMIT}"
    sub = "Немає"
    if ud.get("sub_until", 0) > time.time():
        sub = f"Активна (залишилось {int((ud['sub_until'] - time.time()) / 86400)} дн.)"

    await message.answer(
        f"👤 **Профіль:**\n\n🆔 ID: `{uid}`\n🎁 Безкоштовних: **{free}**\n💎 Кредитів: **{ud['credits']}**\n👑 VIP: **{sub}**",
        parse_mode="Markdown",
    )


@dp.message(F.text.in_({"⚙️ Послуги / Функції", "🎨 Створити фото", "⚙️ Послуги"}))
async def cmd_services(message: Message, state: FSMContext):
    await message.answer("🚀 Оберіть режим створення:", reply_markup=generation_mode_keyboard(), parse_mode="Markdown")


@dp.callback_query(F.data.in_(["gen_text", "gen_photo"]))
async def process_mode(callback: types.CallbackQuery, state: FSMContext):
    await state.update_data(gen_mode="text" if callback.data == "gen_text" else "photo")
    await callback.message.edit_text("🎨 Оберіть стиль:", reply_markup=photo_type_keyboard(), parse_mode="Markdown")
    await callback.answer()


@dp.callback_query(F.data.startswith("type_"))
async def process_type(callback: types.CallbackQuery, state: FSMContext):
    t_map = {"avatar": "Стильна Аватарка", "wallpaper": "Заставка / Шпалери", "art": "Художнє фото"}
    s_name = t_map.get(callback.data.replace("type_", ""), "Стандартний")
    await state.update_data(photo_type=s_name)
    data = await state.get_data()

    if data.get("gen_mode") == "text":
        await state.set_state(PhotoGenerationState.waiting_for_text)
        await callback.message.edit_text(f"🎯 Стиль: **{s_name}**\n\n✏️ Напишіть, що намалювати:", parse_mode="Markdown")
    else:
        await state.set_state(PhotoGenerationState.waiting_for_photo)
        await callback.message.edit_text(f"🎯 Стиль: **{s_name}**\n\n📸 Надішліть фото:", parse_mode="Markdown")
    await callback.answer()


@dp.message(PhotoGenerationState.waiting_for_text)
async def process_text_gen(message: Message, state: FSMContext):
    uid = message.from_user.id
    success, _, _ = use_generation(uid)
    if not success:
        await message.answer("❌ У вас закінчилися спроби та кредити!")
        await state.clear()
        return

    data = await state.get_data()
    s_name = data.get("photo_type", "Стиль")

    status = await message.answer("🚀 Генеруємо...")
    prompt = enhance_prompt(message.text, s_name)
    url = f"https://image.pollinations.ai/prompt/{urllib.parse.quote(prompt)}?width=1024&height=1024&model=flux&nologo=true"

    try:
        await status.delete()
    except Exception:
        pass

    await message.answer_photo(
        photo=url,
        caption=f"✨ **Готово!**\n🎨 Стиль: _{s_name}_\n📝 Запит: _{message.text}_",
        reply_markup=user_result_keyboard(),
        parse_mode="Markdown",
    )
    await state.clear()


@dp.message(PhotoGenerationState.waiting_for_photo, F.photo)
async def process_photo_gen(message: Message, state: FSMContext):
    success, _, _ = use_generation(message.from_user.id)
    if not success:
        await message.answer("❌ Немає спроб!")
        await state.clear()
        return
    await message.answer_photo(photo=message.photo[-1].file_id, caption="✨ Готово!", reply_markup=user_result_keyboard())
    await state.clear()


@dp.callback_query(F.data == "user_report_defect")
async def report_defect(callback: types.CallbackQuery):
    await callback.answer("📩 Скарга надіслана!", show_alert=True)
    await callback.message.edit_caption(caption=callback.message.caption + "\n\n⏳ На перевірці.", parse_mode="Markdown")


@dp.message(F.text == "🛒 Магазин зірок")
@dp.message(Command("shop"))
async def shop(message: Message):
    await message.answer("🛒 Магазин пакетів:", reply_markup=shop_keyboard(), parse_mode="Markdown")


@dp.callback_query(F.data.in_(["buy_50", "buy_100", "buy_vip"]))
async def buy(callback: types.CallbackQuery):
    p_map = {
        "buy_50": ("50 кредитів", 25, "p_50"),
        "buy_100": ("100 кредитів", 45, "p_100"),
        "buy_vip": ("VIP Підписка", 100, "p_vip"),
    }
    title, price, payload = p_map[callback.data]
    await bot.send_invoice(
        chat_id=callback.message.chat.id,
        title=title,
        description="Покупка в боті",
        payload=payload,
        provider_token="",
        currency="XTR",
        prices=[LabeledPrice(label=title, amount=price)],
        start_parameter="pay",
    )
    await callback.answer()


@dp.pre_checkout_query()
async def pre_checkout(q: PreCheckoutQuery):
    await bot.answer_pre_checkout_query(q.id, ok=True)


@dp.message(F.successful_payment)
async def success_pay(message: Message):
    payload = message.successful_payment.invoice_payload
    uid = message.from_user.id
    stars = message.successful_payment.total_amount
    if payload == "p_50":
        update_user_purchase(uid, 50, stars)
    elif payload == "p_100":
        update_user_purchase(uid, 100, stars)
    elif payload == "p_vip":
        update_user_purchase(uid, 50, stars, days=30)
    await message.answer("🎉 Успішно оплачено!")


@dp.message(F.text == "ℹ️ Допомога")
async def help_cmd(message: Message):
    await message.answer(f"ℹ️ Безкоштовно: {DAILY_FREE_LIMIT} генерації на день.", parse_mode="Markdown")


async def main():
    app = web.Application()
    app.router.add_get("/", lambda r: web.Response(text="OK"))
    runner = web.AppRunner(app)
    await runner.setup()
    await web.TCPSite(runner, "0.0.0.0", int(os.environ.get("PORT", 10000))).start()
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
