
import os
import asyncio
import logging
import sqlite3
from io import BytesIO
from datetime import date

from aiohttp import web
from aiogram import Bot, Dispatcher, F
from aiogram.filters import CommandStart, Command
from aiogram.types import (
    Message,
    CallbackQuery,
    BufferedInputFile,
    InlineKeyboardMarkup,
    InlineKeyboardButton,
    LabeledPrice,
    PreCheckoutQuery,
)
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import StatesGroup, State
from aiogram.fsm.storage.memory import MemoryStorage
from huggingface_hub import InferenceClient
from PIL import Image

# ==================================================
# SETTINGS
# Set BOT_TOKEN and HF_TOKEN in Render Environment.
# Never paste real tokens into this file.
# ==================================================

BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
HF_TOKEN = os.getenv("HF_TOKEN", "").strip()
ADMIN_ID = int(os.getenv("ADMIN_ID", "5694967765"))
PORT = int(os.getenv("PORT", "10000"))

DB_PATH = os.getenv("DB_PATH", "marketphoto.db")

FREE_PER_DAY = 5
PACK_CREDITS = 10
PACK_PRICE_STARS = 25

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("marketphoto")

if not BOT_TOKEN:
    raise RuntimeError("BOT_TOKEN is missing in Render Environment")

if not HF_TOKEN:
    logger.warning("HF_TOKEN is missing. Image generation will not work.")

bot = Bot(token=BOT_TOKEN)
dp = Dispatcher(storage=MemoryStorage())

# Hugging Face Inference Providers client
client = (
    InferenceClient(provider="fal-ai", api_key=HF_TOKEN)
    if HF_TOKEN
    else None
)


# ==================================================
# DATABASE
# ==================================================

def init_db():
    with sqlite3.connect(DB_PATH) as con:
        con.execute("""
            CREATE TABLE IF NOT EXISTS users (
                user_id INTEGER PRIMARY KEY,
                day TEXT NOT NULL,
                used INTEGER NOT NULL DEFAULT 0,
                credits INTEGER NOT NULL DEFAULT 0
            )
        """)


def get_user(user_id):
    today = date.today().isoformat()

    with sqlite3.connect(DB_PATH) as con:
        con.execute(
            """INSERT OR IGNORE INTO users
               (user_id, day, used, credits)
               VALUES (?, ?, 0, 0)""",
            (user_id, today),
        )

        row = con.execute(
            "SELECT day, used, credits FROM users WHERE user_id=?",
            (user_id,),
        ).fetchone()

        old_day, used, credits = row

        if old_day != today:
            used = 0
            con.execute(
                "UPDATE users SET day=?, used=0 WHERE user_id=?",
                (today, user_id),
            )

        return {"used": used, "credits": credits}


def can_generate(user_id):
    user = get_user(user_id)
    return user["used"] < FREE_PER_DAY or user["credits"] > 0


def charge_generation(user_id):
    today = date.today().isoformat()

    with sqlite3.connect(DB_PATH) as con:
        con.execute(
            """INSERT OR IGNORE INTO users
               (user_id, day, used, credits)
               VALUES (?, ?, 0, 0)""",
            (user_id, today),
        )

        row = con.execute(
            "SELECT day, used, credits FROM users WHERE user_id=?",
            (user_id,),
        ).fetchone()

        old_day, used, credits = row

        if old_day != today:
            used = 0

        if used < FREE_PER_DAY:
            used += 1
        elif credits > 0:
            credits -= 1
        else:
            return False

        con.execute(
            """UPDATE users
               SET day=?, used=?, credits=?
               WHERE user_id=?""",
            (today, used, credits, user_id),
        )

    return True


def add_credits(user_id, amount):
    get_user(user_id)

    with sqlite3.connect(DB_PATH) as con:
        con.execute(
            "UPDATE users SET credits=credits+? WHERE user_id=?",
            (amount, user_id),
        )


# ==================================================
# KEYBOARDS
# ==================================================

def main_keyboard():
    return InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(
                text="🎨 Создать фото",
                callback_data="generate",
            ),
        ],
        [
            InlineKeyboardButton(
                text="🪄 Редактировать фото",
                callback_data="edit",
            ),
        ],
        [
            InlineKeyboardButton(
                text="👤 Профиль",
                callback_data="profile",
            ),
            InlineKeyboardButton(
                text="⭐ Купить генерации",
                callback_data="shop",
            ),
        ],
    ])


