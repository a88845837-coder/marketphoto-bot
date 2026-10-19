import io
import os
import urllib.parse
from flask import Flask, request
import requests
import telebot

TOKEN = "8616836203:AAHnB70MrjS6275-bH4ON8_NR1tVGb4gm0E"
bot = telebot.TeleBot(TOKEN)

app = Flask("")


def translate_to_english(ukr_text):
  lower_text = ukr_text.lower().strip()

  # Точний словник для предметів, щоб уникнути помилок і людей
  safety_map = {
      "гроші": "stack of hundred dollar bills",
      "грошей": "stack of hundred dollar bills",
      "доллари": "stack of hundred dollar bills",
      "долари": "stack of hundred dollar bills",
      "готівка": "stack of dollar banknotes",
      "валюта": "stack of dollar banknotes",
      "банан": "single fresh yellow banana",
      "банани": "fresh yellow bananas",
      "годинник": "luxury wristwatch",
      "кросівки": "pair of modern sneakers",
      "футболка": "blank white t-shirt",
      "кава": "ceramic cup of hot fresh espresso coffee",
      "чай": "ceramic cup of hot green tea",
      "чашка": "ceramic coffee mug",
  }

  for key, val in safety_map.items():
    if key in lower_text:
      return val

  try:
    url = f"https://api.mymemory.translated.net/get?q={urllib.parse.quote(ukr_text)}&langpair=uk|en"
    response = requests.get(url, timeout=5)
    if response.status_code == 200:
      data = response.json()
      translated = data.get("responseData", {}).get("translatedText", "")
      if translated and not translated.startswith("MYMEMORY"):
        return translated.strip()
  except Exception as e:
    print(f"Помилка перекладу: {e}")

  return ukr_text


def get_universal_prompt(user_text):
  translated_subject = translate_to_english(user_text)
  print(
      f"Оригінал: '{user_text}' | Переклад англійською:"
      f" '{translated_subject}'"
  )

  # Суворий комерційний промпт з жорстокою забороною людей і облич
  return (
      f"Commercial product photography of a single inanimate object:"
      f" {translated_subject}, isolated on a clean pure seamless white"
      " background, studio lighting, professional e-commerce catalog shot,"
      " macro details, ABSOLUTELY NO humans, NO faces, NO people, NO hands,"
      " product only"
  )


@app.route("/")
def home():
  return "MarketPhoto AI Bot is running 24/7! 🚀"


@app.route(f"/{TOKEN}", methods=["POST"])
def webhook():
  if request.headers.get("content-type") == "application/json":
    json_string = request.get_data().decode("utf-8")
    update = telebot.types.Update.de_json(json_string)
    bot.process_new_updates([update])
    return "", 200
  else:
    return "Invalid content type", 403


@bot.message_handler(commands=["start"])
def send_welcome(message):
  bot.reply_to(
      message,
      "👋 **Привіт! Я MarketPhoto AI** 📸\n\n"
      "✨ Надішли мені назву будь-якого товару (наприклад: *кава*, *банан*,"
      " *гроші*), і я згенерую лише предмет на білому фоні без людей! 🚀",
  )


@bot.message_handler(commands=["generate", "styles"])
def cmd_generate(message):
  text = message.text.replace("/generate", "").replace("/styles", "").strip()
  if text:
    generate_and_send_photo(message, text)
  else:
    bot.reply_to(
        message,
        "🎨 Напишіть опис товару після команди, наприклад: `/generate кава`"
        " або просто текстом!",
    )


@bot.message_handler(
    func=lambda message: message.text and not message.text.startswith("/")
)
def handle_message(message):
  generate_and_send_photo(message, message.text)


def generate_and_send_photo(message, user_text):
  print(f"Запит на генерацію: {user_text}")
  bot.send_chat_action(message.chat.id, "upload_photo")

  wait_msg = bot.reply_to(message, "⏳ **Малюю товар без людей...** Зачекайте 🎨")

  image_prompt = get_universal_prompt(user_text)
  print(f"Фінальний промпт: {image_prompt}")

  image_url = f"https://image.pollinations.ai/prompt/{urllib.parse.quote(image_prompt)}?width=1024&height=1024&seed=456&nologo=true"

  headers = {
      "User-Agent": (
          "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML,"
          " like Gecko) Chrome/120.0.0.0 Safari/537.36"
      )
  }

  try:
    img_response = requests.get(image_url, headers=headers, timeout=90)

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
              "✨ **Готово! Професійне фото товару (без людей)** 📸🔥"
          ),
          parse_mode="Markdown",
      )
    else:
      bot.edit_message_text(
          f"❌ Помилка генерації ({img_response.status_code}). Спробуйте ще"
          " раз!",
          message.chat.id,
          wait_msg.message_id,
      )
  except Exception as e:
    print(f"Помилка: {e}")
    try:
      bot.edit_message_text(
          f"❌ Помилка: `{str(e)}`",
          message.chat.id,
          wait_msg.message_id,
          parse_mode="Markdown",
      )
    except:
      bot.reply_to(message, f"❌ Помилка: `{str(e)}`")


if __name__ == "__main__":
  render_url = os.environ.get("RENDER_EXTERNAL_URL")
  if render_url:
    webhook_url = f"{render_url}/{TOKEN}"
    bot.remove_webhook()
    bot.set_webhook(url=webhook_url)
    print(f"Webhook встановлено: {webhook_url}")

  port = int(os.environ.get("PORT", 10000))
  app.run(host="0.0.0.0", port=port)
