
import asyncio
import datetime
import json
import logging
import os
import time
import urllib.parse
from pathlib import Path

import aiohttp
from aiohttp import web
from aiogram import Bot, Dispatcher, F, types
from aiogram.filters import Command, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import (
    BufferedInputFile,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    KeyboardButton,
    LabeledPrice,
    Message,
    PreCheckoutQuery,
    ReplyKeyboardMarkup,
)

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("marketphoto")

BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
ADMIN_ID = int(os.getenv("ADMIN_ID", "5694967765"))
PORT = int(os.getenv("PORT", "10000"))
DAILY_FREE_LIMIT = 5
DB_FILE = Path("users_db.json")

if not BOT_TOKEN:
    raise RuntimeError("Add BOT_TOKEN in Render Environment.")

bot = Bot(token=BOT_TOKEN)
dp = Dispatcher()


class PhotoState(StatesGroup):
    waiting_text = State()
    waiting_photo = State()


def load_db():
    try:
        with DB_FILE.open("r", encoding="utf-8") as file:
            data = json.load(file)
            return data if isinstance(data, dict) else {}
    except FileNotFoundError:
        return {}
    except (json.JSONDecodeError, OSError):
        logger.exception("Database read error")
        return {}


def save_db(db):
    temp = DB_FILE.with_suffix(".tmp")
    with temp.open("w", encoding="utf-8") as file:
        json.dump(db, file, ensure_ascii=False, indent=2)
    temp.replace(DB_FILE)


def get_user(user_id):
    db = load_db()
    uid = str(user_id)
    today = datetime.date.today().isoformat()

    if uid not in db:
        db[uid] = {
            "credits": 0,
            "stars_spent": 0,
            "sub_until": 0,
            "daily_count": 0,
            "last_date": today,
        }

    user = db[uid]
    user.setdefault("credits", 0)
    user.setdefault("stars_spent", 0)
    user.setdefault("sub_until", 0)
    user.setdefault("daily_count", 0)

    if user.get("last_date") != today:
        user["daily_count"] = 0
        user["last_date"] = today

    save_db(db)
    return user


def add_purchase(user_id, credits=0, stars=0, days=0):
    db = load_db()
    uid = str(user_id)
    user = db.get(uid)

    if user is None:
        get_user(user_id)
        db = load_db()
        user = db[uid]

    user["credits"] = user.get("credits", 0) + credits
    user["stars_spent"] = user.get("stars_spent", 0) + stars

    if days:
        user["sub_until"] = max(
            time.time(), user.get("sub_until", 0)
        ) + days * 86400

    save_db(db)


def use_generation(user_id):
    db = load_db()
    uid = str(user_id)
    today = datetime.date.today().isoformat()

    if uid not in db:
        get_user(user_id)
        db = load_db()

    user = db[uid]

    if user.get("last_date") != today:
        user["daily_count"] = 0
        user["last_date"] = today

    if user_id == ADMIN_ID:
        save_db(db)
        return True, "admin", "∞"

    if user.get("sub_until", 0) > time.time():
        save_db(db)
        return True, "vip", "VIP"

    if user.get("daily_count", 0) < DAILY_FREE_LIMIT:
        user["daily_count"] += 1
        remaining = DAILY_FREE_LIMIT - user["daily_count"]
        save_db(db)
        return True, "free", remaining

    if user.get("credits", 0) > 0:
        user["credits"] -= 1
        save_db(db)
        return True, "credit", user["credits"]

    save_db(db)
    return False, "none", 0


def main_keyboard():
    return ReplyKeyboardMarkup(
        keyboard=[
            [
                KeyboardButton(text="👤 Профіль"),
                KeyboardButton(text="🛒 Магазин зірок"),
            ],
            [
                KeyboardButton(text="⚙️ Послуги / Функції"),
                KeyboardButton(text="🎨 Створити фото"),
            ],
            [KeyboardButton(text="ℹ️ Допомога")],
        ],
        resize_keyboard=True,
    )


def mode_keyboard():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(
            text="✨ Генерація з нуля", callback_data="gen_text"
        )],
        [InlineKeyboardButton(
            text="📸 Обробка фото", callback_data="gen_photo"
        )],
    ])


def style_keyboard():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(
            text="👤 Стильна Аватарка", callback_data="style_avatar"
        )],
        [InlineKeyboardButton(
            text="🖼️ Заставка / Шпалери", callback_data="style_wallpaper"
        )],
        [InlineKeyboardButton(
            text="✨ Художнє фото", callback_data="style_art"
        )],
    ])


def shop_keyboard():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(
            text="💎 50 кредитів — 25 Stars", callback_data="buy_50"
        )],
        [InlineKeyboardButton(
            text="💎 100 кредитів — 45 Stars", callback_data="buy_100"
        )],
        [InlineKeyboardButton(
            text="⭐ VIP на 30 днів — 100 Stars", callback_data="buy_vip"
        )],
    ])


