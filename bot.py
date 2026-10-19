import io
import os
import urllib.parse
from flask import Flask, request
import requests
import telebot

TOKEN = "8616836203:AAHnB70MrjS6275-bH4ON8_NR1tVGb4gm0E"
bot = telebot.TeleBot(TOKEN)

app = Flask("")

# Розширений словник для миттєвого й ідеального перекладу
smart_dictionary = {
    "стакан води": "a crystal clear glass filled with pure fresh water",
    "вода": "a glass of pure water",
    "стакан": "transparent empty drinking glass",
    "склянка": "transparent drinking glass",
    "гроші": "stack of hundred dollar bills",
    "грошей": "stack of hundred dollar bills",
    "доллари": "stack of hundred dollar bills",
    "долари": "stack of hundred dollar bills",
    "готівка": "neat stack of dollar banknotes",
    "банан": "single fresh yellow banana",
    "банани": "bunch of fresh yellow bananas",
    "годинник": "luxury Swiss wristwatch",
    "кросівки": "stylish modern sports sneakers",
    "футболка": "blank premium white cotton t-shirt",
    "кава": "ceramic cup of hot fresh espresso coffee",
    "чай": "ceramic cup of hot green tea",
    "чашка": "ceramic coffee mug",
    "парфуми": "luxury perfume bottle",
    "телефон": "modern flagship smartphone",
    "смартфон": "modern flagship smartphone",
    "навушники": "wireless over-ear headphones",
    "окуляри": "stylish sunglasses",
    "сумка": "leather luxury handbag",
}


def translate_to_english(ukr_text):
  lower_text = ukr_text.lower().strip()

  # 1. Шукаємо точний збіг у словнику
  if lower_text in smart_dictionary:
    return smart_dictionary[lower_text]

  # Шукаємо частковий збіг
  for key, val in smart_dictionary.items():
    if key in lower_text:
      return val

  # 2. Якщо немає в словнику — робимо запит до перекладача
  try:
    url = f"https://api.mymemory.translated.net/get?q={urllib.parse.quote(ukr_text)}&langpair=uk|en"
    response = requests.get(url, timeout=8)
    if response.status_code == 200:
      data = response.json()
      translated = data.get("responseData", {}).get("translatedText", "")
      if (
          translated
          and not translated.startswith("MYMEMORY")
          and not any(ord(c) > 127 for c in translated)
      ):
        return translated.strip()
  except Exception as e:
    print(f"Помилка перекладу: {e}")

  # 3. Захисний фолбек, якщо переклад не вдався
  return (
      f"high-end commercial product of {ukr_text}, premium advertising shot"
  )


def get_universal_prompt(user_text):
  translated_subject = translate_to_english(user_text)
  print(
      f"Оригінал: '{user_text}' | Переклад англійською:"
      f" '{translated_subject}'"
  )

  # Професійний рекламний промпт для ідеальної якості
  return (
      f"Commercial advertising product photography of {translated_subject},"
      " studio softbox lighting, cinematic sharp focus, hyper-realistic"
      " textures, isolated on a clean pure seamless white background, 8k"
      " resolution, high-end e-commerce catalog style, professional studio"
      " setup, ABSOLUTELY NO humans, NO faces, NO people, NO hands"
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
      "✨ Я створюю ідеальні професійні фото будь-яких товарів.\n\n"
      "💬 Надішли назву товару (наприклад: *стакан води*, *кава*, *кросівки*),"
      " і я зроблю бездоганне фото! 🚀",
  )


@bot.message_handler(commands=["generate", "styles"])
def cmd_generate(message):
  text = message.text.replace("/generate", "").replace("/styles", "").strip()
  if text:
    generate_and_send_photo(message, text)
  else:
    bot.reply_to(
        message,
        "🎨 Напишіть назву товару після команди, наприклад: `/generate стакан"
        " води` або просто текстом!",
    )


@bot.message_handler(
    func=lambda message: message.text and not message.text.startswith("/")
)
def handle_message(message):
  generate_and_send_photo(message, message.text)


def generate_and_send_photo(message, user_text):
  print(f"Запит на генерацію: {user_text}")
  bot.send_chat_action(message.chat.id, "upload_photo")

  wait_msg = bot.reply_to(
      message, "⏳ **Створюю ідеальне фото товару...** Зачекайте 🎨"
  )

  image_prompt = get_universal_prompt(user_text)
  print(f"Фінальний промпт: {image_prompt}")

  image_url = f"https://image.pollinations.ai/prompt/{urllib.parse.quote(image_prompt)}?width=1024&height=1024&nologo=true"

  headers = {
      "User-Agent": (
          "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML,"
          " like Gecko) Chrome/120.0.0.0 Safari/537.36"
      )
  }

  try:
    img_response = requests.get(image_url, headers=headers, timeout=120)

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
              "✨ **Готово! Професійне студійне фото створено** 📸🔥"
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
