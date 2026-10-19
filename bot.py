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
from huggingface_hub import InferenceClient
from PIL import Image


# ==================================================
# SETTINGS
# Усі секретні ключі додавай у Render -> Environment
# ==================================================

BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
HF_TOKEN = os.getenv("HF_TOKEN", "").strip()

ADMIN_ID = int(os.getenv("ADMIN_ID", "5694967765"))
PORT = int(os.getenv("PORT", "10000"))
DB_PATH = os.getenv("DB_PATH", "marketphoto.db")
TIMEZONE = os.getenv("APP_TIMEZONE", "Europe/Kyiv")

FREE_PER_DAY = 5
PACK_CREDITS = 10
PACK_PRICE_STARS = 25

TEXT_MODEL = os.getenv(
    "HF_TEXT_MODEL",
    "black-forest-labs/FLUX.1-dev",
)

EDIT_MODEL = os.getenv(
    "HF_EDIT_MODEL",
    "black-forest-labs/FLUX.1-Kontext-dev",
)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s:%(name)s:%(message)s",
)

logger = logging.getLogger("marketphoto")

if not BOT_TOKEN:
    raise RuntimeError(
        "BOT_TOKEN не заданий у Render Environment"
    )

if not HF_TOKEN:
    logger.warning(
        "HF_TOKEN відсутній. Генерація та редагування фото не працюватимуть."
    )

bot = Bot(token=BOT_TOKEN)
dp = Dispatcher(storage=MemoryStorage())


# ==================================================
# DATABASE
# ==================================================

def today_string():
    try:
        return datetime.now(
            ZoneInfo(TIMEZONE)
        ).date().isoformat()
    except Exception:
        return datetime.now().date().isoformat()


def connect_db():
    return sqlite3.connect(DB_PATH, timeout=30)


def init_db():
    with connect_db() as con:
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

    with connect_db() as con:
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
    """
    Списує генерацію після успішного створення фото.
    Спочатку використовує безкоштовний денний ліміт.
    Після нього використовує куплені генерації.
    """

    today = today_string()

    with connect_db() as con:
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


def add_credits(user_id, amount):
    today = today_string()

    with connect_db() as con:
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
            (amount, user_id),
        )


def record_payment(charge_id, user_id, amount, currency):
    """
    Не дозволяє зарахувати один платіж двічі.
    """

    today = today_string()

    with connect_db() as con:
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


# ==================================================
# KEYBOARDS
# ==================================================

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


# ==================================================
# FSM STATES
# ==================================================

class PhotoStates(StatesGroup):
    waiting_prompt = State()
    waiting_photo = State()
    waiting_edit_instruction = State()


# ==================================================
# IMAGE GENERATION
# Hugging Face Inference Providers
# ==================================================

def get_hf_client():
    if not HF_TOKEN:
        raise RuntimeError(
            "HF_TOKEN не заданий у Render Environment"
        )

    return InferenceClient(
        provider="auto",
        api_key=HF_TOKEN,
    )


def image_to_bytes(image):
    output = BytesIO()

    image.convert("RGB").save(
        output,
        format="JPEG",
        quality=95,
        optimize=True,
    )

    return output.getvalue()


def generate_image_sync(prompt):
    client = get_hf_client()

    enhanced_prompt = (
        "Create a highly photorealistic professional photograph. "
        "Natural skin texture, realistic human anatomy, natural hands, "
        "realistic eyes, accurate light and shadows, natural colors, "
        "professional camera photography, sharp focus, detailed textures. "
        "Avoid cartoon style, illustration, plastic skin, oversaturation, "
        "blurry details and distorted fingers. "
        "Scene: "
        + prompt
    )

    image = client.text_to_image(
        prompt=enhanced_prompt,
        model=TEXT_MODEL,
    )

    return image_to_bytes(image)


def edit_image_sync(photo_bytes, instruction):
    client = get_hf_client()

    source = Image.open(
        BytesIO(photo_bytes)
    ).convert("RGB")

    prompt = (
        "Edit the provided photograph according to this instruction: "
        + instruction
        + ". Make the result photorealistic, with natural lighting, "
        "realistic textures and natural colors. Preserve the person's "
        "identity and facial features as much as possible. "
        "Do not change unrelated details."
    )

    result = client.image_to_image(
        image=source,
        prompt=prompt,
        model=EDIT_MODEL,
    )

    return image_to_bytes(result)


