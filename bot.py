import asyncio
import datetime
import io
import json
import os
import time
import urllib.parse
from aiohttp import web
from aiogram import Bot, Dispatcher, F, types
from aiogram.filters import Command, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    KeyboardButton,
    LabeledPrice,
    Message,
    PreCheckoutQuery,
    ReplyKeyboardMarkup,
)
from PIL import Image

# ================= Налаштування =================
BOT_TOKEN = "8616836203:AAGImAKF-vFvJOQ0Qo1VSQ8mZFoPJ8nkzLs"
ADMIN_ID = 5694967765

bot = Bot(token=BOT_TOKEN)
dp = Dispatcher()

DB_FILE = "users_db.json"
DAILY_FREE_LIMIT = 5


# ================= Стани для генерації =================
class PhotoGenerationState(StatesGroup):
    waiting_for_text = State()
    waiting_for_photo = State()


# ================= Робота з базою даних =================
def load_db():
    if not os.path.exists(DB_FILE):
        return {}
    try:
        with open(DB_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except json.JSONDecodeError:
        return {}


def save_db(data):
    with open(DB_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=4)


def get_user_data(user_id: int):
    db = load_db()
    user_id_str = str(user_id)
    today_str = datetime.date.today().isoformat()

    if user_id_str not in db:
        db[user_id_str] = {
            "credits": 0,
            "stars_spent": 0,
            "sub_until": 0,
            "joined_at": int(time.time()),
            "daily_count": 0,
            "last_date": today_str,
        }
        save_db(db)
    else:
        user_data = db[user_id_str]
        if user_data.get("last_date") != today_str:
            user_data["daily_count"] = 0
            user_data["last_date"] = today_str
            save_db(db)

    return db[user_id_str]


def update_user_purchase(
    user_id: int, credits_to_add: int, stars_spent: int = 0, days_sub: int = 0
):
    db = load_db()
    user_id_str = str(user_id)
    today_str = datetime.date.today().isoformat()

    if user_id_str not in db:
        db[user_id_str] = {
            "credits": 0,
            "stars_spent": 0,
            "sub_until": 0,
            "joined_at": int(time.time()),
            "daily_count": 0,
            "last_date": today_str,
        }

    db[user_id_str]["credits"] += credits_to_add
    db[user_id_str]["stars_spent"] = (
        db[user_id_str].get("stars_spent", 0) + stars_spent
    )

    if days_sub > 0:
        current_time = time.time()
        active_until = db[user_id_str].get("sub_until", 0)
        base_time = max(current_time, active_until)
        db[user_id_str]["sub_until"] = base_time + (days_sub * 24 * 60 * 60)

    save_db(db)
    return db[user_id_str]


def use_generation(user_id: int):
    db = load_db()
    user_id_str = str(user_id)
    today_str = datetime.date.today().isoformat()

    if user_id_str not in db:
        get_user_data(user_id)
        db = load_db()

    user_data = db[user_id_str]

    if user_data.get("last_date") != today_str:
        user_data["daily_count"] = 0
        user_data["last_date"] = today_str

    if user_id == ADMIN_ID:
        return True, "admin", user_data["credits"]

    if user_data["daily_count"] < DAILY_FREE_LIMIT:
        user_data["daily_count"] += 1
        save_db(db)
        return True, "free", DAILY_FREE_LIMIT - user_data["daily_count"]

    if user_data["credits"] > 0:
        user_data["credits"] -= 1
        save_db(db)
        return True, "credit", user_data["credits"]

    return False, "none", 0


# ================= Інтелектуальний переклад запиту =================
def enhance_prompt(user_prompt: str, style_name: str) -> str:
    prompt_clean = user_prompt.strip()

    dictionary = {
        "бетмен": "Batman superhero dark knight, cinematic lighting, highly detailed",
        "людина-павук": "Spider-Man superhero dynamic action pose, highly detailed",
        "людина павук": "Spider-Man superhero dynamic action pose, highly detailed",
        "рік і морті": "Rick and Morty cartoon style, 2D animation",
        "автомобіль бмв": "luxury BMW car, cinematic lighting",
        "бмв": "luxury BMW car",
        "кіберпанк": "cyberpunk style, neon lights, futuristic",
        "кіт": "cute cat",
        "собака": "cute dog",
        "космос": "outer space, nebula, stars",
    }

    prompt_lower = prompt_clean.lower()
    translated_text = prompt_clean

    for ukr, eng in dictionary.items():
        if ukr in prompt_lower:
            translated_text = prompt_lower.replace(ukr, eng)
            break

    if style_name == "Стильна Аватарка":
        extra_style = "high quality, detailed portrait, masterpiece, centered composition"
    elif style_name == "Заставка / Шпалери":
        extra_style = "epic wallpaper, ultra detailed, 8k resolution, cinematic lighting"
    else:
        extra_style = "digital art illustration, artistic style, masterpiece"

    final_prompt = f"{translated_text}, {extra_style}"
    return final_prompt


# ================= Клавіатури =================
def main_keyboard():
    kb = [
        [
            KeyboardButton(text="👤 Профіль"),
            KeyboardButton(text="🛒 Магазин зірок"),
        ],
        [
            KeyboardButton(text="⚙️ Послуги / Функції"),
            KeyboardButton(text="🎨 Створити фото"),
        ],
        [
            KeyboardButton(text="ℹ️ Допомога"),
        ],
    ]
    return ReplyKeyboardMarkup(keyboard=kb, resize_keyboard=True)


def generation_mode_keyboard():
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="✨ Генерація з нуля (Текст ➔ Фото)", callback_data="gen_text"
                )
            ],
            [
                InlineKeyboardButton(
                    text="📸 Обробка власного фото", callback_data="gen_photo"
                )
            ],
        ]
    )