def result_keyboard():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(
            text="⚠️ Повідомити про брак",
            callback_data="report_defect",
        )]
    ])


def make_prompt(text, style):
    text = text.strip()
    replacements = {
        "кіт": "cute fluffy cat, detailed professional photography",
        "собака": "cute happy dog, professional photography",
        "банан": "fresh yellow banana, studio photography",
        "полуниця": "fresh red strawberry, macro photography",
        "космос": "colorful nebula, deep outer space, glowing stars",
        "кіберпанк": "cyberpunk futuristic city, neon lights",
        "бмв": "luxury BMW car, cinematic night street photography",
        "бетмен": "Batman, dark cinematic superhero illustration",
        "людина-павук": "Spider-Man, dynamic superhero digital art",
        "губка боб": "SpongeBob SquarePants, detailed 3D cartoon",
    }

    lowered = text.lower()
    for key, value in replacements.items():
        if key in lowered:
            text = value
            break

    extra = {
        "Стильна Аватарка":
            "professional portrait, centered composition, sharp focus",
        "Заставка / Шпалери":
            "cinematic wallpaper, beautiful lighting, detailed background",
        "Художнє фото":
            "high-quality visual art, detailed composition",
    }.get(style, "high-quality image, detailed composition")

    return f"{text}, {extra}"


@dp.message(CommandStart())
async def start(message: Message):
    get_user(message.from_user.id)
    await message.answer(
        f"Привіт, {message.from_user.first_name}! 👋\n\n"
        "<b>MarketPhoto AI</b>\n"
        f"🎁 {DAILY_FREE_LIMIT} безкоштовних генерацій щодня.",
        reply_markup=main_keyboard(),
        parse_mode="HTML",
    )


@dp.message(Command("profile"))
@dp.message(F.text == "👤 Профіль")
async def profile(message: Message):
    uid = message.from_user.id
    user = get_user(uid)
    free = (
        "∞"
        if uid == ADMIN_ID
        else str(max(0, DAILY_FREE_LIMIT - user["daily_count"]))
    )
    vip = (
        "Активна"
        if user["sub_until"] > time.time()
        else "Немає"
    )

    await message.answer(
        f"👤 <b>Профіль</b>\n\n"
        f"🆔 ID: <code>{uid}</code>\n"
        f"🎁 Безкоштовних сьогодні: {free}\n"
        f"💎 Кредитів: {user['credits']}\n"
        f"⭐ Витрачено Stars: {user['stars_spent']}\n"
        f"👑 VIP: {vip}",
        parse_mode="HTML",
    )


@dp.message(F.text.in_({
    "⚙️ Послуги / Функції",
    "🎨 Створити фото",
    "⚙️ Послуги",
}))
async def services(message: Message, state: FSMContext):
    await state.clear()
    await message.answer("Оберіть режим:", reply_markup=mode_keyboard())


@dp.callback_query(F.data.in_({"gen_text", "gen_photo"}))
async def choose_mode(callback: types.CallbackQuery, state: FSMContext):
    await state.update_data(
        mode="text" if callback.data == "gen_text" else "photo"
    )
    await callback.message.edit_text(
        "🎨 Оберіть стиль:", reply_markup=style_keyboard()
    )
    await callback.answer()


@dp.callback_query(F.data.startswith("style_"))
async def choose_style(callback: types.CallbackQuery, state: FSMContext):
    styles = {
        "style_avatar": "Стильна Аватарка",
        "style_wallpaper": "Заставка / Шпалери",
        "style_art": "Художнє фото",
    }
    style = styles.get(callback.data, "Художнє фото")
    data = await state.get_data()
    await state.update_data(style=style)

    if data.get("mode") == "text":
        await state.set_state(PhotoState.waiting_text)
        text = f"Стиль: <b>{style}</b>\n\nНапишіть, що згенерувати."
    else:
        await state.set_state(PhotoState.waiting_photo)
        text = (
            f"Стиль: <b>{style}</b>\n\nНадішліть фото. "
            "У цій версії AI-редагування фото ще не підключено."
        )

    await callback.message.edit_text(text, parse_mode="HTML")
    await callback.answer()


