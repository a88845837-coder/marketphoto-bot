import datetime
import io
import json
import os
import time
import urllib.parse
from flask import Flask, request
import requests
import telebot
from telebot import types

TOKEN = "8616836203:AAHnB70MrjS6275-bH4ON8_NR1tVGb4gm0E"
bot = telebot.TeleBot(TOKEN)

app = Flask("")

# Файл для збереження балансу користувачів
DATA_FILE = "users_db.json"


def load_db():
  if os.path.exists(DATA_FILE):
    try:
      with open(DATA_FILE, "r", encoding="utf-8") as f:
        return json.load(f)
    except:
      pass
  return {}


def save_db(data):
  with open(DATA_FILE, "w", encoding="utf-8") as f:
    json.dump(data, f, ensure_ascii=False, indent=4)


def get_user_data(user_id):
  db = load_db()
  str_id = str(user_id)
  today = str(datetime.date.today())

  if str_id not in db:
    db[str_id] = {"credits": 15, "last_date": today, "is_pro": False}
  else:
    # Щоденне оновлення 15 кредитів
    if db[str_id].get("last_date") != today:
      if not db[str_id].get("is_pro", False):
        db[str_id]["credits"] = 15
      db[str_id]["last_date"] = today

  save_db(db)
  return db[str_id]


def update_user_credits(user_id, amount):
  db = load_db()
  str_id = str(user_id)
  if str_id in db:
    db[str_id]["credits"] += amount
    save_db(db)


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
  if lower_text in smart_dictionary:
    return smart_dictionary[lower_text]
  for key, val in smart_dictionary.items():
    if key in lower_text:
      return val

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

  return f"high-end commercial product of {ukr_text}, premium advertising shot"


def get_universal_prompt(user_text, style="standard"):
  translated_subject = translate_to_english(user_text)

  if style == "luxury":
    bg = "dark luxury marble podium background, dramatic studio lighting"
  elif style == "minimal":
    bg = "isolated on a clean pure seamless white background, minimalist"
  else:
    bg = (
        "isolated on a clean pure seamless white background, studio softbox"
        " lighting"
    )

  return (
      f"Commercial advertising product photography of {translated_subject}, {bg},"
      " cinematic sharp focus, hyper-realistic textures, 8k resolution,"
      " high-end e-commerce catalog style, professional studio setup,"
      " ABSOLUTELY NO humans, NO faces, NO people, NO hands"
  )


@app.route("/")
def home():
  return "MarketPhoto AI Bot (Credits System) is running 24/7! 🚀"


@app.route(f"/{TOKEN}", methods=["POST"])
def webhook():
  if request.headers.get("content-type") == "application/json":
    json_string = request.get_data().decode("utf-8")
    update = telebot.types.Update.de_json(json_string)
    bot.process_new_updates([update])
    return "", 200
  else:
    return "Invalid content type", 403


# --- ВСІ КОМАНДИ МЕНЮ ---


@bot.message_handler(commands=["start"])
def cmd_start(message):
  user = get_user_data(message.from_user.id)
  bot.reply_to(
      message,
      "👋 **Привіт! Я MarketPhoto AI** 📸\n\n"
      "✨ Професійний генератор студійних фото товарів для маркетплейсів та"
      " соцмереж.\n\n"
      f"🎁 Ваш баланс: **{user['credits']} кредитів** (1 фото = 5 кредитів).\n"
      "Щодня вам нараховується 15 безкоштовних кредитів!\n\n"
      "📌 Використовуйте меню нижче або команди:\n"
      "• `/generate <товар>` — створити фото\n"
      "• `/profile` — ваш баланс\n"
      "• `/styles` — обрати стиль\n"
      "• `/buy` — купити кредити/PRO\n"
      "• `/help` — допомога",
      parse_mode="Markdown",
  )


@bot.message_handler(commands=["help"])
def cmd_help(message):
  bot.reply_to(
      message,
      "📖 **Довідка MarketPhoto AI** 📸\n\n"
      "1. Напишіть назву товару (наприклад: *кросівки*, *кава*, *гроші*).\n"
      "2. Кожна генерація коштує **5 кредитів**.\n"
      "3. Усі фото створюються у високій якості 8k на ідеальному фоні **без"
      " людей та облич**.\n\n"
      "Скористайтеся `/generate` або просто надішліть текст!",
      parse_mode="Markdown",
  )


@bot.message_handler(commands=["styles"])
def cmd_styles(message):
  markup = types.InlineKeyboardMarkup()
  markup.add(
      types.InlineKeyboardButton("⚪ Білий фон (Стандарт)", callback_data="style_standard")
  )
  markup.add(
      types.InlineKeyboardButton("⚫ Преміум Люкс (Темний мармур)", callback_data="style_luxury")
  )
  markup.add(
      types.InlineKeyboardButton("✨ Мінімалізм", callback_data="style_minimal")
  )

  bot.reply_to(
      message,
      "🎨 **Оберіть стиль або фон для вашого товару:**\n\nНатисніть на кнопку"
      " нижче, щоб встановити стиль для майбутніх генерацій:",
      reply_markup=markup,
      parse_mode="Markdown",
  )


