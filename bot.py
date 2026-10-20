import os
import asyncio
from aiogram import Bot, Dispatcher, types
from aiogram.filters import Command
from aiogram.types import BufferedInputFile
from google import genai
from google.genai import types as genai_types

# Берем ключи из переменных окружения
TELEGRAM_TOKEN = os.environ.get('TELEGRAM_TOKEN')
GOOGLE_API_KEY = os.environ.get('GOOGLE_API_KEY')

if not TELEGRAM_TOKEN or not GOOGLE_API_KEY:
    raise ValueError("Не установлены переменные окружения TELEGRAM_TOKEN или GOOGLE_API_KEY!")

# Инициализация
client = genai.Client(api_key=GOOGLE_API_KEY)
bot = Bot(token=TELEGRAM_TOKEN)
dp = Dispatcher()

@dp.message(Command("start"))
async def cmd_start(message: types.Message):
    await message.answer("Привет! Напиши мне текстовый промпт, и я сгенерирую изображение.")

@dp.message()
async def handle_prompt(message: types.Message):
    prompt = message.text
    if not prompt:
        return

    status_msg = await message.answer(f"🎨 Генерирую изображение по запросу: *{prompt}*...", parse_mode="Markdown")

    try:
        response = await asyncio.to_thread(
            client.models.generate_images,
            model='imagen-3.0-generate-002',
            prompt=prompt,
            config=genai_types.GenerateImagesConfig(
                number_of_images=1,
                output_mime_type='image/jpeg',
                aspect_ratio='1:1',
            )
        )

        if response and response.generated_images:
            image_bytes = response.generated_images[0].image.image_bytes
            photo = BufferedInputFile(image_bytes, filename="generated.jpg")
            
            await bot.send_photo(
                chat_id=message.chat.id, 
                photo=photo, 
                caption=f"✨ Промпт: {prompt}"
            )
            await bot.delete_message(chat_id=message.chat.id, message_id=status_msg.message_id)
        else:
            await bot.edit_message_text(chat_id=message.chat.id, message_id=status_msg.message_id, text="❌ Не удалось сгенерировать изображение.")

    except Exception as e:
        print(f"Ошибка: {e}")
        await bot.edit_message_text(chat_id=message.chat.id, message_id=status_msg.message_id, text=f"⚠️ Произошла ошибка: {e}")

async def main():
    await dp.start_polling(bot)

if __name__ == '__main__':
    asyncio.run(main())
