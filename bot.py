
import os
import asyncio
import base64
import json
import logging
import random
import sqlite3
from datetime import datetime
from io import BytesIO
from zoneinfo import ZoneInfo

import aiohttp
from aiohttp import web
from PIL import Image, UnidentifiedImageError

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


# ============================================================
# CONFIGURATION
# Render -> Environment
# ============================================================

BOT_TOKEN = (
    os.getenv("BOT_TOKEN")
    or os.getenv("TELEGRAM_TOKEN")
    or ""
).strip()

CF_ACCOUNT_ID = (
    os.getenv("CLOUDFLARE_ACCOUNT_ID")
    or os.getenv("CF_ACCOUNT_ID")
    or ""
).strip()

CF_API_TOKEN = (
    os.getenv("CLOUDFLARE_API_TOKEN")
    or os.getenv("CF_API_TOKEN")
    or ""
).strip()

CF_MODEL = "@cf/black-forest-labs/flux-2-klein-4b"

CF_URL = (
    "https://api.cloudflare.com/client/v4/accounts/"
    + CF_ACCOUNT_ID
    + "/ai/run/"
    + CF_MODEL
)

PORT = int(os.getenv("PORT", "10000"))
DB_PATH = os.getenv("DB_PATH", "marketphoto.db")
TIMEZONE = os.getenv("APP_TIMEZONE", "Europe/Kyiv")

FREE_PER_DAY = 5
PACK_CREDITS = 10
PACK_PRICE_STARS = 25

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s:%(name)s:%(message)s",
)
logger = logging.getLogger("marketphoto")

if not BOT_TOKEN:
    raise RuntimeError("BOT_TOKEN не заданий у Render Environment")

if not CF_ACCOUNT_ID:
    raise RuntimeError(
        "CLOUDFLARE_ACCOUNT_ID не заданий у Render Environment"
    )

if not CF_API_TOKEN:
    raise RuntimeError(
        "CLOUDFLARE_API_TOKEN не заданий у Render Environment"
    )

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
                user_id INTEGER NOT NULL
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

        saved_day, used, credits = con.execute(
            """
            SELECT day, used, credits
            FROM users
            WHERE user_id=?
            """,
            (user_id,),
        ).fetchone()

        if saved_day != today:
            used = 0
            con.execute(
                "UPDATE users SET day=?, used=0 WHERE user_id=?",
                (today, user_id),
            )

        return {"used": used, "credits": credits}


def can_generate(user_id):
    user = get_user(user_id)
    return (
        user["used"] < FREE_PER_DAY
        or user["credits"] > 0
    )