def back_keyboard():
    return InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(
                text="⬅️ Главное меню",
                callback_data="home",
            )
        ]
    ])


# ==================================================
# STATES
# ==================================================

class PhotoStates(StatesGroup):
    waiting_prompt = State()
    waiting_photo = State()
    waiting_edit_instruction = State()


# ==================================================
# IMAGE GENERATION
# ==================================================

def generate_image(prompt):
    if client is None:
        raise RuntimeError("HF_TOKEN is not configured")

    enhanced_prompt = (
        "Create a highly photorealistic professional photograph. "
        "Natural skin texture, realistic human anatomy, accurate hands, "
        "natural lighting, physically realistic shadows, detailed materials, "
        "professional camera photography, realistic colors, sharp focus. "
        "Avoid plastic skin, artificial faces, cartoon style, illustration, "
        "excessive saturation, distorted fingers and blurry details. "
        "Image description: " + prompt
    )

    image = client.text_to_image(
        prompt=enhanced_prompt,
        model="black-forest-labs/FLUX.1-dev",
    )

    output = BytesIO()
    image.convert("RGB").save(
        output,
        format="JPEG",
        quality=95,
        optimize=True,
    )
    return output.getvalue()


def edit_image(photo_bytes, instruction):
    if client is None:
        raise RuntimeError("HF_TOKEN is not configured")

    source = Image.open(BytesIO(photo_bytes)).convert("RGB")

    edit_prompt = (
        "Edit this existing photograph according to the user's instruction: "
        + instruction
        + ". Produce a photorealistic result with natural lighting, realistic "
        "textures and natural colors. Preserve the person's identity and "
        "facial features whenever possible. Do not make unrelated changes."
    )

    image = client.image_to_image(
        image=source,
        prompt=edit_prompt,
        model="black-forest-labs/FLUX.1-Kontext-dev",
    )

    output = BytesIO()
    image.convert("RGB").save(
        output,
        format="JPEG",
        quality=95,
        optimize=True,
    )
    return output.getvalue()


# ==================================================
# START / MENU
# ==================================================

@dp.message(CommandStart())
async def start_handler(message: Message, state: FSMContext):
    await state.clear()
    get_user(message.from_user.id)

    await message.answer(
        "📸 <b>MarketPhoto AI</b>\n\n"
        "Создавай реалистичные фотографии или редактируй свои.\n\n"
        f"🎁 Бесплатно: {FREE_PER_DAY} генераций в день.\n"
        "🪄 Для редактирования отправь фотографию и опиши изменения.",
        reply_markup=main_keyboard(),
        parse_mode="HTML",
    )


@dp.message(Command("cancel"))
async def cancel_handler(message: Message, state: FSMContext):
    await state.clear()
    await message.answer(
        "Действие отменено.",
        reply_markup=main_keyboard(),
    )


@dp.callback_query(F.data == "home")
async def home_handler(callback: CallbackQuery, state: FSMContext):
    await state.clear()
    await callback.message.answer(
        "Главное меню MarketPhoto AI:",
        reply_markup=main_keyboard(),
    )
    await callback.answer()


# ==================================================
# PROFILE
# ==================================================

@dp.callback_query(F.data == "profile")
async def profile_handler(callback: CallbackQuery):
    user = get_user(callback.from_user.id)
    remaining = max(0, FREE_PER_DAY - user["used"])

    await callback.message.answer(
        "👤 <b>Твой профиль</b>\n\n"
        f"🎁 Бесплатных генераций осталось сегодня: {remaining}\n"
        f"⭐ Дополнительных генераций: {user['credits']}",
        parse_mode="HTML",
        reply_markup=back_keyboard(),
    )
    await callback.answer()


# ==================================================
# TEXT-TO-IMAGE
# ==================================================

@dp.callback_query(F.data == "generate")
async def begin_generation(callback: CallbackQuery, state: FSMContext):
    await state.set_state(PhotoStates.waiting_prompt)

    await callback.message.answer(
        "🎨 Опиши фотографию, которую хочешь создать.\n\n"
        "Например: реалистичный портрет молодого мужчины "
        "на улице вечером, естественное освещение, "
        "профессиональная фотосъёмка.\n\n"
        "Для отмены напиши /cancel."
    )
    await callback.answer()


