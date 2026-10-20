
import os
import asyncio
import logging
import sqlite3
from io import BytesIO
from datetime import datetime
from zoneinfo import ZoneInfo

from aiohttp import web
from aiogram import Bot, Dispatcher, F
from aiogram.filters import CommandStart, Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import StatesGroup, State
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import (
    Message,
    CallbackQuery,
    PreCheckoutQuery,
    BufferedInputFile,
    InlineKeyboardMarkup,
    InlineKeyboardButton,
    LabeledPrice,
)
from google import genai
from PIL import Image


# ============================================================
# SETTINGS
# Render -> Environment
# ============================================================

BOT_TOKEN = (
    os.getenv("TELEGRAM_TOKEN")
    or os.getenv("BOT_TOKEN")
    or ""
).strip()

GOOGLE_API_KEY = (
    os.getenv("GOOGLE_API_KEY")
    or os.getenv("GEMINI_API_KEY")
    or ""
).strip()

ADMIN_ID = int(os.getenv("ADMIN_ID", "5694967765"))
PORT = int(os.getenv("PORT", "10000"))
DB_PATH = os.getenv("DB_PATH", "marketphoto.db")
TIMEZONE = os.getenv("APP_TIMEZONE", "Europe/Kyiv")

FREE_PER_DAY = 5
PACK_CREDITS = 10
PACK_PRICE_STARS = 25

# Модель генерации та редагування зображень
IMAGE_MODEL = os.getenv(
    "GOOGLE_IMAGE_MODEL",
    "gemini-nano-banana-2.1",
)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s:%(name)s:%(message)s",
)
logger = logging.getLogger("marketphoto")

if not BOT_TOKEN:
    raise RuntimeError(
        "BOT_TOKEN або TELEGRAM_TOKEN не заданий у Render Environment"
    )

if not GOOGLE_API_KEY:
    raise RuntimeError(
        "GOOGLE_API_KEY не заданий у Render Environment"
    )

# Звичайний Gemini Developer API за API-ключем Google AI Studio.
# Тут навмисно не використовується Vertex AI.
client = genai.Client(api_key=GOOGLE_API_KEY)

bot = Bot(token=BOT_TOKEN)
dp = Dispatcher(storage=MemoryStorage())


# ============================================================
# DATABASE
# ============================================================

def today_string():
    try:
        return datetime.now(ZoneInfo(TIMEZONE)).date().isoformat()
    except Exception:
        return datetime.now().date().isoformat()


def db_connect():
    return sqlite3.connect(DB_PATH, timeout=30)


def init_db():
    with db_connect() as con:
        con.execute("""
            CREATE TABLE IF NOT EXISTS users (
                user_id INTEGER PRIMARY KEY,
                day TEXT NOT NULL,
                used INTEGER NOT NULL DEFAULT 0,
                credits INTEGER NOT NULL DEFAULT 0
            )
        """)

        con.execute("""
            CREATE TABLE IF NOT EXISTS payments (
                charge_id TEXT PRIMARY KEY,
                user_id INTEGER NOT NULL,
                amount INTEGER NOT NULL,
                currency TEXT NOT NULL
            )
        """)


def get_user(user_id):
    today = today_string()

    with db_connect() as con:
        con.execute(
            """
            INSERT OR IGNORE INTO users
            (user_id, day, used, credits)
            VALUES (?, ?, 0, 0)
            """,
            (user_id, today),
        )

        row = con.execute(
            """
            SELECT day, used, credits
            FROM users
            WHERE user_id = ?
            """,
            (user_id,),
        ).fetchone()

        saved_day, used, credits = row

        if saved_day != today:
            used = 0
            con.execute(
                """
                UPDATE users
                SET day = ?, used = 0
                WHERE user_id = ?
                """,
                (today, user_id),
            )

        return {
            "used": used,
            "credits": credits,
        }