def charge_generation(user_id):
    """Списує ліміт лише після успішної генерації."""

    today = today_string()

    with db_connect() as con:
        con.execute("BEGIN IMMEDIATE")

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
            WHERE user_id=?
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
            SET day=?, used=?, credits=?
            WHERE user_id=?
            """,
            (today, used, credits, user_id),
        )

    return True


def record_payment_once(charge_id, user_id):
    """Зараховує одну покупку лише один раз."""

    today = today_string()

    with db_connect() as con:
        con.execute("BEGIN IMMEDIATE")

        cursor = con.execute(
            """
            INSERT OR IGNORE INTO payments (charge_id, user_id)
            VALUES (?, ?)
            """,
            (charge_id, user_id),
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
            "UPDATE users SET credits=credits+? WHERE user_id=?",
            (PACK_CREDITS, user_id),
        )

    return True


# ============================================================
# KEYBOARDS
# ============================================================

def main_keyboard():
    return InlineKeyboardMarkup(inline_keyboard=[
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
    ])


def back_keyboard():
    return InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(
                text="⬅️ Головне меню",
                callback_data="home",
            )
        ]
    ])


# ============================================================
# STATES
# ============================================================

class PhotoStates(StatesGroup):
    waiting_prompt = State()
    waiting_photo = State()
    waiting_edit_instruction = State()


# ============================================================
# CLOUDFLARE WORKERS AI
# FLUX.2 Klein 4B
# ============================================================

def get_error_message(raw):
    """Повертає зрозумілий текст помилки API."""

    try:
        data = json.loads(raw.decode("utf-8"))
    except Exception:
        return raw[:1500].decode("utf-8", errors="replace")

    errors = data.get("errors", [])
    if errors:
        messages = []

        for item in errors:
            if isinstance(item, dict):
                messages.append(
                    str(item.get("message", item))
                )
            else:
                messages.append(str(item))

        return "; ".join(messages)[:1500]

    return str(data.get("message", data))[:1500]


def decode_image_response(raw, content_type):
    """
    Cloudflare може віддати зображення як бінарні дані
    або як Base64 у JSON result.image.
    """

    if not raw:
        raise RuntimeError("Cloudflare повернув порожню відповідь.")

    # Відповідь уже є готовим файлом зображення.
    if (
        content_type.startswith("image/")
        or raw.startswith(b"\x89PNG\r\n\x1a\n")
        or raw.startswith(b"\xff\xd8\xff")
        or raw.startswith(b"RIFF")
    ):
        return raw

    try:
        data = json.loads(raw.decode("utf-8"))
    except Exception as exc:
        raise RuntimeError(
            "Не вдалося прочитати відповідь Cloudflare: "
            + raw[:500].decode("utf-8", errors="replace")
        ) from exc

    if data.get("success") is False:
        raise RuntimeError(
            "Cloudflare API: " + get_error_message(raw)
        )

    result = data.get("result", data)

    if isinstance(result, dict):
        image_data = (
            result.get("image")
            or result.get("data")
        )
    elif isinstance(result, str):
        image_data = result
    else:
        image_data = None

    if not image_data:
        raise RuntimeError(
            "У відповіді Cloudflare немає поля image. "
            "Перевір Render Logs."
        )

    if image_data.startswith("data:image"):
        image_data = image_data.split(",", 1)[1]

    try:
        image_bytes = base64.b64decode(image_data)
    except Exception as exc:
        raise RuntimeError(
            "Cloudflare повернув зображення в невідомому форматі."
        ) from exc

    if not image_bytes:
        raise RuntimeError("Отримано порожнє зображення.")

    return image_bytes


async def cloudflare_image(
    prompt,
    reference_image=None,
    width=1024,
    height=1024,
):
    """
    Створює фото або редагує референс через REST API.
    Для FLUX.2 Klein 4B потрібен multipart/form-data.
    """

    form = aiohttp.FormData()

    form.add_field("prompt", prompt)
    form.add_field("width", str(width))
    form.add_field("height", str(height))
    form.add_field("guidance", "4")
    form.add_field(
        "seed",
        str(random.randint(1, 2**31 - 1)),
    )

    if reference_image:
        # За документацією Cloudflare референсні зображення
        # для цієї моделі мають бути меншими за 512x512.
        form.add_field(
            "input_image_0",
            reference_image,
            filename="reference.png",
            content_type="image/png",
        )

    headers = {
        "Authorization": f"Bearer {CF_API_TOKEN}",
    }

    timeout = aiohttp.ClientTimeout(
        total=240,
        connect=20,
        sock_read=220,
    )

    async with aiohttp.ClientSession(
        timeout=timeout
    ) as session:
        async with session.post(
            CF_URL,
            headers=headers,
            data=form,
        ) as response:
            raw = await response.read()
            content_type = response.headers.get(
                "Content-Type", ""
            ).lower()

            if response.status >= 400:
                details = get_error_message(raw)

                logger.error(
                    "Cloudflare returned HTTP %s: %s",
                    response.status,
                    details,
                )

                if response.status == 401:
                    raise RuntimeError(
                        "Cloudflare API Token неправильний або недійсний."
                    )

                if response.status == 403:
                    raise RuntimeError(
                        "Cloudflare заборонив запит. Перевір дозволи токена "
                        "Workers AI Read та Workers AI Edit."
                    )

                if response.status == 429:
                    raise RuntimeError(
                        "Досягнуто ліміт Cloudflare Workers AI. "
                        "Спробуй знову після відновлення квоти."
                    )

                raise RuntimeError(
                    f"Cloudflare HTTP {response.status}: {details}"
                )

            return decode_image_response(
                raw,
                content_type,
            )


# ============================================================
# PROMPT ENHANCEMENT
# ============================================================

def build_generation_prompt(user_prompt):
    return (
        "Create a high-quality photorealistic photograph. "
        "Follow the user's description closely. Natural skin texture, "
        "realistic human anatomy, believable hands and eyes, natural "
        "lighting and shadows, detailed materials, physically realistic "
        "environment, carefully composed professional photography, "
        "balanced colors, sharp focus. Avoid cartoon style, plastic skin, "
        "unwanted text, watermarks and unnecessary objects. "
        "User description: "
        + user_prompt
    )


def build_edit_prompt(instruction):
    return (
        "Edit the provided reference photograph according to this instruction. "
        "Preserve the identity, face, age, and main features of the person "
        "as much as possible. Change only what the user requests. Make the "
        "result photorealistic, with natural skin texture, realistic lighting "
        "and shadows, balanced colors, and consistent perspective. "
        "Editing instruction: "
        + instruction
    )


def prepare_reference(raw_bytes):
    """
    Готує фото меншого за 512x512 розміру
    та підбирає розмір вихідного зображення зі збереженням пропорцій.
    """

    try:
        with Image.open(BytesIO(raw_bytes)) as original:
            original = original.convert("RGB")
            orig_width, orig_height = original.size
            if orig_width <= 0 or orig_height <= 0:
                raise ValueError("Некоректний розмір фото.")

            # Цільовий розмір до 1024 пікселів по довшій стороні.
            scale = min(
                1024 / orig_width,
                1024 / orig_height,
            )

            target_width = max(
                256,
                min(
                    1024,
                    int(orig_width * scale / 64) * 64,
                ),
            )
            target_height = max(
                256,
                min(
                    1024,
                    int(orig_height * scale / 64) * 64,
                ),
            )

            # Для референсу обмежуємо обидві сторони до 480.
            reference = original.copy()
            reference.thumbnail(
                (480, 480),
                Image.Resampling.LANCZOS,
            )

            output = BytesIO()
            reference.save(output, format="PNG")

            return (
                output.getvalue(),
                target_width,
                target_height,
            )

    except (UnidentifiedImageError, OSError) as exc:
        raise ValueError(
            "Не вдалося прочитати завантажене фото."
        ) from exc


# ============================================================
# START AND COMMANDS
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
        "<b>📸 MarketPhoto AI</b>\n\n"
        "🎨 Генеруй реалістичні фото з тексту.\n"
        "🪄 Редагуй завантажені фотографії.\n\n"
        f"🎁 Безкоштовно: до {FREE_PER_DAY} генерацій на день.\n"
        f"Залишилося сьогодні: {remaining}\n\n"
        "Вибери дію в меню:",
        parse_mode="HTML",
        reply_markup=main_keyboard(),
    )


@dp.message(Command("help"))
async def help_handler(message: Message):
    await message.answer(
        "🎨 <b>Створити фото</b> — натисни кнопку та напиши опис.\n\n"
        "🪄 <b>Редагувати фото</b> — натисни кнопку, надішли фото, "
        "потім опиши зміни.\n\n"
        "Для скасування введи /cancel.",
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
    if message.from_user.id != int(
        os.getenv("ADMIN_ID", "5694967765")
    ):
        await message.answer("Немає доступу.")
        return

    await message.answer(
        "🛠 MarketPhoto AI\n\n"
        f"Модель: {CF_MODEL}\n"
        f"Cloudflare Account ID задано: {bool(CF_ACCOUNT_ID)}\n"
        f"API Token задано: {bool(CF_API_TOKEN)}"
    )


# ============================================================
# MENU CALLBACKS
# ============================================================

@dp.callback_query(F.data == "home")
async def home_handler(
    callback: CallbackQuery,
    state: FSMContext,
):
    await state.clear()

    await callback.message.answer(
        "Головне меню:",
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
# GENERATION FROM TEXT
# ============================================================

@dp.callback_query(F.data == "generate")
async def begin_generation(
    callback: CallbackQuery,
    state: FSMContext,
):
    await state.set_state(PhotoStates.waiting_prompt)

    await callback.message.answer(
        "🎨 Напиши опис фото.\n\n"
        "Наприклад: реалістичний портрет молодого чоловіка "
        "на нічній вулиці, природне освітлення, професійна фотографія.\n\n"
        "Скасування: /cancel"
    )

    await callback.answer()


async def create_photo(message: Message, prompt: str):
    prompt = prompt.strip()

    if len(prompt) < 3 or len(prompt) > 1500:
        await message.answer(
            "Опис має містити від 3 до 1500 символів."
        )
        return

    if not can_generate(message.from_user.id):
        await message.answer(
            "🎁 Ліміт генерацій закінчився. "
            "Можеш спробувати завтра або купити додаткові генерації.",
            reply_markup=main_keyboard(),
        )
        return

    status = await message.answer(
        "🎨 Створюю реалістичне фото через Cloudflare..."
    )

    try:
        image_bytes = await cloudflare_image(
            build_generation_prompt(prompt),
            width=1024,
            height=1024,
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

    except Exception as exc:
        logger.exception("Cloudflare image generation failed")

        await message.answer(
            "Не вдалося створити фото.\n\n"
            f"Причина: {str(exc)[:700]}"
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

    await create_photo(message, prompt)


# ============================================================
# EDIT UPLOADED PHOTO
# ============================================================

@dp.callback_query(F.data == "edit")
async def begin_edit(
    callback: CallbackQuery,
    state: FSMContext,
):
    await state.set_state(PhotoStates.waiting_photo)

    await callback.message.answer(
        "🪄 Надішли фотографію як звичайне фото.\n\n"
        "Після цього напиши, що саме змінити.\n\n"
        "Наприклад: «Заміни фон на пляж, обличчя залиш без змін».\n\n"
        "Скасування: /cancel"
    )

    await callback.answer()


@dp.message(PhotoStates.waiting_photo, F.photo)
async def receive_edit_photo(
    message: Message,
    state: FSMContext,
):
    if not can_generate(message.from_user.id):
        await state.clear()

        await message.answer(
            "🎁 Ліміт генерацій закінчився.",
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

        raw_photo = downloaded.getvalue()

        # Переконуємось, що зображення читається.
        prepare_reference(raw_photo)

        await state.update_data(
            photo_bytes=raw_photo
        )

        await state.set_state(
            PhotoStates.waiting_edit_instruction
        )

        await message.answer(
            "📷 Фото отримано!\n"
            "Тепер напиши, що потрібно змінити."
        )

    except Exception:
        logger.exception("Could not download/validate uploaded photo")

        await state.clear()

        await message.answer(
            "Не вдалося відкрити фото. Спробуй надіслати його ще раз."
        )


@dp.message(PhotoStates.waiting_photo, F.text)
async def wait_for_photo(message: Message):
    await message.answer(
        "Спочатку надішли фотографію, а потім інструкцію."
    )


@dp.message(PhotoStates.waiting_edit_instruction, F.text)
async def edit_instruction_handler(
    message: Message,
    state: FSMContext,
):
    instruction = message.text.strip()

    if len(instruction) < 3 or len(instruction) > 1500:
        await message.answer(
            "Інструкція має містити від 3 до 1500 символів."
        )
        return

    if not can_generate(message.from_user.id):
        await state.clear()

        await message.answer(
            "Безкоштовний ліміт закінчився.",
            reply_markup=main_keyboard(),
        )
        return

    data = await state.get_data()
    raw_photo = data.get("photo_bytes")

    if not raw_photo:
        await state.clear()

        await message.answer(
            "Фото не знайдено. Натисни «Редагувати фото» і надішли його знову."
        )
        return

    status = await message.answer(
        "🪄 Редагую фото через Cloudflare..."
    )

    try:
        reference, width, height = prepare_reference(raw_photo)

        image_bytes = await cloudflare_image(
            build_edit_prompt(instruction),
            reference_image=reference,
            width=width,
            height=height,
        )

        if not charge_generation(message.from_user.id):
            await message.answer(
                "Ліміт генерацій закінчився."
            )
            return

        await message.answer_photo(
            photo=BufferedInputFile(
                image_bytes,
                filename="marketphoto_edit.png",
            ),
            caption="✨ Готово! Фото відредаговано.",
        )

    except Exception as exc:
        logger.exception("Cloudflare image editing failed")

        await message.answer(
            "Не вдалося відредагувати фото.\n\n"
            f"Причина: {str(exc)[:700]}"
        )

    finally:
        await state.clear()

        try:
            await status.delete()
        except Exception:
            pass


# ============================================================
# DIRECT TEXT GENERATION
# ============================================================

@dp.message(F.text & ~F.text.startswith("/"))
async def direct_text_handler(message: Message):
    await create_photo(
        message,
        message.text.strip(),
    )


@dp.message(F.photo)
async def photo_without_edit_mode(message: Message):
    await message.answer(
        "Щоб редагувати фото, спочатку натисни кнопку "
        "«Редагувати фото» в меню.",
        reply_markup=main_keyboard(),
    )


# ============================================================
# TELEGRAM STARS SHOP
# ============================================================

@dp.callback_query(F.data == "shop")
async def shop_handler(callback: CallbackQuery):
    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(
                text=f"⭐ {PACK_PRICE_STARS} Stars — {PACK_CREDITS} генерацій",
                callback_data="buy_pack",
            )
        ],
        [
            InlineKeyboardButton(
                text="⬅️ Назад",
                callback_data="home",
            )
        ],
    ])

    await callback.message.answer(
        "<b>⭐ Магазин генерацій</b>\n\n"
        f"{PACK_CREDITS} додаткових генерацій за "
        f"{PACK_PRICE_STARS} Telegram Stars.",
        parse_mode="HTML",
        reply_markup=keyboard,
    )

    await callback.answer()


@dp.callback_query(F.data == "buy_pack")
async def buy_pack_handler(callback: CallbackQuery):
    await bot.send_invoice(
        chat_id=callback.from_user.id,
        title="MarketPhoto AI — генерації",
        description=f"{PACK_CREDITS} додаткових генерацій",
        payload=f"marketphoto_pack_{callback.from_user.id}",
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
    expected = f"marketphoto_pack_{query.from_user.id}"

    if (
        query.invoice_payload != expected
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
    expected = f"marketphoto_pack_{message.from_user.id}"

    if (
        payment.invoice_payload != expected
        or payment.currency != "XTR"
        or payment.total_amount != PACK_PRICE_STARS
    ):
        await message.answer(
            "Не вдалося перевірити платіж."
        )
        return

    granted = record_payment_once(
        payment.telegram_payment_charge_id,
        message.from_user.id,
    )

    if granted:
        await message.answer(
            f"✅ Оплату отримано!\nДодано {PACK_CREDITS} генерацій.",
            reply_markup=main_keyboard(),
        )
    else:
        await message.answer(
            "Цей платіж уже оброблено."
        )


# ============================================================
# FALLBACK HANDLERS
# ============================================================

@dp.callback_query()
async def unknown_callback(callback: CallbackQuery):
    await callback.answer(
        "Кнопка застаріла. Відкрий /start і спробуй знову.",
        show_alert=True,
    )


@dp.message()
async def unknown_message(
    message: Message,
    state: FSMContext,
):
    current = await state.get_state()

    logger.warning(
        "Unhandled update: content_type=%s state=%r",
        message.content_type,
        current,
    )

    await message.answer(
        "Не зрозумів повідомлення. Натисни /start.",
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
        "Health server listening on port %s",
        PORT,
    )

    return runner


# ============================================================
# STARTUP
# ============================================================

async def main():
    init_db()

    runner = await start_web_server()

    try:
        logger.info(
            "Starting MarketPhoto AI. Model=%s",
            CF_MODEL,
        )

        # Ensure polling mode if webhook was configured previously.
        # Stop any other process using the same Telegram token first.
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


if __name__ == "__main__":
    asyncio.run(main())
