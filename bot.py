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


# Обробляємо лише звичайні повідомлення (які НЕ починаються з "/")
@bot.message_handler(
    func=lambda message: message.text and not message.text.startswith("/")
)
def handle_message(message):
  print(f"Отримано текст для генерації: {message.text}")
  bot.send_chat_action(message.chat.id, "upload_photo")

  # Відправляємо сповіщення про процес очікування
  wait_msg = bot.reply_to(
      message, "⏳ **Генерую студійне фото...** Це може зайняти до хвилини 🎨"
  )

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

    # Збільшуємо таймаут до 90 секунд, щоб уникнути помилки тайм-ауту
    img_response = requests.get(image_url, timeout=90)

    if img_response.status_code == 200:
      photo_file = io.BytesIO(img_response.content)
      photo_file.name = "product_photo.jpg"

      # Видаляємо текстове повідомлення "Генерую..." перед відправкою фото
      try:
        bot.delete_message(message.chat.id, wait_msg.message_id)
      except:
        pass

      bot.send_photo(
          message.chat.id,
          photo_file,
          caption=(
              "✨ **Готово! Професійне студійне фото товару створено** 📸🔥"
          ),
          parse_mode="Markdown",
      )
    else:
      bot.edit_message_text(
          "❌ Не вдалося отримати фото. Спробуйте ще раз!",
          message.chat.id,
          wait_msg.message_id,
      )

  except Exception as e:
    print(f"Помилка при відправці фото: {e}")
    try:
      bot.edit_message_text(
          f"❌ Сталася помилка при генерації:\n`{str(e)}`",
          message.chat.id,
          wait_msg.message_id,
          parse_mode="Markdown",
      )
    except:
      bot.reply_to(message, f"❌ Помилка: `{str(e)}`", parse_mode="Markdown")


def run_bot():
  print("Бот запускається та підключається до Telegram...")
  bot.infinity_polling(skip_pending=True)


if __name__ == "__main__":
  import threading

  bot_thread = threading.Thread(target=run_bot)
  bot_thread.start()

  port = int(os.environ.get("PORT", 10000))
  app.run(host="0.0.0.0", port=port)