def can_generate(user_id):
    user = get_user(user_id)
    return (
        user["used"] < FREE_PER_DAY
        or user["credits"] > 0
    )


def charge_generation(user_id):
    """Списує одну генерацію після успішного створення зображення."""
    today = today_string()

    with db_connect() as con:
        con.execute(
            """
            INSERT OR IGNORE INTO users
            (user_id, day, used, credits)
            VALUES (?, ?, 0, 0)
            """,
            (user_id, today),
        )

        saved_day, used, credits = con.execute(
            """
            SELECT day, used, credits
            FROM users
            WHERE user_id = ?
            """,
            (user_id,),
        ).fetchone()

        if saved_day != today:
            used = 0

        if used < FREE_PER_DAY:
            used += 1
        elif credits > 0:
            credits -= 1
        else:
            return False

        con.execute(
            """
            UPDATE users
            SET day = ?, used = ?, credits = ?
            WHERE user_id = ?
            """,
            (today, used, credits, user_id),
        )

    return True


def record_payment_once(charge_id, user_id, amount, currency):
    """Не зараховує один і той самий платіж двічі."""
    today = today_string()

    with db_connect() as con:
        cursor = con.execute(
            """
            INSERT OR IGNORE INTO payments
            (charge_id, user_id, amount, currency)
            VALUES (?, ?, ?, ?)
            """,
            (charge_id, user_id, amount, currency),
        )

        if cursor.rowcount == 0:
            return False

        con.execute(
            """
            INSERT OR IGNORE INTO users
            (user_id, day, used, credits)
            VALUES (?, ?, 0, 0)
            """,
            (user_id, today),
        )

        con.execute(
            """
            UPDATE users
            SET credits = credits + ?
            WHERE user_id = ?
            """,
            (PACK_CREDITS, user_id),
        )

    return True


# ============================================================
# KEYBOARDS
# ============================================================

def main_keyboard():
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="🎨 Створити фото",
                    callback_data="generate",
                )
            ],
            [
                InlineKeyboardButton(
                    text="🪄 Редагувати фото",
                    callback_data="edit",
                )
            ],
            [
                InlineKeyboardButton(
                    text="👤 Профіль",
                    callback_data="profile",
                ),
                InlineKeyboardButton(
                    text="⭐ Купити генерації",
                    callback_data="shop",
                ),
            ],
        ]
    )


def back_keyboard():
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="⬅️ Головне меню",
                    callback_data="home",
                )
            ]
        ]
    )


# ============================================================
# STATES
# ============================================================

class PhotoStates(StatesGroup):
    waiting_prompt = State()
    waiting_photo = State()
    waiting_edit_instruction = State()


# ============================================================
# GOOGLE IMAGE GENERATION
# Uses Gemini Developer API: models.generate_content()
# ============================================================

def extract_image(response):
    """
    Шукає зображення у відповіді Gemini та повертає PNG-байти.
    Якщо API повернув тільки текст, записує його у лог.
    """
    for part in response.parts or []:
        if getattr(part, "inline_data", None):
            image = part.as_image()

            output = BytesIO()
            image.save(output, format="PNG")

            return output.getvalue()

    response_text = getattr(response, "text", None)
    logger.error(
        "Gemini response contains no image. Response text: %s",
        response_text,
    )

    raise RuntimeError(
        "Gemini API не повернув зображення. Перевір Render Logs."
    )


def generate_image_sync(prompt):
    """Створює нове реалістичне фото з тексту."""
    enhanced_prompt = (
        "Create a highly photorealistic professional photograph. "
        "Natural skin texture, realistic human anatomy, detailed eyes, "
        "natural hands, realistic lighting and shadows, natural colors, "
        "realistic materials, professional camera photography, sharp focus. "
        "Avoid cartoon style, illustration, artificial plastic skin, "
        "excessive saturation and distorted details. Scene: "
        + prompt
    )

    response = client.models.generate_content(
        model=IMAGE_MODEL,
        contents=[enhanced_prompt],
    )

    return extract_image(response)