def photo_type_keyboard():
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="👤 Стильна Аватарка", callback_data="type_avatar"
                )
            ],
            [
                InlineKeyboardButton(
                    text="🖼️ Заставка / Шпалери", callback_data="type_wallpaper"
                )
            ],
            [
                InlineKeyboardButton(
                    text="✨ Художнє фото", callback_data="type_art"
                )
            ],
        ]
    )


def shop_keyboard():
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="💎 50 кредитів — 25 Stars",
                    callback_data="buy_50_credits",
                )
            ],
            [
                InlineKeyboardButton(
                    text="💎 100 кредитів — 45 Stars",
                    callback_data="buy_100_credits",
                )
            ],
            [
                InlineKeyboardButton(
                    text="⭐ VIP підписка (30 днів) — 100 Stars",
                    callback_data="buy_vip_sub",
                )
            ],
        ]
    )


def user_result_keyboard():
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="⚠️ Брак (повернути спробу)",
                    callback_data="user_report_defect",
                )
            ]
        ]
    )


# ================= Хендлери =================

@dp.message(CommandStart())
async def cmd_start(message: Message):
    get_user_data(message.from_user.id)
    text = (
        f"Привіт, {message.from_user.first_name}! 👋\n\n"
        f"🤖 Ласкаво просимо до **MarketPhoto AI**!\n"
        f"🎁 Вам доступно **{DAILY_FREE_LIMIT} безкоштовних генерацій** щодня.\n"
        f"Обирайте режим або тисніть «⚙️ Послуги / Функції»."
    )
    await message.answer(text, reply_markup=main_keyboard(), parse_mode="Markdown")


@dp.message(Command("profile"))
@dp.message(F.text == "👤 Профіль")
async def cmd_profile(message: Message):
    user_id = message.from_user.id
    user_data = get_user_data(user_id)
    credits = user_data.get("credits", 0)
    stars_spent = user_data.get("stars_spent", 0)
    sub_until = user_data.get("sub_until", 0)

    if user_id == ADMIN_ID:
        free_left = "♾️ Нескінченно (Адмін)"
    else:
        free_left = f"{max(0, DAILY_FREE_LIMIT - user_data.get('daily_count', 0))} з {DAILY_FREE_LIMIT}"

    sub_status = "Немає"
    if sub_until > time.time():
        time_left = int((sub_until - time.time()) / (24 * 3600))
        sub_status = f"Активна (залишилось {time_left} дн.)"

    profile_text = (
        f"👤 **Ваш особистий профіль:**\n\n"
        f"🆔 ID: `{user_id}`\n"
        f"🎁 Безкоштовних на сьогодні: **{free_left}**\n"
        f"💎 Доступно кредитів: **{credits}**\n"
        f"⭐ Витрачено зірок (XTR): **{stars_spent}**\n"
        f"👑 VIP-Підписка: **{sub_status}**"
    )
    await message.answer(profile_text, parse_mode="Markdown")


