import asyncio
import aiohttp
from aiohttp import web
import base64
import html
import io
import json
import logging
import os
import re
import sqlite3
import time
import uuid

from datetime import datetime, timezone
from PIL import Image, ImageOps

from aiogram import Bot, Dispatcher, F
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.filters import Command, CommandStart
from aiogram.types import (
    Message,
    CallbackQuery,
    PreCheckoutQuery,
    InlineKeyboardMarkup,
    InlineKeyboardButton,
    BufferedInputFile,
    LabeledPrice,
    BotCommand,
)

# =========================================================
# CONFIG
# =========================================================

BOT_TOKEN = (
    os.getenv("BOT_TOKEN")
    or os.getenv("TELEGRAM_BOT_TOKEN")
    or ""
).strip()

CF_ACCOUNT_ID = (
    os.getenv("CLOUDFLARE_ACCOUNT_ID")
    or os.getenv("CF_ACCOUNT_ID")
    or ""
).strip()

CF_API_TOKEN = (
    os.getenv("CLOUDFLARE_API_TOKEN")
    or os.getenv("CF_API_TOKEN")
    or ""
).strip()

ADMIN_ID = int(os.getenv("ADMIN_ID", "0") or "0")
PRICE_STARS = max(1, int(os.getenv("PRICE_STARS", "3") or "3"))
PORT = int(os.getenv("PORT", "10000"))

MODEL = os.getenv(
    "CLOUDFLARE_MODEL",
    "@cf/black-forest-labs/flux-2-klein-4b",
).strip()

FREE_LIMIT = 5
DATABASE_PATH = os.getenv("DATABASE_PATH", "marketphoto.db")
MAX_UPLOAD_SIZE = 10 * 1024 * 1024

if not BOT_TOKEN:
    raise RuntimeError(
        "BOT_TOKEN is missing. Add BOT_TOKEN in Render Environment."
    )

if not CF_ACCOUNT_ID or not CF_API_TOKEN:
    raise RuntimeError(
        "Missing CLOUDFLARE_ACCOUNT_ID or CLOUDFLARE_API_TOKEN."
    )

if ADMIN_ID <= 0:
    raise RuntimeError(
        "ADMIN_ID is missing. Set your numeric Telegram user ID."
    )

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
)

log = logging.getLogger("marketphoto")

bot = Bot(
    token=BOT_TOKEN,
    default=DefaultBotProperties(parse_mode=ParseMode.HTML),
)

dp = Dispatcher()

db = sqlite3.connect(
    DATABASE_PATH,
    check_same_thread=False,
)

db.row_factory = sqlite3.Row
db.execute("PRAGMA journal_mode=WAL")

http_session = None
generation_lock = asyncio.Lock()


# =========================================================
# DATABASE
# =========================================================

def init_db():
    db.executescript("""
    CREATE TABLE IF NOT EXISTS users (
        user_id INTEGER PRIMARY KEY,
        username TEXT,
        created_at INTEGER NOT NULL
    );

    CREATE TABLE IF NOT EXISTS daily_usage (
        user_id INTEGER NOT NULL,
        day TEXT NOT NULL,
        count INTEGER NOT NULL DEFAULT 0,
        PRIMARY KEY (user_id, day)
    );

    CREATE TABLE IF NOT EXISTS pending_edits (
        user_id INTEGER PRIMARY KEY,
        image_bytes BLOB NOT NULL,
        created_at INTEGER NOT NULL
    );

    CREATE TABLE IF NOT EXISTS orders (
        payload TEXT PRIMARY KEY,
        user_id INTEGER NOT NULL,
        prompt TEXT NOT NULL,
        kind TEXT NOT NULL,
        image_bytes BLOB,
        created_at INTEGER NOT NULL
    );

    CREATE TABLE IF NOT EXISTS payments (
        charge_id TEXT PRIMARY KEY,
        user_id INTEGER NOT NULL,
        payload TEXT NOT NULL,
        stars INTEGER NOT NULL,
        refunded INTEGER NOT NULL DEFAULT 0,
        created_at INTEGER NOT NULL
    );

    CREATE TABLE IF NOT EXISTS generations (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id INTEGER NOT NULL,
        prompt TEXT NOT NULL,
        kind TEXT NOT NULL,
        photo_file_id TEXT,
        charge_id TEXT,
        stars_paid INTEGER NOT NULL DEFAULT 0,
        created_at INTEGER NOT NULL,
        complaint_status TEXT NOT NULL DEFAULT 'none',
        refunded INTEGER NOT NULL DEFAULT 0
    );

    CREATE TABLE IF NOT EXISTS complaints (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        generation_id INTEGER NOT NULL UNIQUE,
        user_id INTEGER NOT NULL,
        status TEXT NOT NULL DEFAULT 'pending',
        created_at INTEGER NOT NULL
    );
    """)
    db.commit()