def edit_image_sync(photo_bytes, instruction):
    """Редагує фото, надіслане користувачем."""
    source = Image.open(
        BytesIO(photo_bytes)
    ).convert("RGB")

    prompt = (
        "Edit the provided photograph according to this instruction: "
        + instruction
        + ". Keep the result photorealistic, with realistic textures, "
        "natural colors, lighting and shadows. Preserve the person's "
        "identity and facial features as much as possible. "
        "Do not change unrelated details."
    )

    response = client.models.generate_content(
        model=IMAGE_MODEL,
        contents=[prompt, source],
    )

    return extract_image(response)


# ============================================================
# START AND GENERAL COMMANDS
# ============================================================

@dp.message(CommandStart())
async def start_handler(
    message: Message,
    state: FSMContext,
):
    await state.clear()

    user = get_user(message.from_user.id)
    remaining = max(0, FREE_PER_DAY - user["used"])

    await message.answer(
        "📸 <b>MarketPhoto AI</b>\n\n"
        "Створюй реалістичні фото з тексту або редагуй власні.\n\n"
        f"🎁 Безкоштовно: {FREE_PER_DAY} генерацій на день.\n"
        f"Залишилося сьогодні: {remaining}\n\n"
        "Обери потрібну дію в меню.",
        reply_markup=main_keyboard(),
        parse_mode="HTML",
    )


@dp.message(Command("help"))
async def help_handler(message: Message):
    await message.answer(
        "🎨 <b>Створення фото</b>\n"
        "Натисни «Створити фото» та напиши опис.\n\n"
        "🪄 <b>Редагування фото</b>\n"
        "Натисни «Редагувати фото», надішли фотографію "
        "та напиши, що потрібно змінити.\n\n"
        "Для скасування поточної дії введи /cancel.",
        parse_mode="HTML",
        reply_markup=main_keyboard(),
    )


@dp.message(Command("cancel"))
async def cancel_handler(
    message: Message,
    state: FSMContext,
):
    await state.clear()
    await message.answer(
        "Дію скасовано.",
        reply_markup=main_keyboard(),
    )


@dp.message(Command("id"))
async def id_handler(message: Message):
    await message.answer(
        f"Твій Telegram ID: {message.from_user.id}"
    )


@dp.message(Command("admin"))
async def admin_handler(message: Message):
    if message.from_user.id != ADMIN_ID:
        await message.answer("Немає доступу.")
        return

    await message.answer(
        "🛠 <b>MarketPhoto AI</b>\n\n"
        "Бот отримав команду адміністратора.\n"
        f"Модель: {IMAGE_MODEL}\n"
        f"Google API Key задано: {bool(GOOGLE_API_KEY)}\n"
        f"Telegram Token задано: {bool(BOT_TOKEN)}",
        parse_mode="HTML",
    )


# ============================================================
# MAIN MENU CALLBACKS
# ============================================================

@dp.callback_query(F.data == "home")
async def home_handler(
    callback: CallbackQuery,
    state: FSMContext,
):
    await state.clear()
    await callback.message.answer(
        "Головне меню MarketPhoto AI:",
        reply_markup=main_keyboard(),
    )
    await callback.answer()


@dp.callback_query(F.data == "profile")
async def profile_handler(callback: CallbackQuery):
    user = get_user(callback.from_user.id)
    remaining = max(0, FREE_PER_DAY - user["used"])

    await callback.message.answer(
        "<b>👤 Твій профіль</b>\n\n"
        f"🎁 Безкоштовних генерацій залишилося: {remaining}\n"
        f"⭐ Додаткових генерацій: {user['credits']}",
        parse_mode="HTML",
        reply_markup=back_keyboard(),
    )

    await callback.answer()


# ============================================================
# GENERATION WORKFLOW
# ============================================================