@dp.message(F.text == "⚙️ Послуги / Функції")
@dp.message(F.text == "🎨 Створити фото")
@dp.message(F.text == "⚙️ Послуги")
async def cmd_choose_service(message: Message, state: FSMContext):
    await message.answer(
        "🚀 **Оберіть режим створення:**\n\n"
        "1️⃣ **З нуля за описом (Text-to-Image)** — напишіть будь-який запит (наприклад: *Бетмен*), і ШІ змалює саме це.\n"
        "2️⃣ **Обробка фото** — надішліть свою світлину для стилізації.",
        reply_markup=generation_mode_keyboard(),
        parse_mode="Markdown",
    )


@dp.callback_query(F.data.in_(["gen_text", "gen_photo"]))
async def process_gen_mode(callback: types.CallbackQuery, state: FSMContext):
    if callback.data == "gen_text":
        await state.update_data(gen_mode="text")
        await callback.message.edit_text(
            "🎨 **Оберіть категорію для генерації з нуля:**",
            reply_markup=photo_type_keyboard(),
            parse_mode="Markdown",
        )
    else:
        await state.update_data(gen_mode="photo")
        await callback.message.edit_text(
            "📸 **Оберіть стиль для обробки вашої фотографії:**",
            reply_markup=photo_type_keyboard(),
            parse_mode="Markdown",
        )
    await callback.answer()


@dp.callback_query(F.data.startswith("type_"))
async def process_type_selection(callback: types.CallbackQuery, state: FSMContext):
    photo_type = callback.data.replace("type_", "")
    type_names = {
        "avatar": "Стильна Аватарка",
        "wallpaper": "Заставка / Шпалери",
        "art": "Художнє фото",
    }
    selected_name = type_names.get(photo_type, "Стандартний стиль")
    await state.update_data(photo_type=selected_name)

    state_data = await state.get_data()
    gen_mode = state_data.get("gen_mode", "text")

    if gen_mode == "text":
        await state.set_state(PhotoGenerationState.waiting_for_text)
        await callback.message.edit_text(
            f"🎯 Обрано стиль: **{selected_name}**\n\n"
            f"✏️ Напишіть текстом, що саме ви хочете намалювати (наприклад: _«Бетмен»_):",
            parse_mode="Markdown",
        )
    else:
        await state.set_state(PhotoGenerationState.waiting_for_photo)
        await callback.message.edit_text(
            f"🎯 Обрано стиль: **{selected_name}**\n\n"
            f"📸 Надішліть вашу фотографію сюди в чат:",
            parse_mode="Markdown",
        )
    await callback.answer()


# ================= Анімація прогресу =================
async def animate_progress(status_msg: Message, style_name: str):
    stages = [
        ("🚀 **Запуск нейромережі Flux...**\n░░░░░░░░░░ **0%** ⚙️ *Обробка запиту...*", 0.4),
        ("🎨 **Малюємо за вашим описом...**\n▓▓▓░░░░░░░ **35%** 🖌️ *Створюємо деталі...*", 0.5),
        ("✨ **Покращення якості...**\n▓▓▓▓▓▓▓░░░ **70%** ⚡ *Налаштування освітлення...*", 0.5),
        ("🎉 **Готово!**\n▓▓▓▓▓▓▓▓▓▓ **100%** 🏁 *Відправляємо результат...*", 0.3),
    ]

    for text, delay in stages:
        try:
            await status_msg.edit_text(
                f"🎨 Стиль: **{style_name}**\n\n{text}",
                parse_mode="Markdown",
            )
            await asyncio.sleep(delay)
        except Exception:
            pass


