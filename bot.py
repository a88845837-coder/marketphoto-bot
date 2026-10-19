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

# 📌 Ваш Telegram ID для отримання заявок на оплату
ADMIN_ID = 5694967765

# 📌 Ваш номер картки для оплати
CARD_NUMBER = "4874 0700 7067 8378"
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


def set_user_pro(user_id, is_pro=True):
  db = load_db()
  str_id = str(user_id)
  if str_id in db:
    db[str_id]["is_pro"] = is_pro
    if is_pro:
      db[str_id]["credits"] = 9999
    save_db(db)


# Словник для швидкого перекладу
smart_dictionary = {
    "стакан води": "a crystal clear glass filled with pure fresh water",
    "вода": "a glass of pure water",
    "гроші": "stack of hundred dollar bills",
    "долари": "stack of hundred dollar bills",
    "банан": "single fresh yellow banana",
    "годинник": "luxury Swiss wristwatch",
    "кросівки": "stylish modern sports sneakers",
    "футболка": "blank premium white cotton t-shirt",
    "кава": "ceramic cup of hot fresh espresso coffee",
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
  return (
      f"Commercial advertising product photography of {translated_subject},"
      " isolated on a clean pure seamless white background, studio softbox"
      " lighting, cinematic sharp focus, hyper-realistic textures, 8k"
      " resolution, high-end e-commerce catalog style, ABSOLUTELY NO humans, NO"
      " faces, NO people, NO hands"
  )


@app.route("/")
def home():
  return "MarketPhoto AI Bot (Card Payment) is running 24/7! 🚀"


@app.route(f"/{TOKEN}", methods=["POST"])
def webhook():
  if request.headers.get("content-type") == "application/json":
    json_string = request.get_data().decode("utf-8")
    update = telebot.types.Update.de_json(json_string)
    bot.process_new_updates([update])
    return "", 200
  else:
    return "Invalid content type", 403


# --- КОМАНДИ МЕНЮ ---


@bot.message_handler(commands=["start"])
def cmd_start(message):
  user = get_user_data(message.from_user.id)
  bot.reply_to(
      message,
      "👋 **Привіт! Я MarketPhoto AI** 📸\n\n"
      f"🎁 Ваш баланс: **{user['credits']} кредитів**.\n"
      "📌 Використовуйте меню або команди:\n"
      "• `/generate <товар>` — створити фото\n"
      "• `/profile` — баланс\n"
      "• `/styles` — стилі\n"
      "• `/buy` — купити кредити / PRO",
      parse_mode="Markdown",
  )


@bot.message_handler(commands=["help"])
def cmd_help(message):
  bot.reply_to(
      message,
      "📖 **Довідка MarketPhoto AI**\n\nНапишіть назву товару, і бот створить"
      " студійне фото у високій якості 8k без людей. 1 генерація = 5 кредитів.",
      parse_mode="Markdown",
  )


@bot.message_handler(commands=["styles"])
def cmd_styles(message):
  markup = types.InlineKeyboardMarkup()
  markup.add(
      types.InlineKeyboardButton("⚪ Білий фон (Стандарт)", callback_data="style_standard")
  )
  markup.add(
      types.InlineKeyboardButton("⚫ Преміум Люкс", callback_data="style_luxury")
  )
  markup.add(
      types.InlineKeyboardButton("✨ Мінімалізм", callback_data="style_minimal")
  )
  bot.reply_to(
      message,
      "🎨 **Оберіть стиль зйомки:**",
      reply_markup=markup,
      parse_mode="Markdown",
  )


@bot.callback_query_handler(func=lambda call: call.data.startswith("style_"))
def callback_styles(call):
  style_name = call.data.replace("style_", "")
  bot.answer_callback_query(call.id, f"✅ Стиль змінено на: {style_name}")
  bot.edit_message_text(
      f"✅ **Стиль встановлено:** `{style_name.upper()}`",
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
      f"📦 Статус: **{status}**",
      parse_mode="Markdown",
  )


@bot.message_handler(commands=["buy"])
def cmd_buy(message):
  markup = types.InlineKeyboardMarkup()
  markup.add(
      types.InlineKeyboardButton("💳 Оплатити 50 кредитів (55 грн)", callback_data="pay_card_50")
  )
  markup.add(
      types.InlineKeyboardButton("⭐ Оплатити PRO (199 грн)", callback_data="pay_card_pro")
  )

  bot.reply_to(
      message,
      "🛒 **Магазин підписок та кредитів**\n\nОберіть пакет, який хочете придбати:",
      reply_markup=markup,
      parse_mode="Markdown",
  )


# --- ВИДАЧА РЕКВІЗИТІВ КАРТКИ ---


@bot.callback_query_handler(func=lambda call: call.data in ["pay_card_50", "pay_card_pro"])
def callback_card_payment(call):
  user_id = call.from_user.id
  if call.data == "pay_card_50":
    pack_title = "+50 кредитів (55 грн)"
    claim_data = "claim_50"
  else:
    pack_title = "PRO Підписку (199 грн)"
    claim_data = "claim_pro"

  markup = types.InlineKeyboardMarkup()
  markup.add(types.InlineKeyboardButton("✅ Я оплатив (-ла)", callback_data=claim_data))

  bot.edit_message_text(
      f"💳 **Реквізити для оплати ({pack_title}):**\n\n"
      f"Номер картки: `{CARD_NUMBER}`\n\n"
      f"📌 **ОБОВ'ЯЗКОВО** в коментарі до переказу вкажіть ваш ID: `{user_id}`\n\n"
      "Після здійснення переказу натисніть кнопку **«✅ Я оплатив (-ла)»** нижче:",
      call.message.chat.id,
      call.message.message_id,
      reply_markup=markup,
      parse_mode="Markdown",
  )


# --- ЗАПИТ НА ПІДТВЕРДЖЕННЯ ОПЛАТИ ---


@bot.callback_query_handler(func=lambda call: call.data in ["claim_50", "claim_pro"])
def callback_claim_payment(call):
  user_id = call.from_user.id
  username = f"@{call.from_user.username}" if call.from_user.username else "Без username"
  pack_name = "+50 Кредитів (55 грн)" if call.data == "claim_50" else "PRO Підписка (199 грн)"

  bot.answer_callback_query(
      call.id,
      "⏳ Запит надіслано адміністратору! Очікуйте перевірки.",
      show_alert=True,
  )
  bot.send_message(
      call.message.chat.id,
      "⏳ **Запит на перевірку надіслано!** Дякуємо! Адміністратор перевірить"
      " надходження коштів на картку і підтвердить платіж.",
      parse_mode="Markdown",
  )

  # Кнопки для вас (адміна)
  admin_markup = types.InlineKeyboardMarkup()
  admin_markup.add(
      types.InlineKeyboardButton(
          "✅ Підтвердити", callback_data=f"approve_{call.data}_{user_id}"
      ),
      types.InlineKeyboardButton(
          "❌ Відхилити", callback_data=f"decline_{user_id}"
      ),
  )

  try:
    bot.send_message(
        ADMIN_ID,
        "💰 **НОВА ЗАЯВКА НА ОПЛАТУ!**\n\n"
        f"👤 Користувач: {username}\n"
        f"🆔 ID: `{user_id}`\n"
        f"📦 Товар: **{pack_name}**\n\n"
        "Перевірте свій додаток банку і натисніть кнопку нижче:",
        reply_markup=admin_markup,
        parse_mode="Markdown",
    )
  except Exception as e:
    print(f"Помилка відправки адміну: {e}")


@bot.callback_query_handler(
    func=lambda call: call.data.startswith("approve_") or call.data.startswith("decline_")
)
def callback_admin_action(call):
  if call.from_user.id != ADMIN_ID:
    bot.answer_callback_query(call.id, "❌ Ви не адміністратор!", show_alert=True)
    return

  data = call.data.split("_")
  action = data[0]

  if action == "approve":
    pack_type = data[1] + "_" + data[2]
    target_user_id = int(data[3])

    if pack_type == "claim_50":
      update_user_credits(target_user_id, 50)
      msg_text = "🎉 **Вашу оплату підтверджено!** Нараховано +50 кредитів. 💎"
    else:
      set_user_pro(target_user_id, True)
      msg_text = "⭐ **Вашу оплату підтверджено!** PRO-підписку активовано. 🚀"

    bot.send_message(target_user_id, msg_text, parse_mode="Markdown")
    bot.edit_message_text(
        f"✅ **УСПІШНО ПІДТВЕРДЖЕНО** для ID `{target_user_id}`!",
        call.message.chat.id,
        call.message.message_id,
        parse_mode="Markdown",
    )

  elif action == "decline":
    target_user_id = int(data[1])
    bot.send_message(
        target_user_id,
        "❌ **Оплату не знайдено.** Перевірте, чи ви переказали кошти та вказали"
        " правильний ID у коментарі.",
        parse_mode="Markdown",
    )
    bot.edit_message_text(
        f"❌ **ВІДХИЛЕНО** для ID `{target_user_id}`.",
        call.message.chat.id,
        call.message.message_id,
        parse_mode="Markdown",
    )


# --- ГЕНЕРАЦІЯ ФОТО ---


@bot.message_handler(commands=["generate"])
def cmd_generate(message):
  text = message.text.replace("/generate", "").strip()
  if text:
    process_generation(message, text)
  else:
    bot.reply_to(
        message,
        "🎨 Напишіть назву товару після команди, наприклад: `/generate кросівки`",
        parse_mode="Markdown",
    )


@bot.message_handler(
    func=lambda message: message.text and not message.text.startswith("/")
)
def handle_message(message):
  process_generation(message, message.text)


def process_generation(message, user_text):
  user_id = message.from_user.id
  user = get_user_data(user_id)

  if not user.get("is_pro", False) and user["credits"] < 5:
    bot.reply_to(
        message,
        "❌ **Недостатньо кредитів!**\nДля генерації потрібно 5 кредитів."
        " Скористайтеся `/buy`, щоб придбати пакети.",
        parse_mode="Markdown",
    )
    return

  bot.send_chat_action(message.chat.id, "upload_photo")
  wait_msg = bot.reply_to(message, "⏳ **Генерую фото... [25%]**")
  time.sleep(0.5)
  try:
    bot.edit_message_text(
        "⏳ **Генерую фото... [50%]**", message.chat.id, wait_msg.message_id
    )
  except:
    pass

  image_prompt = get_universal_prompt(user_text)
  time.sleep(0.5)
  try:
    bot.edit_message_text(
        "⏳ **Генерую фото... [75%]**", message.chat.id, wait_msg.message_id
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
      if not user.get("is_pro", False):
        update_user_credits(user_id, -5)
        credits_left = get_user_data(user_id)["credits"]
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
              "✨ **[100%] Готово! Професійне фото створено** 📸🔥\n\n"
              f"💳 Списано: 5 кредитів | Залишок: **{credits_left}**"
          ),
          parse_mode="Markdown",
      )
    else:
      bot.edit_message_text(
          "❌ Помилка генерації. Спробуйте ще раз!",
          message.chat.id,
          wait_msg.message_id,
      )
  except Exception as e:
    print(f"Помилка: {e}")


if __name__ == "__main__":
  render_url = os.environ.get("RENDER_EXTERNAL_URL")
  if render_url:
    webhook_url = f"{render_url}/{TOKEN}"
    bot.remove_webhook()
    bot.set_webhook(url=webhook_url)
    print(f"Webhook встановлено: {webhook_url}")

  port = int(os.environ.get("PORT", 10000))
  app.run(host="0.0.0.0", port=port)
