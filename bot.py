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
GEMINI_API_KEY = "AQ.Ab8RN6KQpPZjjgQtQnCOwQnvAaSlmBEbKDsKh06iEB5jld1esQ"

bot = telebot.TeleBot(TOKEN)

try:
  client = genai.Client(api_key=GEMINI_API_KEY)
except Exception as e:
  client = None


@bot.message_handler(commands=["start"])
def send_welcome(message):
  bot.reply_to(
      message,
      "👋 **Привіт! Я MarketPhoto AI** 📸\n\n"
      "✨ Я твій професійний AI-генератор студійних фото товарів преміум-якості!\n\n"
      "💬 Напиши мені опис товару, і я згенерую фото каталожної якості! 🚀",
  )


@bot.message_handler(commands=["generate"])
def cmd_generate(message):
  bot.reply_to(
      message,
      "🎨 **Режим преміум-генерації!**\n\nОпишіть товар якомога детальніше! 📸✨",
  )


@bot.message_handler(func=lambda message: True)
def handle_message(message):
  bot.send_chat_action(message.chat.id, "upload_photo")

  if not client:
    bot.reply_to(message, "❌ Помилка ініціалізації клієнта AI.")
    return

  try:
    prompt_refiner = client.models.generate_content(
        model="gemini-2.5-flash",
        contents=(
            "Convert this product description into an ultra-detailed, professional"
            " commercial product photography prompt in English for a high-end AI"
            " image generator (Flux). Include details about professional studio"
            " softbox lighting, sharp focus, hyper-realistic textures, clean"
            " isolated pure seamless white background, 8k resolution,"
            " e-commerce catalog style, highly detailed: "
            f"{message.text}"
        ),
    )
    image_prompt = prompt_refiner.text.strip()

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
    bot.reply_to(
        message,
        f"❌ Сталася помилка при генерації:\n`{str(e)}`",
        parse_Mode="Markdown",
    )


def run_bot():
  print("Бот запущений!")
  bot.infinity_polling()


if __name__ == "__main__":
  import threading

  bot_thread = threading.Thread(target=run_bot)
  bot_thread.start()

  port = int(os.environ.get("PORT", 10000))
  app.run(host="0.0.0.0", port=port)