# ================= Генерація за текстом =================
@dp.message(PhotoGenerationState.waiting_for_text)
async def process_text_generation(message: Message, state: FSMContext):
    user_id = message.from_user.id
    prompt_text = message.text

    success, source_type, remainder = use_generation(user_id)
    if not success:
        await message.answer(
            "❌ У вас закінчилися як безкоштовні спроби на сьогодні, так і кредити!\n"
            "Поповніть баланс у «🛒 Магазин зірок» або зачекайте до завтра."
        )
        await state.clear()
        return

    state_data = await state.get_data()
    style_name = state_data.get("photo_type", "Загальний стиль")

    status_msg = await message.answer("🚀 Починаємо створення за вашим запитом...")
    await animate_progress(status_msg, style_name)

    formatted_prompt = enhance_prompt(prompt_text, style_name)
    encoded_prompt = urllib.parse.quote(formatted_prompt)
    seed = int(time.time() * 1000) % 100000
    ai_image_url = f"https://image.pollinations.ai/prompt/{encoded_prompt}?width=1024&height=1024&model=flux&seed={seed}&nologo=true"

    try:
        await status_msg.delete()
    except Exception:
        pass

    # Надсилаємо пряме посилання — Telegram сам швидко завантажить картинку без помилок
    await message.answer_photo(
        photo=ai_image_url,
        caption=(
            f"✨ **Згенеровано за вашим запитом!**\n"
            f"🎨 Стиль: _{style_name}_\n"
            f"📝 Ваш запит: _{prompt_text}_\n\n"
            f"Якщо якість вас не влаштувала, натисніть кнопку нижче:"
        ),
        reply_markup=user_result_keyboard(),
        parse_mode="Markdown",
    )
    await state.clear()


# ================= Обробка фото =================
@dp.message(PhotoGenerationState.waiting_for_photo, F.photo)
async def process_photo_style(message: Message, state: FSMContext):
    user_id = message.from_user.id

    success, source_type, remainder = use_generation(user_id)
    if not success:
        await message.answer(
            "❌ У вас закінчилися безкоштовні спроби та кредити!\n"
            "Поповніть баланс у «🛒 Магазин зірок»."
        )
        await state.clear()
        return

    state_data = await state.get_data()
    style_name = state_data.get("photo_type", "Загальний стиль")

    status_msg = await message.answer("🚀 Обробляємо ваше фото...")
    await animate_progress(status_msg, style_name)

    photo_file_id = message.photo[-1].file_id

    try:
        await status_msg.delete()
    except Exception:
        pass

    await message.answer_photo(
        photo=photo_file_id,
        caption=(
            f"✨ **Обробку завершено!**\n"
            f"🎨 Стиль: _{style_name}_\n\n"
            f"Якщо результат бракований, натисніть кнопку нижче:"
        ),
        reply_markup=user_result_keyboard(),
        parse_mode="Markdown",
    )
    await state.clear()


# ================= Заявка про брак від юзера =================
@dp.callback_query(F.data == "user_report_defect")
async def process_user_defect_report(callback: types.CallbackQuery):
    user_id = callback.from_user.id
    photo_file_id = callback.message.photo[-1].file_id if callback.message.photo else None

    await callback.answer("📩 Заявка про брак надіслана адміністратору!", show_alert=True)
    await callback.message.edit_caption(
        caption=callback.message.caption + "\n\n⏳ **Заявку про брак відправлено на перевірку адміну.**",
        parse_mode="Markdown",
    )

    admin_kb = InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="🔄 Повернути спробу / кредит",
                    callback_data=f"mod_refund_{user_id}",
                ),
                InlineKeyboardButton(
                    text="❌ Відхилити",
                    callback_data=f"mod_reject_{user_id}",
                ),
            ]
        ]
    )

    try:
        caption_text = (
            f"⚠️ **Скарга на брак від користувача!**\n\n"
            f"👤 Користувач: @{callback.from_user.username or 'немає'} (ID: `{user_id}`)\n"
            f"Перевірте фото та поверніть спробу за потреби:"
        )

        if photo_file_id:
            await bot.send_photo(
                ADMIN_ID,
                photo=photo_file_id,
                caption=caption_text,
                reply_markup=admin_kb,
                parse_mode="Markdown",
            )
        else:
            await bot.send_message(
                ADMIN_ID,
                text=caption_text,
                reply_markup=admin_kb,
                parse_mode="Markdown",
            )
    except Exception as e:
        print(f"Помилка відправки адміну: {e}")