@dp.callback_query(F.data == "generate")
async def begin_generation(
    callback: CallbackQuery,
    state: FSMContext,
):
    await state.set_state(PhotoStates.waiting_prompt)

    await callback.message.answer(
        "🎨 Напиши, яке фото створити.\n\n"
        "Приклад: реалістичний портрет чоловіка на нічній вулиці, "
        "природне освітлення, професійна фотографія.\n\n"
        "Для скасування введи /cancel."
    )

    await callback.answer()


async def create_and_send_image(
    message: Message,
    prompt: str,
):
    if len(prompt) < 3:
        await message.answer("Напиши детальніший опис фото.")
        return

    if len(prompt) > 1500:
        await message.answer(
            "Опис занадто довгий. Максимум 1500 символів."
        )
        return

    if not can_generate(message.from_user.id):
        await message.answer(
            "🎁 Безкоштовний ліміт закінчився. "
            "Можна придбати додаткові генерації.",
            reply_markup=main_keyboard(),
        )
        return

    status = await message.answer(
        "🎨 Створюю реалістичне фото..."
    )

    try:
        image_bytes = await asyncio.to_thread(
            generate_image_sync,
            prompt,
        )

        if not charge_generation(message.from_user.id):
            await message.answer(
                "Ліміт генерацій закінчився. Спробуй пізніше."
            )
            return

        await message.answer_photo(
            photo=BufferedInputFile(
                image_bytes,
                filename="marketphoto.png",
            ),
            caption="✨ Готово! Ось твоє фото.",
        )

    except Exception:
        logger.exception("Image generation failed")

        await message.answer(
            "Не вдалося створити фото.\n\n"
            "Перевір Render → Logs. Можливі причини: "
            "недійсний API-ключ, недоступна модель, "
            "обмеження квоти або помилка Google API."
        )

    finally:
        try:
            await status.delete()
        except Exception:
            pass


@dp.message(PhotoStates.waiting_prompt, F.text)
async def generation_prompt_handler(
    message: Message,
    state: FSMContext,
):
    prompt = message.text.strip()

    await state.clear()
    await create_and_send_image(message, prompt)


# ============================================================
# PHOTO EDITING WORKFLOW
# ============================================================

@dp.callback_query(F.data == "edit")
async def begin_editing(
    callback: CallbackQuery,
    state: FSMContext,
):
    await state.set_state(PhotoStates.waiting_photo)

    await callback.message.answer(
        "🪄 Надішли фотографію як звичайне фото в Telegram.\n\n"
        "Потім напиши, що змінити.\n\n"
        "Наприклад: «Заміни фон на сучасну фотостудію, "
        "а обличчя та одяг залиш без змін».\n\n"
        "Для скасування введи /cancel."
    )

    await callback.answer()


@dp.message(PhotoStates.waiting_photo, F.photo)
async def receive_photo(
    message: Message,
    state: FSMContext,
):
    if not can_generate(message.from_user.id):
        await state.clear()
        await message.answer(
            "Безкоштовний ліміт закінчився. "
            "Придбай додаткові генерації.",
            reply_markup=main_keyboard(),
        )
        return

    try:
        file_info = await bot.get_file(
            message.photo[-1].file_id
        )

        downloaded = BytesIO()

        await bot.download_file(
            file_info.file_path,
            destination=downloaded,
        )

        photo_bytes = downloaded.getvalue()

        if not photo_bytes:
            raise ValueError("Отримано порожній файл.")

        # Перевіряємо, що Telegram-файл справді є зображенням.
        with Image.open(BytesIO(photo_bytes)) as image:
            image.verify()

        await state.update_data(
            photo_bytes=photo_bytes
        )

        await state.set_state(
            PhotoStates.waiting_edit_instruction
        )

        await message.answer(
            "📷 Фото отримано!\n\n"
            "Тепер напиши, що потрібно змінити."
        )

    except Exception:
        logger.exception("Photo download or validation failed")
        await state.clear()
        await message.answer(
            "Не вдалося відкрити фото. Надішли інше зображення."
        )


