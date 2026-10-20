
import os
import asyncio
import base64
import logging
from io import BytesIO

from aiohttp import web
from aiogram import Bot, Dispatcher, F
from aiogram.filters import CommandStart, Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import StatesGroup, State
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import Message, BufferedInputFile
from google import genai


# ==================================================
# CONFIG
# Render -> Environment
# ==================================================

TELEGRAM_TOKEN = (
    os.getenv("TELEGRAM_TOKEN")
    or os.getenv("BOT_TOKEN")
    or ""
).strip()

GOOGLE_API_KEY = (
    os.getenv("GOOGLE_API_KEY")
    or os.getenv("GEMINI_API_KEY")
    or ""
).strip()

PORT = int(os.getenv("PORT", "10000"))

# Актуальна модель генерації та редагування зображень
IMAGE_MODEL = os.getenv(
    "GOOGLE_IMAGE_MODEL",
    "gemini-nano-banana-2.1",
)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s:%(name)s:%(message)s",
)

logger = logging.getLogger("marketphoto")

if not TELEGRAM_TOKEN:
    raise RuntimeError(
        "TELEGRAM_TOKEN або BOT_TOKEN не заданий у Render Environment"
    )

if not GOOGLE_API_KEY:
    raise RuntimeError(
        "GOOGLE_API_KEY не заданий у Render Environment"
    )

# Явно використовуємо Gemini Developer API,
# а не Vertex AI / Enterprise.
client = genai.Client(
    api_key=GOOGLE_API_KEY,
    enterprise=False,
    vertexai=False,
)

bot = Bot(token=TELEGRAM_TOKEN)
dp = Dispatcher(storage=MemoryStorage())


# ==================================================
# STATES
# ==================================================

class EditStates(StatesGroup):
    waiting_instruction = State()


# ==================================================
# IMAGE API HELPERS
# ==================================================

def extract_image(response):
    """
    Отримує байти зображення з відповіді Gemini API.
    """

    output_image = getattr(response, "output_image", None)

    if not output_image or not getattr(output_image, "data", None):
        response_text = getattr(response, "output_text", None)
        logger.error(
            "API response did not contain output_image. Text=%r",
            response_text,
        )
        raise RuntimeError(
            "Google API не повернув зображення. Перевір Render Logs."
        )

    image_bytes = base64.b64decode(output_image.data)

    mime_type = (
        getattr(output_image, "mime_type", None)
        or "image/png"
    )

    extension = ".jpg" if "jpeg" in mime_type.lower() else ".png"

    return image_bytes, extension


def generate_image_sync(prompt):
    """
    Генерація нового фото з текстового опису.
    """

    enhanced_prompt = (
        "Create a highly photorealistic professional photograph. "
        "Natural skin texture, realistic anatomy, detailed eyes, "
        "physically accurate lighting and shadows, natural colors, "
        "realistic materials, professional camera photography, "
        "sharp focus and convincing details. Avoid plastic-looking "
        "skin, cartoon style, illustration and excessive saturation. "
        "User's scene: "
        + prompt
    )

    response = client.interactions.create(
        model=IMAGE_MODEL,
        input=enhanced_prompt,
    )

    return extract_image(response)


def edit_image_sync(photo_bytes, instruction):
    """
    Редагування надісланої користувачем фотографії.
    """

    image_base64 = base64.b64encode(photo_bytes).decode("ascii")

    edit_prompt = (
        "Edit the provided photograph according to this instruction: "
        + instruction
        + ". Keep the result photorealistic, with natural lighting, "
        "realistic textures and colors. Preserve the person's identity "
        "and facial features as much as possible. Avoid unrelated changes."
    )

    response = client.interactions.create(
        model=IMAGE_MODEL,
        input=[
            {
                "type": "text",
                "text": edit_prompt,
            },
            {
                "type": "image",
                "mime_type": "image/jpeg",
                "data": image_base64,
            },
        ],
    )

    return extract_image(response)


# ==================================================
# START / HELP / CANCEL
# ==================================================

@dp.message(CommandStart())
async def start_handler(
    message: Message,
    state: FSMContext,
):
    await state.clear()

    await message.answer(
        "📸 MarketPhoto AI\n\n"
        "🎨 Щоб створити фото, просто напиши опис.\n\n"
        "🪄 Щоб редагувати фото:\n"
        "1. Надішли фотографію.\n"
        "2. Напиши, що потрібно змінити.\n\n"
        "Приклад генерації: «Реалістичний портрет чоловіка "
        "на вулиці ввечері, професійна фотозйомка».\n\n"
        "Для скасування введи /cancel."
    )