# ================= Колбеки модератора =================
@dp.callback_query(F.data.startswith("mod_"))
async def process_moderation(callback: types.CallbackQuery):
    if callback.from_user.id != ADMIN_ID:
        await callback.answer("❌ Немає прав.", show_alert=True)
        return

    data = callback.data.split("_")
    action = data[1]
    target_user_id = int(data[2])

    if action == "reject":
        await callback.message.edit_caption(
            caption=callback.message.caption + "\n\n❌ **Скаргу відхилено.**",
            parse_mode="Markdown",
        )
        await callback.answer("Відхилено.")

    elif action == "refund":
        db = load_db()
        user_id_str = str(target_user_id)
        if user_id_str in db:
            if db[user_id_str].get("daily_count", 0) > 0:
                db[user_id_str]["daily_count"] -= 1
            else:
                db[user_id_str]["credits"] += 1
            save_db(db)

        await callback.message.edit_caption(
            caption=callback.message.caption + "\n\n✅ **Спробу/кредит повернуто користувачеві.**",
            parse_mode="Markdown",
        )
        await callback.answer("Компенсацію надано!")

        try:
            await bot.send_message(
                target_user_id,
                "🎁 **Компенсація!**\nВашу скаргу на брак розглянуто. Вам повернено 1 спробу/кредит на баланс.",
            )
        except Exception:
            pass


# ================= Оплата Telegram Stars =================
@dp.message(Command("shop", "buy"))
@dp.message(F.text == "🛒 Магазин зірок")
async def cmd_shop(message: Message):
    await message.answer(
        "🛒 **Магазин покупок за Telegram Stars (XTR)**\n\n"
        "Оберіть потрібний пакет. Оплата проходить миттєво й безпечно:",
        reply_markup=shop_keyboard(),
        parse_mode="Markdown",
    )


@dp.callback_query(
    F.data.in_(["buy_50_credits", "buy_100_credits", "buy_vip_sub"])
)
async def process_buy_callback(callback: types.CallbackQuery):
    if callback.data == "buy_50_credits":
        title = "Пакет 50 кредитів"
        desc = "Поповнення балансу бота на 50 кредитів."
        price = 25
        payload = "payload_50_credits"
    elif callback.data == "buy_100_credits":
        title = "Пакет 100 кредитів"
        desc = "Поповнення балансу бота на 100 кредитів."
        price = 45
        payload = "payload_100_credits"
    elif callback.data == "buy_vip_sub":
        title = "VIP Підписка на 30 днів"
        desc = "Отримання VIP-статусу + 50 бонусних кредитів."
        price = 100
        payload = "payload_vip_sub"

    prices = [LabeledPrice(label=title, amount=price)]

    await bot.send_invoice(
        chat_id=callback.message.chat.id,
        title=title,
        description=desc,
        payload=payload,
        provider_token="",
        currency="XTR",
        prices=prices,
        start_parameter="buy-stars",
    )
    await callback.answer()


@dp.pre_checkout_query()
async def process_pre_checkout(pre_checkout_query: PreCheckoutQuery):
    await bot.answer_pre_checkout_query(pre_checkout_query.id, ok=True)


@dp.message(F.successful_payment)
async def process_successful_payment(message: Message):
    payment_info = message.successful_payment
    payload = payment_info.invoice_payload
    user_id = message.from_user.id
    stars = payment_info.total_amount

    if payload == "payload_50_credits":
        user_data = update_user_purchase(
            user_id, credits_to_add=50, stars_spent=stars
        )
        msg_text = f"🎉 **Дякуємо за покупку!**\n\n➕ Зараховано: **50 кредитів**\n💰 Поточний баланс: **{user_data['credits']}**"
    elif payload == "