@dp.message(PhotoStates.waiting_photo, F.text)
async def photo_required(message: Message):
    await message.answer(
        "Спочатку надішли фотографію як звичайне фото."
    )


@dp.message(PhotoStates.waiting_edit_instruction, F.text)
async def edit_instruction_handler(
    message: Message,
    state: FSMContext,
):
    instruction = message.text.strip()

    if len(instruction) < 3:
        await message.answer(
            "Опиши детальніше, що потрібно змінити."
        )
        return

    if len(instruction) > 1500:
        await message.answer(
            "Інструкція занадто довга. Максимум 1500 символів."
        )
        return

    if not can_generate(message.from_user.id):
        await state.clear()
        await message.answer(
            "Ліміт генерацій закінчився. Придбай додаткові генерації.",
            reply_markup=main_keyboard(),
        )
        return

    data = await state.get_data()
    photo_bytes = data.get("photo_bytes")

    if not photo_bytes:
        await state.clear()
        await message.answer(
            "Фото не знайдено. Натисни «Редагувати фото» і надішли його знову."
        )
        return

    status = await message.answer(
        "🪄 Редагую фото..."
    )

    try:
        image_bytes = await asyncio.to_thread(
            edit_image_sync,
            photo_bytes,
            instruction,
        )

        if not charge_generation(message.from_user.id):
            await message.answer(
                "Ліміт генерацій закінчився. Спробуй пізніше."
            )
            return

        await message.answer_photo(
            photo=BufferedInputFile(
                image_bytes,
                filename="marketphoto_edited.png",
            ),
            caption="✨ Готово! Фото відредаговано.",
        )

    except Exception:
        logger.exception("Photo editing failed")
        await message.answer(
            "Не вдалося відредагувати фото.\n"
            "Перевір Render Logs: Google API має повернути зображення."
        )

    finally:
        await state.clear()

        try:
            await status.delete()
        except Exception:
            pass


# ============================================================
# DIRECT TEXT PROMPTS
# Users can also generate by sending text without pressing a button.
# ============================================================

@dp.message(F.text & ~F.text.startswith("/"))
async def direct_prompt_handler(message: Message):
    await create_and_send_image(
        message,
        message.text.strip(),
    )


@dp.message(F.photo)
async def photo_outside_edit_flow(message: Message):
    await message.answer(
        "Щоб редагувати це фото, спочатку натисни "
        "«Редагувати фото» в меню, а потім надішли зображення.",
        reply_markup=main_keyboard(),
    )


# ============================================================
# TELEGRAM STARS SHOP
# ============================================================

@dp.callback_query(F.data == "shop")
async def shop_handler(callback: CallbackQuery):
    keyboard = InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text=(
                        f"⭐ {PACK_PRICE_STARS} Stars — "
                        f"{PACK_CREDITS} генерацій"
                    ),
                    callback_data="buy_pack",
                )
            ],
            [
                InlineKeyboardButton(
                    text="⬅️ Назад",
                    callback_data="home",
                )
            ],
        ]
    )

    await callback.message.answer(
        "<b>⭐ Магазин генерацій</b>\n\n"
        f"{PACK_CREDITS} додаткових генерацій "
        f"за {PACK_PRICE_STARS} Telegram Stars.",
        reply_markup=keyboard,
        parse_mode="HTML",
    )

    await callback.answer()


@dp.callback_query(F.data == "buy_pack")
async def buy_pack_handler(callback: CallbackQuery):
    await bot.send_invoice(
        chat_id=callback.from_user.id,
        title="MarketPhoto AI — генерації",
        description=f"{PACK_CREDITS} додаткових генерацій фото",
        payload=f"marketphoto_pack_{callback.from_user.id}",
        provider_token="",
        currency="XTR",
        prices=[
            LabeledPrice(
                label=f"{PACK_CREDITS} генерацій",
                amount=PACK_PRICE_STARS,
            )
        ],
    )

    await callback.answer()


