import io
import os
import urllib.parse
from flask import Flask, request
import requests
import telebot

TOKEN = "8616836203:AAHnB70MrjS6275-bH4ON8_NR1tVGb4gm0E"
bot = telebot.TeleBot(TOKEN)

app = Flask("")

# Точний словник для перекладу товарів
translations = {
    "годинник": "luxury wristwatch",
    "годинники": "luxury wristwatches",
    "кросівки": "stylish sports sneakers",
    "парфуми": "luxury perfume bottle",
    "сумка": "leather handbag",
    "телефон": "modern smartphone",
    "смартфон": "modern smartphone",
    "навушники": "wireless headphones",
    "окуляри": "stylish sunglasses",
    "каблучка": "elegant diamond ring",
    "кільце": "elegant diamond ring",
    "кава": "cup of fresh artisanal coffee",
    "книга": "hardcover book",
    "футболка": "cotton t-shirt",
    "куртка": "stylish winter jacket",
}


def get_english_prompt(user_text):
  lower_text = user_text.lower().strip()
  base_subject = ""
  for key, val in translations.items():
    if key in lower_text:
      base_subject = val
      break
  if not base_subject:
    base_subject = f"commercial product of {user_text}"

  return (
      f"Professional commercial e-commerce product photography of"
      f" {base_subject}, studio softbox lighting, sharp focus,"
      " hyper-realistic textures, isolated on a clean pure seamless white"
      " background, 8k resolution, catalogue style, advertising shot, highly"
      " detailed, NO humans, NO faces, NO people"
  )


@app.route("/")
def home():
  return "MarketPhoto AI Bot is running via Webhook 24/7! 🚀"


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
      "✨ Я твій професійний AI-генератор студійних фото товарів преміум-якості!\n\n"
      "💬 Напиши назву товару (наприклад: *годинник*, *кросівки*) або"
      " скористайся командою `/generate годинник`! 🚀",
  )


@bot.message_handler(commands=["generate", "styles"])
def cmd_generate(message):
  text = message.text.replace("/generate", "").replace("/styles", "").strip()
  if text:
    generate_and_send_photo(message, text)
  else:
    bot.reply_to(
        message,
        "🎨 **Режим преміум-генерації!**\n\nНапишіть назву товару після команди,"
        " наприклад: `/generate годинник` або просто надішліть її текстом!"
        " 📸✨",
    )


@bot.message_handler(
    func=lambda message: message.text and not message.text.startswith("/")
)
def handle_message(message):
  generate_and_send_photo(message, message.text)


def generate_and_send_photo(message, user_text):
  print(f"Генерація для: {user_text}")
  bot.send_chat_action(message.chat.id, "upload_photo")

  wait_msg = bot.reply_to(
      message, "⏳ **Генерую студійне фото товару...** Це може зайняти хвилину 🎨"
  )

  image_prompt = get_english_prompt(user_text)
  print(f"Фінальний промпт: {image_prompt}")

  try:
    encoded_prompt = urllib.parse.quote(image_prompt)
    image_url = f"https://image.pollinations.ai/prompt/{encoded_prompt}?width=1024&height=1024&model=flux&nologo=true"

    # Додаємо заголовки браузера, щоб уникнути блокування запиту
    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML,"
            " like Gecko) Chrome/120.0.0.0 Safari/537.36"
        )
    }
    img_response = requests.get(image_url, headers=headers, timeout=120)
    print(f"Статус відповіді від генератора: {img_response.status_code}")

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
      print(f"Помилка генератора: {img_response.text}")
      bot.edit_message_text(
          (
              "❌ Не вдалося отримати фото (код помилки:"
              f" {img_response.status_code}). Спробуйте ще раз!"
          ),
          message.chat.id,
          wait_msg.message_id,
      )

  except Exception as e:
    print(f"Помилка при генерації: {e}")
    try:
      bot.edit_message_text(
          f"❌ Сталася помилка при генерації:\n`{str(e)}`",
          message.chat.id,
          wait_msg.message_id,
          parse_mode="Markdown",
      )
    except:
      bot.reply_to(message, f"❌ Помилка: `{str(e)}`", parse_mode="Markdown")


if __name__ == "__main__":
  render_url = os.environ.get("RENDER_EXTERNAL_URL")
  if render_url:
    webhook_url = f"{render_url}/{TOKEN}"
    bot.remove_webhook()
    bot.set_webhook(url=webhook_url)
    print(f"Webhook успішно встановлено на: {webhook_url}")

  port = int(os.environ.get("PORT", 10000))
  app.run(host="0.0.0.0", port=port)