@dp.message(PhotoStates.waiting_prompt, F.text)
async def generation_prompt_handler(
    message: Message,
    state: FSMContext,
):
    prompt = message.text.strip()

    if len(prompt) < 3:
        await message.answer("Напиши более подробное описание.")
        return

    if len(prompt) > 1500:
        await message.answer("Описание слишком длинное. Максимум 1500 символов.")
        return

    if not can_generate(message.from_user.id):
        await state.clear()
        await message.answer(
            "🎁 Бесплатный лимит закончился.\n"
            "Можно дождаться следующего дня или купить генерации.",
            reply_markup=main_keyboard(),
        )
        return

    if client is None:
        await state.clear()
        await message.answer(
            "Генератор не настроен: добавь HF_TOKEN в Render Environment."
        )
        return

    status = await message.answer("🎨 Создаю реалистичное фото. Подожди...")

    try:
        image_bytes = await asyncio.to_thread(generate_image, prompt)

        # Charge only after the model successfully returns an image.
        if not charge_generation(message.from_user.id):
            await message.answer("Лимит генераций закончился. Попробуй позже.")
            return

        await message.answer_photo(
            photo=BufferedInputFile(
                image_bytes,
                filename="marketphoto.jpg",
            ),
            caption="✨ Готово! Вот твоё фото.",
        )

    except Exception:
        logger.exception("Image generation failed")
        await message.answer(
            "Не удалось создать фото.\n"
            "Возможные причины: лимит провайдера, недоступная модель "
            "или проблема с токеном. Попробуй позже."
        )

    finally:
        await state.clear()
        try:
            await status.delete()
        except Exception:
            pass


# ==================================================
# IMAGE EDITING
# ==================================================

@dp.callback_query(F.data == "edit")
async def begin_editing(callback: CallbackQuery, state: FSMContext):
    await state.set_state(PhotoStates.waiting_photo)

    await callback.message.answer(
        "🪄 Отправь фотографию, которую хочешь изменить.\n\n"
        "После этого напиши, что именно нужно сделать.\n\n"
        "Например: «Замени фон на современную фотостудию, "
        "лицо и одежду оставь без изменений».\n\n"
        "Для отмены напиши /cancel."
    )
    await callback.answer()


@dp.message(PhotoStates.waiting_photo, F.photo)
async def receive_photo(message: Message, state: FSMContext):
    if not can_generate(message.from_user.id):
        await state.clear()
        await message.answer(
            "Бесплатный лимит закончился. Купи дополнительные генерации.",
            reply_markup=main_keyboard(),
        )
        return

    photo = message.photo[-1]
    file_info = await bot.get_file(photo.file_id)

    downloaded = BytesIO()
    await bot.download_file(
        file_info.file_path,
        destination=downloaded,
    )

    photo_bytes = downloaded.getvalue()

    await state.update_data(photo_bytes=photo_bytes)
    await state.set_state(PhotoStates.waiting_edit_instruction)

    await message.answer(
        "📷 Фото получено!\n\n"
        "Теперь напиши, что изменить на фото."
    )


@dp.message(PhotoStates.waiting_photo, F.text)
async def photo_required(message: Message):
    await message.answer(
        "Сначала отправь фотографию как изображение, а не как файл."
    )


@dp.message(PhotoStates.waiting_edit_instruction, F.text)
async def edit_instruction_handler(
    message: Message,
    state: FSMContext,
):
    instruction = message.text.strip()

    if len(instruction) < 3:
        await message.answer("Опиши, что именно нужно изменить.")
        return

    if len(instruction) > 1000:
        await message.answer("Инструкция слишком длинная. Максимум 1000 символов.")
        return

    if client is None:
        await state.clear()
        await message.answer("Добавь HF_TOKEN в Render Environment.")
        return

    data = await state.get_data()
    photo_bytes = data.get("photo_bytes")

    if not photo_bytes:
        await state.clear()
        await message.answer("Фото не найдено. Отправь его ещё раз.")
        return

    status = await message.answer("🪄 Редактирую фото...")

    try:
        result = await asyncio.to_thread(
            edit_image,
            photo_bytes,
            instruction,
        )

        if not charge_generation(message.from_user.id):
            await message.answer("Лимит генераций закончился.")
            return

        await message.answer_photo(
            photo=BufferedInputFile(
                result,
                filename="marketphoto_edit.jpg",
            ),
            caption="✨ Готово! Фото отредактировано.",
        )

    except Exception:
        logger.exception("Image editing failed")
        await message.answer(
            "Не удалось отредактировать фото.\n"
            "Проверь доступность модели редактирования и лимит провайдера."
        )

    finally:
        await state.clear()
        try:
            await status.delete()
        except Exception:
            pass