@dp.pre_checkout_query()
async def pre_checkout_handler(query: PreCheckoutQuery):
    expected_payload = (
        f"marketphoto_pack_{query.from_user.id}"
    )

    if (
        query.invoice_payload != expected_payload
        or query.currency != "XTR"
        or query.total_amount != PACK_PRICE_STARS
    ):
        await query.answer(
            ok=False,
            error_message="Не вдалося перевірити замовлення.",
        )
        return

    await query.answer(ok=True)


@dp.message(F.successful_payment)
async def successful_payment_handler(message: Message):
    payment = message.successful_payment

    expected_payload = (
        f"marketphoto_pack_{message.from_user.id}"
    )

    if (
        payment.invoice_payload != expected_payload
        or payment.currency != "XTR"
        or payment.total_amount != PACK_PRICE_STARS
    ):
        logger.warning(
            "Unexpected payment details for user %s",
            message.from_user.id,
        )

        await message.answer(
            "Не вдалося перевірити платіж. Звернися до адміністратора."
        )
        return

    granted = record_payment_once(
        payment.telegram_payment_charge_id,
        message.from_user.id,
        payment.total_amount,
        payment.currency,
    )

    if granted:
        await message.answer(
            f"✅ Оплата успішна!\n"
            f"Додано генерацій: {PACK_CREDITS}",
            reply_markup=main_keyboard(),
        )
    else:
        await message.answer(
            "Цей платіж уже був оброблений."
        )


# ============================================================
# FALLBACK HANDLERS
# ============================================================

@dp.callback_query()
async def unknown_callback_handler(callback: CallbackQuery):
    logger.warning(
        "Unhandled callback data=%r",
        callback.data,
    )

    await callback.answer(
        "Кнопка застаріла. Відкрий /start і спробуй знову.",
        show_alert=True,
    )


@dp.message()
async def unknown_message_handler(
    message: Message,
    state: FSMContext,
):
    current_state = await state.get_state()

    logger.warning(
        "Unhandled message: user=%s state=%r type=%s",
        message.from_user.id if message.from_user else None,
        current_state,
        message.content_type,
    )

    await message.answer(
        "Не зрозумів повідомлення. Надішли текст, фото для редагування "
        "або натисни /start.",
        reply_markup=main_keyboard(),
    )


# ============================================================
# RENDER HEALTH SERVER
# ============================================================

async def health(request):
    return web.Response(text="MarketPhoto AI is running")


async def start_web_server():
    app = web.Application()
    app.router.add_get("/", health)
    app.router.add_get("/health", health)

    runner = web.AppRunner(app)
    await runner.setup()

    site = web.TCPSite(
        runner,
        "0.0.0.0",
        PORT,
    )

    await site.start()

    logger.info(
        "Health server listening on 0.0.0.0:%s",
        PORT,
    )

    return runner


# ============================================================
# STARTUP
# ============================================================

async def main():
    init_db()

    # Render Web Service needs a listening HTTP port.
    runner = await start_web_server()

    try:
        logger.info(
            "Starting MarketPhoto AI polling. Model=%s",
            IMAGE_MODEL,
        )

        # Clear webhook if one was previously configured.
        # This does NOT stop another polling process using the same token.
        await bot.delete_webhook(drop_pending_updates=False)

        await dp.start_polling(
            bot,
            allowed_updates=dp.resolve_used_update_types(),
            close_bot_session=False,
        )

    finally:
        await runner.cleanup()

        try:
            await dp.storage.close()
        except Exception:
            pass

        await bot.session.close()
        client.close()


if __name__ == "__main__":
    asyncio.run(main())
