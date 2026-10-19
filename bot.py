import io
import os
import urllib.parse
from flask import Flask
import requests
import telebot
from google import genai

# Веб-сервер Flask для підтримки роботи на Render 24/7
app = Flask("")


@app.route("/")
def home():
  return "MarketPhoto AI Bot is running 24/7! 🚀"


TOKEN = "8616836203:AAGkRFDiPqI2QUahx45iDBm7NyqbPwbv1wA"
GEMINI_API_KEY = "AQ.Ab8RN6JHrxiEhFM_nbVGBTUmBEzIdlKHdp0f3G5rCP_roFJc2A"

bot = telebot.TeleBot(TOKEN)

try:
  client = genai.Client(api_key=GEMINI_API_KEY)
except Exception as e:
  print(f"Помилка ініціалізації genai: {e}")
  client = None


@bot.message_handler(commands=["start"])
def send_welcome(message):
  print(f"Отримано команду /start від користувача {message.chat.id}")
  bot.reply_to(
      message,
      "👋 **Привіт! Я MarketPhoto AI** 📸\n\n"
      "✨ Я твій професійний AI-генератор студійних фото товарів преміум-якості!\n\n"
      "💬 Напиши мені опис товару, і я згенерую фото каталожної якості! 🚀",
  )


@bot.message_handler(commands=["generate"])
def cmd_generate(message):
  print(f"Отримано команду /generate від користувача {message.chat.id}")
  bot.reply_to(
      message,
      "🎨 **Режим преміум-генерації!**\n\nОпишіть товар якомога детальніше! 📸✨",
  )


@bot.message_handler(func=lambda message: True)
def handle_message(message):
  print(f"Отримано текст для генерації: {message.text}")
  bot.send_chat_action(message.chat.id, "upload_photo")
  user_text = message.text
  image_prompt = user_text  # Запасний варіант

  if client:
    try:
      prompt_refiner = client.models.generate_content(
          model="gemini-2.5-flash",
          contents=(
              "Convert this product description into an ultra-detailed,"
              " professional commercial product photography prompt in English"
              " for a high-end AI image generator (Flux). Include details about"
              " professional studio softbox lighting, sharp focus,"
              " hyper-realistic textures, clean isolated pure seamless white"
              " background, 8k resolution, e-commerce catalog style,"
              f" highly detailed: {user_text}"
          ),
      )
      if prompt_refiner and prompt_refiner.text:
        image_prompt = prompt_refiner.text.strip()
    except Exception as e:
      print(f"Попередження Gemini (продовжуємо генерацію): {e}")

  try:
    encoded_prompt = urllib.parse.quote(image_prompt)
    image_url = f"https://image.pollinations.ai/prompt/{encoded_prompt}?width=1024&height=1024&model=flux&nologo=true"

    img_response = requests.get(image_url, timeout=45)

    if img_response.status_code == 200:
      photo_file = io.BytesIO(img_response.content)
      photo_file.name = "product_photo.jpg"

      bot.send_photo(
          message.chat.id,
          photo_file,
          caption=(
              "✨ **Готово! Професійне студійне фото товару створено** 📸🔥"
          ),
          parse_mode="Markdown",
      )
    else:
      bot.reply_to(
          message, "❌ Не вдалося отримати фото. Спробуйте ще раз!"
      )

  except Exception as e:
    print(f"Помилка при відправці фото: {e}")
    bot.reply_to(
        message,
        f"❌ Сталася помилка при генерації:\n`{str(e)}`",
        parse_mode="Markdown",
    )


def run_bot():
  print("Бот запускається та підключається до Telegram...")
  bot.infinity_polling(skip_pending=True)


if __name__ == "__main__":
  import threading

  bot_thread = threading.Thread(target=run_bot)
  bot_thread.start()

  port = int(os.environ.get("PORT", 10000))
  app.run(host="0.0.0.0", port=port)