@bot.callback_query_handler(func=lambda call: call.data.startswith("style_"))
def callback_styles(call):
  style_name = call.data.replace("style_", "")
  bot.answer_callback_query(
      call.id, f"✅ Стиль успішно змінено на: {style_name.upper()}"
  )
  bot.edit_message_text(
      f"✅ **Стиль зйомки встановлено:** `{style_name.upper()}`\n\nТепер просто"
      " надішліть назву товару, і він буде згенерований у цьому стилі! 📸",
      call.message.chat.id,
      call.message.message_id,
      parse_mode="Markdown",
  )


@bot.message_handler(commands=["profile"])
def cmd_profile(message):
  user = get_user_data(message.from_user.id)
  status = "PRO (Безліміт) ⭐" if user.get("is_pro") else "Стандартний 🟢"
  bot.reply_to(
      message,
      "👤 **Особистий кабінет**\n\n"
      f"🆔 ID: `{message.from_user.id}`\n"
      f"💎 Кредити: **{user['credits']}**\n"
      f"📦 Статус: **{status}**\n\n"
      "💡 1 фото = 5 кредитів. Кредити оновлюються щодня до 15!",
      parse_mode="Markdown",
  )


@bot.message_handler(commands=["buy"])
def cmd_buy(message):
  markup = types.InlineKeyboardMarkup()
  markup.add(
      types.InlineKeyboardButton("💳 +50 кредитів (55 грн)", callback_data="buy_50")
  )
  markup.add(
      types.InlineKeyboardButton("⭐ PRO Підписка на місяць (199 грн)", callback_data="buy_pro")
  )

  bot.reply_to(
      message,
      "🛒 **Магазин кредитів та підписок**\n\n"
      "Оберіть пакет, щоб отримати більше можливостей для створення фото:",
      reply_markup=markup,
      parse_mode="Markdown",
  )


@bot.callback_query_handler(func=lambda call: call.data.startswith("buy_"))
def callback_buy(call):
  if call.data == "buy_50":
    update_user_credits(call.from_user.id, 50)
    bot.answer_callback_query(call.id, "успішно додано 50 кредитів! 🎉", show_alert=True)
    bot.edit_message_text(
        "🎉 **Дякуємо за покупку!** На ваш баланс зараховано **+50 кредитів**.",
        call.message.chat.id,
        call.message.message_id,
        parse_mode="Markdown",
    )
  elif call.data == "buy_pro":
    db = load_db()
    str_id = str(call.from_user.id)
    if str_id in db:
      db[str_id]["is_pro"] = True
      db[str_id]["credits"] = 9999
      save_db(db)
    bot.answer_callback_query(call.id, "PRO активовано! 🚀", show_alert=True)
    bot.edit_message_text(
        "⭐ **Вітаємо! PRO-підписку активовано!** Тепер у вас безліміт генерацій.",
        call.message.chat.id,
        call.message.message_id,
        parse_mode="Markdown",
    )


@bot.message_handler(commands=["generate"])
def cmd_generate(message):
  text = message.text.replace("/generate", "").strip()
  if text:
    process_generation(message, text)
  else:
    bot.reply_to(
        message,
        "🎨 **Режим генерації!**\n\nНапишіть назву товару після команди,"
        " наприклад:\n`/generate кросівки`\n\nАбо просто надішліть текст у"
        " чат! 📸",
        parse_mode="Markdown",
    )


@bot.message_handler(
    func=lambda message: message.text and not message.text.startswith("/")
)
def handle_message(message):
  process_generation(message, message.text)


# --- ФУНКЦІЯ ГЕНЕРАЦІЇ З ПРОГРЕСОМ ТА СПИСАННЯМ КРЕДИТІВ ---


def process_generation(message, user_text):
  user_id = message.from_user.id
  user = get_user_data(user_id)

  # Перевірка кредитів (коштує 5 кредитів)
  if not user.get("is_pro", False) and user["credits"] < 5:
    bot.reply_to(
        message,
        "❌ **Недостатньо кредитів!**\n\nДля генерації фото потрібно 5 кредитів,"
        f" а у вас залишилося {user['credits']}.\nСкористайтеся командою `/buy`"
        " або зачекайте до завтра!",
        parse_mode="Markdown",
    )
    return

  print(f"Запуск генерації для: {user_text}")
  bot.send_chat_action(message.chat.id, "upload_photo")

  # Створюємо стартове повідомлення для прогресу
  wait_msg = bot.reply_to(
      message, "⏳ **Генерую фото... [25%]**\nАналіз та переклад запиту..."
  )

  time.sleep(0.8)
  try:
    bot.edit_message_text(
        "⏳ **Генерую фото... [50%]**\nНалаштування студійного світла та"
        " фону...",
        message.chat.id,
        wait_msg.message_id,
    )
  except:
    pass

  image_prompt = get_universal_prompt(user_text)
  time.sleep(0.8)

  try:
    bot.edit_message_text(
        "⏳ **Генерую фото... [75%]**\nРендеринг нейромережею (Flux / 8k)...",
        message.chat.id,
        wait_msg.message_id,
    )
  except:
    pass

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
      # Списуємо 5 кредитів, якщо не PRO
      if not user.get("is_pro", False):
        update_user_credits(user_id, -5)
        updated_user = get_user_data(user_id)
        credits_left = updated_user["credits"]
      else:
        credits_left = "Безліміт ⭐"

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
              "✨ **[100%] Готово! Професійне студійне фото створено** 📸🔥\n\n"
              f"💳 Списано кредитів: **5** | Залишок: **{credits_left}**"
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