# ==================================================
# /START
# ==================================================

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
        "Створюй реалістичні фото з тексту або редагуй "
        "власні фотографії.\n\n"
        f"🎁 Безкоштовно: {FREE_PER_DAY} генерацій на день.\n"
        f"Залишилося сьогодні: {remaining}\n\n"
        "Вибери дію в меню:",
        reply_markup=main_keyboard(),
        parse_mode="HTML",
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

    hf_status = "налаштований" if HF_TOKEN else "НЕ налаштований"

    await message.answer(
        "🛠 MarketPhoto AI\n\n"
        "Telegram-бот працює.\n"
        f"HF_TOKEN: {hf_status}\n"
        f"Безкоштовний ліміт: {FREE_PER_DAY} на день.\n"
        f"Пакет: {PACK_CREDITS} генерацій за {PACK_PRICE_STARS} Stars."
    )


# ==================================================
# MAIN MENU
# ==================================================

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


# ==================================================
# USER PROFILE
# ==================================================

@dp.callback_query(F.data == "profile")
async def profile_handler(callback: CallbackQuery):
    user = get_user(callback.from_user.id)

    remaining = max(
        0,
        FREE_PER_DAY - user["used"],
    )

    await callback.message.answer(
        "<b>👤 Твій профіль</b>\n\n"
        f"🎁 Безкоштовних генерацій залишилося: {remaining}\n"
        f"⭐ Додаткових генерацій: {user['credits']}",
        parse_mode="HTML",
        reply_markup=back_keyboard(),
    )

    await callback.answer()


# ==================================================
# GENERATE A PHOTO FROM TEXT
# ==================================================

@dp.callback_query(F.data == "generate")
async def begin_generation(
    callback: CallbackQuery,
    state: FSMContext,
):
    await state.set_state(PhotoStates.waiting_prompt)

    await callback.message.answer(
        "🎨 Напиши, яке фото потрібно створити.\n\n"
        "Приклад:\n"
        "Реалістичний портрет молодого чоловіка на вулиці "
        "вечором, природне освітлення, професійна камера.\n\n"
        "Скасування: /cancel"
    )

    await callback.answer()


@dp.message(PhotoStates.waiting_prompt, F.text)
async def generation_prompt_handler(
    message: Message,
    state: FSMContext,
):
    prompt = message.text.strip()

    if len(prompt) < 3:
        await message.answer(
            "Напиши детальніший опис фото."
        )
        return

    if len(prompt) > 1500:
        await message.answer(
            "Опис занадто довгий. Максимум 1500 символів."
        )
        return

    if not can_generate(message.from_user.id):
        await state.clear()

        await message.answer(
            "🎁 Твій безкоштовний ліміт закінчився.\n"
            "Можеш придбати додаткові генерації.",
            reply_markup=main_keyboard(),
        )
        return

    if not HF_TOKEN:
        await state.clear()

        await message.answer(
            "Генератор не налаштований.\n"
            "Додай HF_TOKEN у Render → Environment."
        )
        return

    status = await message.answer(
        "🎨 Створюю реалістичне фото. Зачекай..."
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
                filename="marketphoto.jpg",
            ),
            caption="✨ Готово! Ось твоє фото.",
        )

    except Exception:
        logger.exception("Text-to-image failed")

        await message.answer(
            "Не вдалося створити фото.\n\n"
            "Можливі причини:\n"
            "• неправильний HF_TOKEN;\n"
            "• модель недоступна через провайдера;\n"
            "• закінчилися кошти або ліміти API.\n\n"
            "Перевір Render Logs."
        )

    finally:
        await state.clear()

        try:
            await status.delete()
        except Exception:
            pass


# ==================================================
# EDIT AN UPLOADED PHOTO
# ==================================================