@dp.message(Command("help"))
async def help_handler(message: Message):
    await message.answer(
        "🎨 Створити фото: надішли текстовий опис.\n\n"
        "🪄 Редагувати фото: надішли фото, а потім напиши інструкцію.\n\n"
        "Приклад редагування: «Заміни фон на пляж, але залиш "
        "обличчя без змін».\n\n"
        "Скасувати редагування: /cancel"
    )


@dp.message(Command("cancel"))
async def cancel_handler(
    message: Message,
    state: FSMContext,
):
    await state.clear()
    await message.answer(
        "Дію скасовано. Надішли новий опис фото або /start."
    )


# ==================================================
# PHOTO EDITING: RECEIVE ORIGINAL PHOTO
# ==================================================

@dp.message(F.photo)
async def receive_photo(
    message: Message,
    state: FSMContext,
):
    try:
        photo = message.photo[-1]
        file_info = await bot.get_file(photo.file_id)

        downloaded = BytesIO()

        await bot.download_file(
            file_info.file_path,
            destination=downloaded,
        )

        photo_bytes = downloaded.getvalue()

        if not photo_bytes:
            raise ValueError("Telegram повернув порожній файл")

        await state.update_data(
            photo_bytes=photo_bytes
        )

        await state.set_state(
            EditStates.waiting_instruction
        )

        await message.answer(
            "📷 Фото отримано!\n\n"
            "Тепер напиши, що потрібно змінити.\n\n"
            "Наприклад: «Зроби професійний фон фотостудії, "
            "обличчя не змінюй».\n\n"
            "Скасування: /cancel"
        )

    except Exception:
        logger.exception("Could not download uploaded photo")

        await state.clear()

        await message.answer(
            "Не вдалося завантажити фото. Спробуй надіслати його ще раз."
        )


# ==================================================
# PHOTO EDITING: PROCESS INSTRUCTION
# ==================================================

@dp.message(EditStates.waiting_instruction, F.text)
async def handle_edit_instruction(
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
            "Опис занадто довгий. Максимум 1500 символів."
        )
        return

    data = await state.get_data()
    photo_bytes = data.get("photo_bytes")

    if not photo_bytes:
        await state.clear()
        await message.answer(
            "Фото не знайдено. Надішли його ще раз."
        )
        return

    status = await message.answer(
        "🪄 Редагую фото..."
    )

    try:
        result_bytes, extension = await asyncio.to_thread(
            edit_image_sync,
            photo_bytes,
            instruction,
        )

        await message.answer_photo(
            photo=BufferedInputFile(
                result_bytes,
                filename="edited" + extension,
            ),
            caption="✨ Готово! Фото відредаговано.",
        )

    except Exception:
        logger.exception("Photo editing failed")

        await message.answer(
            "Не вдалося відредагувати фото.\n"
            "Перевір останню помилку в Render Logs."
        )

    finally:
        await state.clear()

        try:
            await status.delete()
        except Exception:
            pass


# ==================================================
# GENERATE IMAGE FROM TEXT
# ==================================================

@dp.message(F.text & ~F.text.startswith("/"))
async def handle_prompt(message: Message):
    prompt = message.text.strip()

    if len(prompt) < 3:
        await message.answer(
            "Напиши детальніший опис фотографії."
        )
        return

    if len(prompt) > 1500:
        await message.answer(
            "Опис занадто довгий. Максимум 1500 символів."
        )
        return

    status = await message.answer(
        "🎨 Створюю реалістичне фото..."
    )

    try:
        image_bytes, extension = await asyncio.to_thread(
            generate_image_sync,
            prompt,
        )

        await message.answer_photo(
            photo=BufferedInputFile(
                image_bytes,
                filename="generated" + extension,
            ),
            caption="✨ Ось результат!",
        )

    except Exception:
        logger.exception("Image generation failed")

        await message.answer(
            "Не вдалося створити фото.\n"
            "Можливі причини: помилка API-ключа, недоступна модель, "
            "обмеження квоти або помилка запиту.\n\n"
            "Адреса для перевірки: Render → Logs."
        )

    finally:
        try:
            await status.delete()
        except Exception:
            pass


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
        "Health server started on port %s",
        PORT,
    )

    return runner


# ==================================================
# START BOT
# ==================================================

async def main():
    runner = await start_web_server()

    try:
        logger.info(
            "Starting MarketPhoto AI using Gemini Developer API. Model=%s",
            IMAGE_MODEL,
        )

        await dp.start_polling(
            bot,
            allowed_updates=dp.resolve_used_update_types(),
        )

    finally:
        await runner.cleanup()
        await bot.session.close()
        client.close()


if __name__ == "__main__":
    asyncio.run(main())