# ==================================================
# STARS SHOP
# ==================================================

@dp.callback_query(F.data == "shop")
async def shop_handler(callback: CallbackQuery):
    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(
                text=f"⭐ {PACK_PRICE_STARS} Stars — {PACK_CREDITS} генераций",
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
        "⭐ <b>Магазин генераций</b>\n\n"
        f"Пакет: {PACK_CREDITS} дополнительных генераций "
        f"за {PACK_PRICE_STARS} Telegram Stars.",
        reply_markup=keyboard,
        parse_mode="HTML",
    )
    await callback.answer()


@dp.callback_query(F.data == "buy_pack")
async def buy_pack_handler(callback: CallbackQuery):
    await bot.send_invoice(
        chat_id=callback.from_user.id,
        title="MarketPhoto AI — генерации",
        description=f"{PACK_CREDITS} дополнительных генераций фото",
        payload=f"marketphoto_pack_{callback.from_user.id}",
        currency="XTR",
        prices=[
            LabeledPrice(
                label=f"{PACK_CREDITS} генераций",
                amount=PACK_PRICE_STARS,
            )
        ],
    )
    await callback.answer()


@dp.pre_checkout_query()
async def pre_checkout_handler(query: PreCheckoutQuery):
    expected_payload = f"marketphoto_pack_{query.from_user.id}"

    if query.invoice_payload != expected_payload:
        await query.answer(
            ok=False,
            error_message="Неверный заказ. Попробуй ещё раз.",
        )
        return

    if query.currency != "XTR" or query.total_amount != PACK_PRICE_STARS:
        await query.answer(
            ok=False,
            error_message="Некорректная сумма заказа.",
        )
        return

    await query.answer(ok=True)


@dp.message(F.successful_payment)
async def successful_payment_handler(message: Message):
    payment = message.successful_payment

    expected_payload = f"marketphoto_pack_{message.from_user.id}"

    if payment.invoice_payload != expected_payload:
        logger.warning("Unexpected payment payload for user %s",
                       message.from_user.id)
        return

    if payment.currency != "XTR" or payment.total_amount != PACK_PRICE_STARS:
        logger.warning("Unexpected payment amount for user %s",
                       message.from_user.id)
        return

    # Telegram sends successful_payment only after successful payment.
    add_credits(message.from_user.id, PACK_CREDITS)

    await message.answer(
        f"✅ Оплата прошла успешно!\n"
        f"Добавлено генераций: {PACK_CREDITS}",
        reply_markup=main_keyboard(),
    )


# ==================================================
# ADMIN COMMAND
# ==================================================

@dp.message(Command("admin"))
async def admin_handler(message: Message):
    if message.from_user.id != ADMIN_ID:
        return

    await message.answer(
        "🛠 MarketPhoto AI\n"
        "Бот запущен.\n"
        f"Бесплатный лимит: {FREE_PER_DAY}/день.\n"
        f"Пакет: {PACK_CREDITS} генераций за {PACK_PRICE_STARS} Stars."
    )


# ==================================================
# RENDER HEALTH CHECK
# ==================================================

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

    logger.info("Health server listening on port %s", PORT)
    return runner


# ==================================================
# START BOT
# ==================================================

async def main():
    init_db()
    runner = await start_web_server()

    try:
        logger.info("Starting MarketPhoto AI polling")
        await dp.start_polling(
            bot,
            allowed_updates=dp.resolve_used_update_types(),
        )
    finally:
        await runner.cleanup()
        await bot.session.close()


if __name__ == "__main__":
    asyncio.run(main())
