import io
import os
import urllib.parse
from flask import Flask
import requests
import telebot

# Веб-сервер Flask для підтримки роботи на Render 24/7
app = Flask("")


@app.route("/")
def home():
  return "MarketPhoto AI Bot is running 24/7! 🚀"


# Ваш токен бота від BotFather
TOKEN = "8616836203:AAHnB70MrjS6275-bH4ON8_NR1tVGb4gm0E"

bot = telebot.TeleBot(TOKEN)


# Функція автоматичного перекладу українського опису на англійську для якісної генерації Flux
def translate_to_english(ukr_text):
  try:
    url = f"https://api.mymemory.translated.net/get?q={urllib.parse.quote(ukr_text)}&langpair=uk|en"
    response = requests.get(url, timeout=5)
    if response.status_code == 200:
      data = response.json()
      translated = data.get("responseData", {}).get("translatedText", "")
      if translated and not translated.startswith("MYMEMORY"):
        return translated
  except Exception as e:
    print(f"Помилка перекладу: {e}")
  return ukr_text


@bot.message_handler(commands=["start"])
def send_welcome(message):
  bot.reply_to(
      message,
      "👋 **Привіт! Я MarketPhoto AI** 📸\n\n"
      "✨ Я твій професійний AI-генератор студійних фото товарів преміум-якості!\n\n"
      "💬 Напиши мені опис товару (наприклад: *годинник* або *кросівки*), і я"
      " згенерую фото каталожної якості! 🚀",
  )


@bot.message_handler(commands=["generate", "styles"])
def cmd_generate(message):
  bot.reply_to(
      message,
      "🎨 **Режим преміум-генерації!**\n\nОпишіть товар якомога детальніше!"
      " 📸✨",
  )


@bot.message_handler(
    func=lambda message: message.text and not message.text.startswith("/")
)
def handle_message(message):
  print(f"Отримано текст для генерації: {message.text}")
  bot.send_chat_action(message.chat.id, "upload_photo")

  wait_msg = bot.reply_to(
      message, "⏳ **Генерую студійне фото...** Це може зайняти до хвилини 🎨"
  )

  user_text = message.text

  # Перекладаємо опис на англійську мову, щоб Flux розумів його ідеально
  translated_desc = translate_to_english(user_text)
  print(f"Перекладений промпт: {translated_desc}")

  # Формуємо професійний студійний промпт
  image_prompt = (
      f"Professional commercial product photography of {translated_desc}, studio"
      " softbox lighting, sharp focus, hyper-realistic textures, clean"
      " isolated pure seamless white background, 8k resolution, e-commerce"
      " catalog style"
  )

  try:
    encoded_prompt = urllib.parse.quote(image_prompt)
    image_url = f"https://image.pollinations.ai/prompt/{encoded_prompt}?width=1024&height=1024&model=flux&nologo=true"

    img_response = requests.get(image_url, timeout=120)

    if img_response.status_code == 200:
      photo_file = io.BytesIO(img_response.content)
      photo_file.name = "product_photo.jpg"

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
  print("Бот запускається без конфліктів...")
  bot.remove_webhook()
  bot.infinity_polling(skip_pending=True)


if __name__ == "__main__":
  import threading

  bot_thread = threading.Thread(target=run_bot)
  bot_thread.start()

  port = int(os.environ.get("PORT", 10000))
  app.run(host="0.0.0.0", port=port)