@dp.callback_query(F.data == "edit")
async def begin_editing(
    callback: CallbackQuery,
    state: FSMContext,
):
    await state.set_state(PhotoStates.waiting_photo)

    await callback.message.answer(
        "🪄 Надішли фотографію як звичайне фото в Telegram.\n\n"
        "Після цього напиши, що потрібно змінити.\n\n"
        "Приклад:\n"
        "Заміни фон на сучасну фотостудію, обличчя залиш незмінним.\n\n"
        "Скасування: /cancel"
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
            "Безкоштовний ліміт закінчився. Придбай генерації.",
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

    except Exception:
        logger.exception("Could not download Telegram photo")

        await state.clear()

        await message.answer(
            "Не вдалося завантажити фотографію. Спробуй ще раз."
        )
        return

    await state.update_data(
        photo_bytes=photo_bytes
    )

    await state.set_state(
        PhotoStates.waiting_edit_instruction
    )

    await message.answer(
        "📷 Фото отримано!\n"
        "Тепер напиши, що саме потрібно змінити."
    )


@dp.message(PhotoStates.waiting_photo, F.text)
async def photo_required(message: Message):
    await message.answer(
        "Спочатку надішли фотографію, а не текст."
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

    if len(instruction) > 1000:
        await message.answer(
            "Інструкція занадто довга. Максимум 1000 символів."
        )
        return

    if not can_generate(message.from_user.id):
        await state.clear()

        await message.answer(
            "Ліміт генерацій закінчився. Придбай додаткові генерації.",
            reply_markup=main_keyboard(),
        )
        return

    if not HF_TOKEN:
        await state.clear()

        await message.answer(
            "Редагування не налаштоване.\n"
            "Додай HF_TOKEN у Render → Environment."
        )
        return

    data = await state.get_data()
    photo_bytes = data.get("photo_bytes")

    if not photo_bytes:
        await state.clear()

        await message.answer(
            "Фото не знайдено. Натисни «Редагувати фото» і надішли його ще раз."
        )
        return

    status = await message.answer(
        "🪄 Редагую фото. Зачекай..."
    )

    try:
        result_bytes = await asyncio.to_thread(
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
                result_bytes,
                filename="marketphoto_edit.jpg",
            ),
            caption="✨ Фото відредаговано!",
        )

    except Exception:
        logger.exception("Image editing failed")

        await message.answer(
            "Не вдалося відредагувати фото.\n\n"
            "Перевір доступність моделі редагування, токен Hugging Face "
            "і помилки в Render Logs."
        )

    finally:
        await state.clear()

        try:
            await status.delete()
        except Exception:
            pass


# ==================================================
# TELEGRAM STARS SHOP
# ==================================================

@dp.callback_query(F.data == "shop")
async def shop_handler(callback: CallbackQuery):
    keyboard = InlineKeyboardMarkup(
        inline_keyboard=[
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
async def pre_checkout_handler(
    query: PreCheckoutQuery,
):
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
            "Unexpected payment for user %s",
            message.from_user.id,
        )

        await message.answer(
            "Не вдалося перевірити платіж. Звернися до адміністратора."
        )
        return

    granted = record_payment(
        payment.telegram_payment_charge_id,
        message.from_user.id,
        payment.total_amount,
        payment.currency,
    )

    if granted:
        await message.answer(
            f"✅ Оплату отримано!\n"
            f"Додано генерацій: {PACK_CREDITS}",
            reply_markup=main_keyboard(),
        )
    else:
        await message.answer(
            "Цей платіж уже був оброблений."
        )


# ==================================================
# FALLBACK HANDLERS
# Щоб повідомлення не залишалися без відповіді
# ==================================================

@dp.callback_query()
async def unknown_callback_handler(
    callback: CallbackQuery,
):
    logger.warning(
        "Unknown callback data=%r user_id=%s",
        callback.data,
        callback.from_user.id if callback.from_user else None,
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

    if current_state == PhotoStates.waiting_prompt.state:
        await message.answer(
            "Напиши опис фото звичайним текстом або введи /cancel."
        )

    elif current_state == PhotoStates.waiting_photo.state:
        await message.answer(
            "Чекаю на фотографію. Надішли її як звичайне фото або введи /cancel."
        )

    elif current_state == PhotoStates.waiting_edit_instruction.state:
        await message.answer(
            "Напиши текстом, що змінити на фото, або введи /cancel."
        )

    else:
        await message.answer(
            "Не зрозумів повідомлення. Натисни /start, щоб відкрити меню.",
            reply_markup=main_keyboard(),
        )


# ==================================================
# RENDER HEALTH CHECK
# ==================================================

async def health(request):
    return web.Response(
        text="MarketPhoto AI is running"
    )


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


# ==================================================
# MAIN
# ==================================================

async def main():
    init_db()

    runner = await start_web_server()

    try:
        logger.info(
            "Starting MarketPhoto AI. HF_TOKEN configured=%s",
            bool(HF_TOKEN),
        )

        await dp.start_polling(
            bot,
            allowed_updates=dp.resolve_used_update_types(),
        )

    finally:
        await runner.cleanup()
        await bot.session.close()


if __name__ == "__main__":
    asyncio.run(main())

if __name__ == "__main__":
    asyncio.run(main())