@dp.message(PhotoState.waiting_text)
async def generate(message: Message, state: FSMContext):
    if not message.text:
        await message.answer("Надішліть текстовий опис зображення.")
        return

    allowed, reason, remaining = use_generation(message.from_user.id)
    if not allowed:
        await message.answer(
            "❌ Безкоштовні спроби закінчилися. "
            "Придбайте кредити в магазині або зачекайте до завтра."
        )
        await state.clear()
        return

    data = await state.get_data()
    style = data.get("style", "Художнє фото")
    status = await message.answer("🎨 Генерую зображення, зачекайте...")

    prompt = make_prompt(message.text, style)
    seed = int(time.time() * 1000) % 2147483647
    url = (
        "https://image.pollinations.ai/prompt/"
        + urllib.parse.quote(prompt, safe="")
        + f"?width=1024&height=1024&model=turbo&seed={seed}&nologo=true"
    )

    image_bytes = None
    try:
        timeout = aiohttp.ClientTimeout(total=120)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.get(url) as response:
                if response.status == 200:
                    image_bytes = await response.read()
                else:
                    logger.error("Image API HTTP status: %s", response.status)
    except (aiohttp.ClientError, asyncio.TimeoutError):
        logger.exception("Image generation failed")

    try:
        await status.delete()
    except Exception:
        pass

    if not image_bytes:
        await message.answer(
            "❌ Сервіс генерації не повернув зображення. "
            "Спробуйте ще раз пізніше."
        )
        await state.clear()
        return

    await message.answer_photo(
        BufferedInputFile(image_bytes, filename="generated.png"),
        caption=(
            f"✨ <b>Готово!</b>\n"
            f"🎨 Стиль: {style}\n"
            f"📝 Запит: {message.text[:700]}\n"
            f"🎟️ Залишок: {remaining}"
        ),
        reply_markup=result_keyboard(),
        parse_mode="HTML",
    )
    await state.clear()


@dp.message(PhotoState.waiting_photo, F.photo)
async def receive_photo(message: Message, state: FSMContext):
    await message.answer(
        "📸 Фото отримано. Для справжнього AI-редагування потрібно "
        "підключити окремий сервіс обробки зображень."
    )
    await state.clear()


@dp.message(PhotoState.waiting_photo)
async def require_photo(message: Message):
    await message.answer("Будь ласка, надішліть фото.")


@dp.callback_query(F.data == "report_defect")
async def report_defect(callback: types.CallbackQuery):
    try:
        await bot.send_message(
            ADMIN_ID,
            f"⚠️ Скарга на результат від користувача {callback.from_user.id}",
        )
        await callback.answer("Скаргу передано адміністратору.", show_alert=True)
    except Exception:
        logger.exception("Could not notify admin")
        await callback.answer("Не вдалося повідомити адміністратора.", show_alert=True)


@dp.message(F.text == "🛒 Магазин зірок")
@dp.message(Command("shop"))
async def shop(message: Message):
    await message.answer("🛒 <b>Магазин</b>", reply_markup=shop_keyboard(), parse_mode="HTML")


@dp.callback_query(F.data.in_({"buy_50", "buy_100", "buy_vip"}))
async def buy(callback: types.CallbackQuery):
    products = {
        "buy_50": ("50 кредитів", 25, "p_50"),
        "buy_100": ("100 кредитів", 45, "p_100"),
        "buy_vip": ("VIP на 30 днів", 100, "p_vip"),
    }
    title, amount, payload = products[callback.data]

    try:
        await bot.send_invoice(
            chat_id=callback.message.chat.id,
            title=title,
            description="Цифровий товар MarketPhoto AI",
            payload=payload,
            provider_token="",
            currency="XTR",
            prices=[LabeledPrice(label=title, amount=amount)],
            start_parameter="marketphoto",
        )
        await callback.answer()
    except Exception:
        logger.exception("Invoice creation failed")
        await callback.answer("Не вдалося створити рахунок.", show_alert=True)


@dp.pre_checkout_query()
async def pre_checkout(query: PreCheckoutQuery):
    await bot.answer_pre_checkout_query(query.id, ok=True)


@dp.message(F.successful_payment)
async def successful_payment(message: Message):
    payment = message.successful_payment
    payload = payment.invoice_payload
    stars = payment.total_amount

    if payload == "p_50":
        add_purchase(message.from_user.id, credits=50, stars=stars)
    elif payload == "p_100":
        add_purchase(message.from_user.id, credits=100, stars=stars)
    elif payload == "p_vip":
        add_purchase(message.from_user.id, stars=stars, days=30)
    else:
        await message.answer(
            "Оплату отримано, але товар не розпізнано. Зверніться до адміністратора."
        )
        return

    await message.answer("🎉 Оплату підтверджено! Покупку додано до профілю.")


@dp.message(F.text == "ℹ️ Допомога")
async def help_message(message: Message):
    await message.answer(
        f"<b>MarketPhoto AI</b>\n"
        f"• {DAILY_FREE_LIMIT} безкоштовних генерацій щодня\n"
        "• Додаткові кредити за Telegram Stars\n"
        "• /profile — профіль\n"
        "• /shop — магазин",
        parse_mode="HTML",
    )


async def health(request: web.Request):
    return web.Response(text="MarketPhoto AI OK")


async def main():
    app = web.Application()
    app.router.add_get("/", health)
    app.router.add_get("/health", health)

    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", PORT)
    await site.start()
    logger.info("Web health server listening on port %s", PORT)

    try:
        # Run exactly one polling process for this bot token.
        await dp.start_polling(
            bot,
            allowed_updates=dp.resolve_used_update_types(),
            close_bot_session=False,
        )
    finally:
        await runner.cleanup()
        await bot.session.close()


if __name__ == "__main__":
    asyncio.run(main())