def utc_day():
    return datetime.now(timezone.utc).date().isoformat()


def register_user(user):
    db.execute(
        """
        INSERT INTO users(user_id, username, created_at)
        VALUES (?, ?, ?)
        ON CONFLICT(user_id)
        DO UPDATE SET username=excluded.username
        """,
        (user.id, user.username, int(time.time())),
    )
    db.commit()


def used_today(user_id):
    if user_id == ADMIN_ID:
        return 0

    row = db.execute(
        """
        SELECT count FROM daily_usage
        WHERE user_id=? AND day=?
        """,
        (user_id, utc_day()),
    ).fetchone()

    return int(row["count"]) if row else 0


def remaining_today(user_id):
    if user_id == ADMIN_ID:
        return "∞"

    return max(0, FREE_LIMIT - used_today(user_id))


def increment_usage(user_id):
    if user_id == ADMIN_ID:
        return

    db.execute(
        """
        INSERT INTO daily_usage(user_id, day, count)
        VALUES (?, ?, 1)
        ON CONFLICT(user_id, day)
        DO UPDATE SET count=count+1
        """,
        (user_id, utc_day()),
    )
    db.commit()


# =========================================================
# MENUS
# =========================================================

def main_menu():
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="🎨 Создать изображение",
                    callback_data="menu_generate",
                )
            ],
            [
                InlineKeyboardButton(
                    text="🖼 Редактировать фото",
                    callback_data="menu_edit",
                )
            ],
            [
                InlineKeyboardButton(
                    text="⭐ Оплата и генерации",
                    callback_data="menu_payment",
                )
            ],
            [
                InlineKeyboardButton(
                    text="📊 Мой лимит",
                    callback_data="menu_limit",
                )
            ],
        ]
    )


def defect_keyboard(generation_id):
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="❌ Брак",
                    callback_data=f"defect:{generation_id}",
                )
            ]
        ]
    )


def admin_complaint_keyboard(complaint_id):
    # Refund is offered only after the admin confirms the defect.
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="✅ Подтвердить брак",
                    callback_data=f"complaint_accept:{complaint_id}",
                )
            ],
            [
                InlineKeyboardButton(
                    text="❌ Отклонить жалобу",
                    callback_data=f"complaint_reject:{complaint_id}",
                )
            ],
        ]
    )


def refund_keyboard(complaint_id):
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="⭐ Вернуть Stars",
                    callback_data=f"refund:{complaint_id}",
                )
            ]
        ]
    )


# =========================================================
# SAFETY FOR PHOTO EDITING
# =========================================================

def disallowed_prompt(prompt):
    """
    Block instructions intended to undress a real person.
    Normal clothing, fashion, hairstyle and background edits
    are still available.
    """
    blocked_phrases = [
        "раздеть",
        "раздевает",
        "раздевать",
        "снять одежду",
        "убрать одежду",
        "без одежды",
        "обнажить",
        "обнаженная",
        "обнажённая",
        "обнаженный",
        "обнажённый",
        "голая",
        "голый",
        "голое тело",
        "undress",
        "naked",
        "nude",
        "remove her clothes",
        "remove his clothes",
    ]

    normalized = prompt.lower()

    return any(
        phrase in normalized
        for phrase in blocked_phrases
    )


# =========================================================
# CLOUDFLARE FLUX.2 KLEIN
# =========================================================

async def cloudflare_generate(prompt, image_bytes=None):
    """
    FLUX.2 Klein requires multipart/form-data even when there
    is no reference image. force_multipart=True is important.

    Reference images are resized so both dimensions are
    below 512 pixels, as required by the model's API.
    """

    global http_session

    if http_session is None or http_session.closed:
        timeout = aiohttp.ClientTimeout(total=180)
        http_session = aiohttp.ClientSession(timeout=timeout)

    url = (
        "https://api.cloudflare.com/client/v4/accounts/"
        f"{CF_ACCOUNT_ID}/ai/run/{MODEL}"
    )

    # Force multipart even for text-only generations.
    form = aiohttp.FormData(force_multipart=True)

    form.add_field("prompt", prompt)
    form.add_field("width", "1024")
    form.add_field("height", "768")

    if image_bytes:
        try:
            with Image.open(io.BytesIO(image_bytes)) as opened:
                image = ImageOps.exif_transpose(opened).convert("RGB")
                image.thumbnail((511, 511))

                output = io.BytesIO()
                image.save(output, format="JPEG", quality=90)
                reference = output.getvalue()

        except Exception as exc:
            raise RuntimeError(
                f"Cannot read uploaded image: {exc}"
            ) from exc

        form.add_field(
            "input_image_0",
            reference,
            filename="reference.jpg",
            content_type="image/jpeg",
        )

    headers = {
        "Authorization": f"Bearer {CF_API_TOKEN}",
    }

    async with http_session.post(
        url,
        data=form,
        headers=headers,
    ) as response:

        raw = await response.read()

        if response.status >= 400:
            detail = raw.decode(
                "utf-8",
                errors="replace",
            )[:1500]

            log.error(
                "Cloudflare HTTP %s: %s",
                response.status,
                detail,
            )

            raise RuntimeError(
                f"Cloudflare HTTP {response.status}: {detail}"
            )

        content_type = response.headers.get(
            "Content-Type",
            "",
        ).lower()

        # Some endpoints return the image directly.
        if content_type.startswith("image/"):
            return raw

        if raw.startswith(b"\x89PNG") or raw.startswith(b"\xff\xd8"):
            return raw

        try:
            data = json.loads(raw.decode("utf-8"))
        except Exception as exc:
            raise RuntimeError(
                f"Unknown Cloudflare response: {raw[:300]!r}"
            ) from exc

        if data.get("success") is False or data.get("errors"):
            detail = json.dumps(
                data.get("errors") or data.get("messages"),
                ensure_ascii=False,
            )[:1000]

            raise RuntimeError(
                f"Cloudflare rejected request: {detail}"
            )

        result = data.get("result", data)

        if isinstance(result, dict):
            image_value = result.get("image")

            if image_value is None:
                image_value = result.get("data")

            if isinstance(image_value, str):
                try:
                    return base64.b64decode(image_value)
                except Exception:
                    pass

            if isinstance(image_value, list):
                try:
                    return bytes(image_value)
                except Exception:
                    pass

        raise RuntimeError(
            "Cloudflare response did not contain image data."
        )


# =========================================================
# SAVE AND SEND GENERATED IMAGE
# =========================================================

async def send_generated(
    message,
    prompt,
    kind="text",
    image_bytes=None,
    charge_id=None,
    stars_paid=0,
):
    status = await message.answer(
        "⏳ Создаю изображение..."
    )

    try:
        image = await cloudflare_generate(
            prompt,
            image_bytes=image_bytes,
        )

        if not image or len(image) < 100:
            raise RuntimeError("Cloudflare returned an empty image")

        sent = await message.answer_photo(
            BufferedInputFile(
                image,
                filename="marketphoto.png",
            ),
            caption=(
                "✨ Готово!\n"
                "Если фото получилось испорченным, нажми «Брак»."
            ),
        )

        cursor = db.execute(
            """
            INSERT INTO generations(
                user_id, prompt, kind, photo_file_id,
                charge_id, stars_paid, created_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                message.from_user.id,
                prompt,
                kind,
                sent.photo[-1].file_id,
                charge_id,
                stars_paid,
                int(time.time()),
            ),
        )

        generation_id = cursor.lastrowid
        db.commit()

        await sent.edit_reply_markup(
            reply_markup=defect_keyboard(generation_id)
        )

        try:
            await status.delete()
        except Exception:
            pass

        return True

    except Exception as exc:
        log.exception(
            "Generation failed for user_id=%s",
            message.from_user.id,
        )

        try:
            await status.edit_text(
                "❌ Не удалось создать изображение.\n\n"
                "Попробуй другой запрос. Если ошибка повторяется, "
                "проверь Render Logs.\n\n"
                f"<code>{html.escape(str(exc)[:700])}</code>"
            )
        except Exception:
            pass

        return False


# =========================================================
# FREE GENERATIONS AND PAID ORDERS
# =========================================================

async def create_paid_order(
    message,
    prompt,
    kind,
    image_bytes=None,
):
    payload = "marketphoto_" + uuid.uuid4().hex

    db.execute(
        """
        INSERT INTO orders(
            payload, user_id, prompt, kind,
            image_bytes, created_at
        )
        VALUES (?, ?, ?, ?, ?, ?)
        """,
        (
            payload,
            message.from_user.id,
            prompt,
            kind,
            image_bytes,
            int(time.time()),
        ),
    )
    db.commit()

    try:
        await bot.send_invoice(
            chat_id=message.chat.id,
            title="MarketPhoto AI — 1 генерация",
            description="Одна генерация или редактирование фото",
            payload=payload,
            provider_token="",
            currency="XTR",
            prices=[
                LabeledPrice(
                    label="Одна генерация",
                    amount=PRICE_STARS,
                )
            ],
        )

    except Exception:
        log.exception("Failed to create Stars invoice")

        db.execute(
            "DELETE FROM orders WHERE payload=?",
            (payload,),
        )
        db.commit()

        await message.answer(
            "Не удалось создать счёт Stars. "
            "Проверь настройки бота и Render Logs."
        )


async def start_generation(
    message,
    prompt,
    kind="text",
    image_bytes=None,
):
    user_id = message.from_user.id

    if not prompt.strip():
        await message.answer("Напиши описание изображения.")
        return

    if disallowed_prompt(prompt):
        await message.answer(
            "Я не могу помогать с раздеванием реальных людей. "
            "Можно изменить одежду, фасон, цвет, причёску, "
            "фон или художественный стиль."
        )
        return

    # Administrator always has unlimited free generation.
    if user_id == ADMIN_ID:
        async with generation_lock:
            success = await send_generated(
                message,
                prompt,
                kind,
                image_bytes,
            )

        if success:
            await message.answer(
                "👑 Администраторский безлимит: ∞ генераций."
            )

        return

    # Check the free limit while holding the generation lock.
    async with generation_lock:
        if used_today(user_id) < FREE_LIMIT:
            success = await send_generated(
                message,
                prompt,
                kind,
                image_bytes,
            )

            if success:
                increment_usage(user_id)
                await message.answer(
                    f"🎁 Бесплатных генераций осталось: "
                    f"<b>{remaining_today(user_id)}</b> из {FREE_LIMIT}."
                )

            return

    # Five free generations have been used.
    await create_paid_order(
        message,
        prompt,
        kind,
        image_bytes,
    )


# =========================================================
# START AND COMMANDS
# =========================================================

@dp.message(CommandStart())
async def start_command(message: Message):
    register_user(message.from_user)

    await message.answer(
        "👋 <b>Добро пожаловать в MarketPhoto AI!</b>\n\n"
        "🎨 Генерация фото по описанию\n"
        "🖼 Редактирование изображений\n"
        "🎁 5 бесплатных генераций ежедневно\n"
        "⭐ Дополнительные генерации через Telegram Stars\n"
        "❌ Кнопка «Брак» под каждым результатом\n\n"
        "👑 Администратор имеет безлимитные генерации.",
        reply_markup=main_menu(),
    )


@dp.message(Command("help"))
async def help_command(message: Message):
    await message.answer(
        "Как пользоваться ботом:\n\n"
        "1. Нажми «Создать изображение» и напиши описание.\n"
        "2. Для редактирования отправь фото, затем опиши изменения.\n"
        "3. После бесплатного лимита оплати следующую генерацию Stars.\n"
        "4. Если изображение испорчено, нажми «Брак».\n\n"
        "/limit — проверить лимит\n"
        "/cancel — отменить ожидание фото\n"
        "/admin — статистика администратора",
        reply_markup=main_menu(),
    )


@dp.message(Command("limit"))
async def limit_command(message: Message):
    register_user(message.from_user)

    user_id = message.from_user.id

    if user_id == ADMIN_ID:
        await message.answer(
            "👑 Ваш лимит: <b>без ограничений</b>."
        )
    else:
        await message.answer(
            f"Использовано сегодня: {used_today(user_id)}/{FREE_LIMIT}\n"
            f"Осталось: {remaining_today(user_id)}"
        )


@dp.message(Command("cancel"))
async def cancel_command(message: Message):
    db.execute(
        "DELETE FROM pending_edits WHERE user_id=?",
        (message.from_user.id,),
    )
    db.commit()

    await message.answer(
        "Ожидание фото отменено.",
        reply_markup=main_menu(),
    )


@dp.message(Command("admin"))
async def admin_command(message: Message):
    if message.from_user.id != ADMIN_ID:
        await message.answer("Эта команда доступна только администратору.")
        return

    users = db.execute(
        "SELECT COUNT(*) AS n FROM users"
    ).fetchone()["n"]

    generations = db.execute(
        "SELECT COUNT(*) AS n FROM generations"
    ).fetchone()["n"]

    complaints = db.execute(
        "SELECT COUNT(*) AS n FROM complaints WHERE status='pending'"
    ).fetchone()["n"]

    income = db.execute(
        """
        SELECT COALESCE(SUM(stars), 0) AS n
        FROM payments WHERE refunded=0
        """
    ).fetchone()["n"]

    await message.answer(
        "🛠 <b>Панель администратора</b>\n\n"
        f"Пользователей: {users}\n"
        f"Всего изображений: {generations}\n"
        f"Новых жалоб: {complaints}\n"
        f"Невозвращённых Stars: {income}\n\n"
        "Ваш лимит: ∞"
    )


# =========================================================
# MAIN MENU CALLBACKS
# =========================================================

@dp.callback_query(F.data == "menu_generate")
async def menu_generate(call: CallbackQuery):
    await call.answer()
    await call.message.answer(
        "Напиши описание изображения, которое нужно создать."
    )


@dp.callback_query(F.data == "menu_edit")
async def menu_edit(call: CallbackQuery):
    await call.answer()
    await call.message.answer(
        "Отправь фотографию, которую нужно отредактировать. "
        "Следующим сообщением напиши, что изменить."
    )


@dp.callback_query(F.data == "menu_payment")
async def menu_payment(call: CallbackQuery):
    await call.answer()

    if call.from_user.id == ADMIN_ID:
        await call.message.answer(
            "👑 Для администратора генерации бесплатны и не ограничены."
        )
    else:
        await call.message.answer(
            f"🎁 Бесплатно: {FREE_LIMIT} генераций в день.\n"
            f"⭐ Затем каждая генерация стоит {PRICE_STARS} Stars."
        )


@dp.callback_query(F.data == "menu_limit")
async def menu_limit(call: CallbackQuery):
    await call.answer()

    if call.from_user.id == ADMIN_ID:
        await call.message.answer(
            "👑 Ваш лимит: <b>∞</b>."
        )
    else:
        await call.message.answer(
            f"Использовано сегодня: {used_today(call.from_user.id)}/{FREE_LIMIT}\n"
            f"Осталось: {remaining_today(call.from_user.id)}"
        )


# =========================================================
# PHOTO UPLOAD AND EDITING
# =========================================================

@dp.message(F.photo)
async def receive_photo(message: Message):
    register_user(message.from_user)

    try:
        photo = message.photo[-1]
        telegram_file = await bot.get_file(photo.file_id)

        buffer = io.BytesIO()

        await bot.download_file(
            telegram_file.file_path,
            destination=buffer,
        )

        image_bytes = buffer.getvalue()

        if len(image_bytes) > MAX_UPLOAD_SIZE:
            await message.answer(
                "Фото слишком большое. Отправь изображение до 10 МБ."
            )
            return

        # Save image while waiting for the edit instruction.
        db.execute(
            """
            INSERT INTO pending_edits(user_id, image_bytes, created_at)
            VALUES (?, ?, ?)
            ON CONFLICT(user_id)
            DO UPDATE SET
                image_bytes=excluded.image_bytes,
                created_at=excluded.created_at
            """,
            (
                message.from_user.id,
                image_bytes,
                int(time.time()),
            ),
        )
        db.commit()

        # If user added a caption, use it as the edit prompt.
        if message.caption and message.caption.strip():
            db.execute(
                "DELETE FROM pending_edits WHERE user_id=?",
                (message.from_user.id,),
            )
            db.commit()

            await start_generation(
                message,
                message.caption.strip(),
                kind="edit",
                image_bytes=image_bytes,
            )
        else:
            await message.answer(
                "📷 Фото получено!\n\n"
                "Теперь напиши, что изменить: одежду, фон, "
                "причёску, освещение, цвета или художественный стиль."
            )

    except Exception:
        log.exception("Photo upload error")
        await message.answer(
            "Не удалось обработать фотографию. Попробуй отправить её ещё раз."
        )


@dp.message(F.text)
async def receive_text(message: Message):
    prompt = (message.text or "").strip()

    if not prompt or prompt.startswith("/"):
        return

    register_user(message.from_user)

    row = db.execute(
        "SELECT image_bytes FROM pending_edits WHERE user_id=?",
        (message.from_user.id,),
    ).fetchone()

    if row:
        image_bytes = row["image_bytes"]

        db.execute(
            "DELETE FROM pending_edits WHERE user_id=?",
            (message.from_user.id,),
        )
        db.commit()

        await start_generation(
            message,
            prompt,
            kind="edit",
            image_bytes=image_bytes,
        )

    else:
        await start_generation(
            message,
            prompt,
            kind="text",
        )


# =========================================================
# TELEGRAM STARS PAYMENT
# =========================================================

@dp.pre_checkout_query()
async def pre_checkout(query: PreCheckoutQuery):
    order = db.execute(
        "SELECT * FROM orders WHERE payload=?",
        (query.invoice_payload,),
    ).fetchone()

    if not order or order["user_id"] != query.from_user.id:
        await query.answer(
            ok=False,
            error_message="Заказ не найден.",
        )
        return

    if query.currency != "XTR" or query.total_amount != PRICE_STARS:
        await query.answer(
            ok=False,
            error_message="Неверная сумма платежа.",
        )
        return

    await query.answer(ok=True)


async def refund_stars(charge_id, user_id):
    """
    Returns actual Telegram Stars through Telegram's Bot API.
    Database is updated only after Telegram confirms the refund.
    """
    await bot.refund_star_payment(
        user_id=user_id,
        telegram_payment_charge_id=charge_id,
    )

    db.execute(
        "UPDATE payments SET refunded=1 WHERE charge_id=?",
        (charge_id,),
    )

    db.execute(
        "UPDATE generations SET refunded=1 WHERE charge_id=?",
        (charge_id,),
    )

    db.commit()


@dp.message(F.successful_payment)
async def successful_payment(message: Message):
    payment = message.successful_payment
    user_id = message.from_user.id

    payload = payment.invoice_payload
    charge_id = payment.telegram_payment_charge_id

    # Prevent duplicate processing of the same Telegram charge.
    already_processed = db.execute(
        "SELECT charge_id FROM payments WHERE charge_id=?",
        (charge_id,),
    ).fetchone()

    if already_processed:
        await message.answer(
            "Этот платёж уже обработан."
        )
        return

    order = db.execute(
        "SELECT * FROM orders WHERE payload=?",
        (payload,),
    ).fetchone()

    if not order or order["user_id"] != user_id:
        log.error(
            "Payment without valid order: user=%s payload=%s",
            user_id,
            payload,
        )

        await bot.send_message(
            ADMIN_ID,
            "⚠️ Оплата без найденного заказа.\n"
            f"User ID: <code>{user_id}</code>\n"
            f"Charge ID: <code>{html.escape(charge_id)}</code>",
        )

        await message.answer(
            "Платёж получен, но заказ не найден. "
            "Администратор проверит ситуацию."
        )
        return

    db.execute(
        """
        INSERT INTO payments(
            charge_id, user_id, payload, stars,
            refunded, created_at
        )
        VALUES (?, ?, ?, ?, 0, ?)
        """,
        (
            charge_id,
            user_id,
            payload,
            payment.total_amount,
            int(time.time()),
        ),
    )
    db.commit()

    await message.answer(
        "⭐ Оплата получена. Начинаю генерацию..."
    )

    async with generation_lock:
        success = await send_generated(
            message,
            order["prompt"],
            kind=order["kind"],
            image_bytes=order["image_bytes"],
            charge_id=charge_id,
            stars_paid=payment.total_amount,
        )

    if success:
        db.execute(
            "DELETE FROM orders WHERE payload=?",
            (payload,),
        )
        db.commit()
        return

    # Automatic refund if generation fails after payment.
    try:
        await refund_stars(charge_id, user_id)

        db.execute(
            "DELETE FROM orders WHERE payload=?",
            (payload,),
        )
        db.commit()

        await message.answer(
            "Генерация не удалась. Оплата Stars возвращена."
        )

    except Exception:
        log.exception("Automatic Stars refund failed")

        await message.answer(
            "Генерация не удалась, а автоматический возврат не прошёл. "
            "Администратор проверит платёж."
        )

        await bot.send_message(
            ADMIN_ID,
            "⚠️ Не удалось вернуть Stars автоматически.\n"
            f"User ID: <code>{user_id}</code>\n"
            f"Charge ID: <code>{html.escape(charge_id)}</code>\n"
            f"Сумма: {payment.total_amount} Stars",
        )


# =========================================================
# BUTTON: "БРАК"
# =========================================================

@dp.callback_query(F.data.startswith("defect:"))
async def defect_button(call: CallbackQuery):
    try:
        generation_id = int(call.data.split(":")[1])
    except (ValueError, IndexError):
        await call.answer(
            "Некорректный запрос.",
            show_alert=True,
        )
        return

    generation = db.execute(
        "SELECT * FROM generations WHERE id=?",
        (generation_id,),
    ).fetchone()

    if not generation or generation["user_id"] != call.from_user.id:
        await call.answer(
            "Это изображение не принадлежит вашему аккаунту.",
            show_alert=True,
        )
        return

    if generation["complaint_status"] != "none":
        await call.answer(
            "Жалоба уже отправлена.",
            show_alert=True,
        )
        return

    cursor = db.execute(
        """
        INSERT INTO complaints(
            generation_id, user_id, status, created_at
        )
        VALUES (?, ?, 'pending', ?)
        """,
        (
            generation_id,
            call.from_user.id,
            int(time.time()),
        ),
    )

    complaint_id = cursor.lastrowid

    db.execute(
        """
        UPDATE generations
        SET complaint_status='pending'
        WHERE id=?
        """,
        (generation_id,),
    )
    db.commit()

    await call.answer(
        "Жалоба отправлена администратору."
    )

    username = (
        "@" + call.from_user.username
        if call.from_user.username
        else "без username"
    )

    caption = (
        f"🚨 <b>Жалоба на изображение #{generation_id}</b>\n\n"
        f"Жалоба: <code>{complaint_id}</code>\n"
        f"Пользователь: {html.escape(username)}\n"
        f"ID пользователя: <code>{call.from_user.id}</code>\n"
        f"Тип: {html.escape(generation['kind'])}\n"
        f"Запрос: {html.escape(generation['prompt'][:350])}\n"
        f"Оплата: {generation['stars_paid']} Stars"
    )

    try:
        await bot.send_photo(
            chat_id=ADMIN_ID,
            photo=generation["photo_file_id"],
            caption=caption,
            reply_markup=admin_complaint_keyboard(
                complaint_id
            ),
        )

    except Exception:
        log.exception(
            "Could not send complaint photo to administrator"
        )

        await bot.send_message(
            ADMIN_ID,
            caption,
            reply_markup=admin_complaint_keyboard(
                complaint_id
            ),
        )


# =========================================================
# ADMIN: ACCEPT OR REJECT COMPLAINT
# =========================================================

async def resolve_complaint(call, decision):
    if call.from_user.id != ADMIN_ID:
        await call.answer(
            "Только администратор может выполнять это действие.",
            show_alert=True,
        )
        return

    try:
        complaint_id = int(call.data.split(":")[1])
    except (ValueError, IndexError):
        await call.answer(
            "Некорректная жалоба.",
            show_alert=True,
        )
        return

    complaint = db.execute(
        "SELECT * FROM complaints WHERE id=?",
        (complaint_id,),
    ).fetchone()

    if not complaint or complaint["status"] != "pending":
        await call.answer(
            "Жалоба уже обработана или не найдена.",
            show_alert=True,
        )
        return

    generation = db.execute(
        "SELECT * FROM generations WHERE id=?",
        (complaint["generation_id"],),
    ).fetchone()

    db.execute(
        "UPDATE complaints SET status=? WHERE id=?",
        (decision, complaint_id),
    )

    db.execute(
        """
        UPDATE generations
        SET complaint_status=?
        WHERE id=?
        """,
        (decision, complaint["generation_id"]),
    )

    db.commit()

    if decision == "accepted":
        await bot.send_message(
            complaint["user_id"],
            "✅ Администратор подтвердил брак изображения.",
        )
    else:
        await bot.send_message(
            complaint["user_id"],
            "❌ Администратор проверил жалобу, "
            "но не подтвердил брак.",
        )

    # Refund button appears only after the admin approves
    # a paid generation complaint.
    has_paid_payment = bool(
        decision == "accepted"
        and generation
        and generation["charge_id"]
        and generation["stars_paid"] > 0
        and not generation["refunded"]
    )

    try:
        if has_paid_payment:
            await call.message.edit_reply_markup(
                reply_markup=refund_keyboard(complaint_id)
            )
        else:
            await call.message.edit_reply_markup(
                reply_markup=None
            )
    except Exception:
        log.exception("Could not update complaint buttons")

    await call.answer(
        "Решение сохранено."
    )


@dp.callback_query(F.data.startswith("complaint_accept:"))
async def accept_complaint(call: CallbackQuery):
    await resolve_complaint(call, "accepted")


@dp.callback_query(F.data.startswith("complaint_reject:"))
async def reject_complaint(call: CallbackQuery):
    await resolve_complaint(call, "rejected")


# =========================================================
# ADMIN: REFUND STARS
# =========================================================

@dp.callback_query(F.data.startswith("refund:"))
async def refund_complaint(call: CallbackQuery):
    if call.from_user.id != ADMIN_ID:
        await call.answer(
            "Только администратор может возвращать Stars.",
            show_alert=True,
        )
        return

    try:
        complaint_id = int(call.data.split(":")[1])
    except (ValueError, IndexError):
        await call.answer(
            "Некорректная жалоба.",
            show_alert=True,
        )
        return

    complaint = db.execute(
        "SELECT * FROM complaints WHERE id=?",
        (complaint_id,),
    ).fetchone()

    if not complaint or complaint["status"] != "accepted":
        await call.answer(
            "Сначала нужно подтвердить брак.",
            show_alert=True,
        )
        return

    generation = db.execute(
        "SELECT * FROM generations WHERE id=?",
        (complaint["generation_id"],),
    ).fetchone()

    if (
        not generation
        or not generation["charge_id"]
        or generation["stars_paid"] <= 0
    ):
        await call.answer(
            "Это была бесплатная генерация.",
            show_alert=True,
        )
        return

    payment = db.execute(
        "SELECT * FROM payments WHERE charge_id=?",
        (generation["charge_id"],),
    ).fetchone()

    if (
        not payment
        or payment["refunded"]
        or generation["refunded"]
    ):
        await call.answer(
            "Этот платёж уже возвращён или не найден.",
            show_alert=True,
        )
        return

    try:
        await refund_stars(
            generation["charge_id"],
            generation["user_id"],
        )

        db.execute(
            """
            UPDATE complaints
            SET status='refunded'
            WHERE id=?
            """,
            (complaint_id,),
        )

        db.execute(
            """
            UPDATE generations
            SET complaint_status='refunded'
            WHERE id=?
            """,
            (generation["id"],),
        )

        db.commit()

        await bot.send_message(
            generation["user_id"],
            f"⭐ Возвращено {generation['stars_paid']} Telegram Stars.",
        )

        await call.message.edit_reply_markup(
            reply_markup=None
        )

        await call.answer("Stars успешно возвращены.")

    except Exception as exc:
        log.exception("Stars refund failed")

        await call.answer(
            f"Возврат не выполнен: {str(exc)[:150]}",
            show_alert=True,
        )


# =========================================================
# RENDER HEALTH SERVER + BOT
# =========================================================

async def health(request):
    return web.json_response({
        "ok": True,
        "service": "MarketPhoto AI",
    })


async def main():
    init_db()

    # Render Web Service requires a listening HTTP port.
    app = web.Application()
    app.router.add_get("/", health)
    app.router.add_get("/health", health)

    runner = web.AppRunner(app)
    await runner.setup()

    site = web.TCPSite(
        runner,
        host="0.0.0.0",
        port=PORT,
    )

    await site.start()

    log.info(
        "HEALTH SERVER STARTED on port %s",
        PORT,
    )

    try:
        await bot.set_my_commands([
            BotCommand(
                command="start",
                description="Открыть меню",
            ),
            BotCommand(
                command="help",
                description="Помощь",
            ),
            BotCommand(
                command="limit",
                description="Мой лимит",
            ),
            BotCommand(
                command="cancel",
                description="Отменить действие",
            ),
            BotCommand(
                command="admin",
                description="Статистика администратора",
            ),
        ])

        me = await bot.get_me()

        log.info(
            "TELEGRAM CONNECTED: @%s, id=%s",
            me.username,
            me.id,
        )

        log.info("STARTING TELEGRAM POLLING")

        await dp.start_polling(bot)

    finally:
        await runner.cleanup()

        if http_session and not http_session.closed:
            await http_session.close()

        await bot.session.close()
        db.close()


if __name__ == "__main__":
    asyncio.run(main())
