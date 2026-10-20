import asyncio
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

import aiohttp
from PIL import Image

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
)

# =========================================================
# CONFIGURATION
# =========================================================

BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
CLOUDFLARE_ACCOUNT_ID = os.getenv(
    "CLOUDFLARE_ACCOUNT_ID", ""
).strip()
CLOUDFLARE_API_TOKEN = os.getenv(
    "CLOUDFLARE_API_TOKEN", ""
).strip()

ADMIN_ID = int(os.getenv("ADMIN_ID", "0"))
PRICE_STARS = int(os.getenv("PRICE_STARS", "3"))

MODEL = os.getenv(
    "CLOUDFLARE_MODEL",
    "@cf/black-forest-labs/flux-2-klein-4b"
)

FREE_LIMIT = 5
DB_FILE = os.getenv("DATABASE_PATH", "marketphoto.db")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s:%(name)s:%(message)s",
)

log = logging.getLogger("marketphoto")

if not BOT_TOKEN:
    raise RuntimeError("Set BOT_TOKEN in Render Environment")

if not CLOUDFLARE_ACCOUNT_ID or not CLOUDFLARE_API_TOKEN:
    raise RuntimeError(
        "Set CLOUDFLARE_ACCOUNT_ID and CLOUDFLARE_API_TOKEN"
    )

if ADMIN_ID <= 0:
    raise RuntimeError("Set ADMIN_ID to your numeric Telegram user ID")

bot = Bot(
    BOT_TOKEN,
    default=DefaultBotProperties(parse_mode=ParseMode.HTML),
)

dp = Dispatcher()

db = sqlite3.connect(DB_FILE, check_same_thread=False)
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

    CREATE TABLE IF NOT EXISTS pending_edits (
        user_id INTEGER PRIMARY KEY,
        image_bytes BLOB NOT NULL,
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
        (
            user.id,
            user.username,
            int(time.time()),
        ),
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


def free_left(user_id):
    if user_id == ADMIN_ID:
        return "∞"

    return max(0, FREE_LIMIT - used_today(user_id))


def use_one_free_generation(user_id):
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
# KEYBOARDS
# =========================================================

def main_menu():
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="🎨 Создать изображение",
                    callback_data="generate",
                )
            ],
            [
                InlineKeyboardButton(
                    text="🖼 Редактировать фото",
                    callback_data="edit",
                )
            ],
            [
                InlineKeyboardButton(
                    text="⭐ Купить генерацию",
                    callback_data="buy",
                )
            ],
            [
                InlineKeyboardButton(
                    text="📊 Мой лимит",
                    callback_data="limit",
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


def admin_complaint_keyboard(complaint_id, has_paid_generation):
    rows = [
        [
            InlineKeyboardButton(
                text="✅ Подтвердить брак",
                callback_data=f"accept:{complaint_id}",
            ),
            InlineKeyboardButton(
                text="❌ Отклонить",
                callback_data=f"reject:{complaint_id}",
            ),
        ]
    ]

    if has_paid_generation:
        rows.append([
            InlineKeyboardButton(
                text="⭐ Вернуть Stars",
                callback_data=f"refund:{complaint_id}",
            )
        ])

    return InlineKeyboardMarkup(inline_keyboard=rows)


def refund_only_keyboard(complaint_id):
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
# CLOUDFLARE IMAGE GENERATION
# =========================================================

async def cloudflare_generate(prompt, image_bytes=None):
    """
    FLUX.2 Klein accepts multipart/form-data.

    For editing, the reference image is resized to no more
    than 512x512 before being sent as input_image_0.
    """

    global http_session

    if http_session is None or http_session.closed:
        timeout = aiohttp.ClientTimeout(total=180)
        http_session = aiohttp.ClientSession(timeout=timeout)

    url = (
        "https://api.cloudflare.com/client/v4/accounts/"
        f"{CLOUDFLARE_ACCOUNT_ID}/ai/run/{MODEL}"
    )

    form = aiohttp.FormData()

    form.add_field("prompt", prompt)
    form.add_field("width", "1024")
    form.add_field("height", "768")

    if image_bytes:
        try:
            image = Image.open(io.BytesIO(image_bytes))
            image = image.convert("RGB")
            image.thumbnail((512, 512))

            output = io.BytesIO()
            image.save(output, format="PNG")
            resized_image = output.getvalue()

        except Exception as exc:
            raise RuntimeError(
                f"Invalid uploaded image: {exc}"
            ) from exc

        form.add_field(
            "input_image_0",
            resized_image,
            filename="input.png",
            content_type="image/png",
        )

    headers = {
        "Authorization": f"Bearer {CLOUDFLARE_API_TOKEN}",
    }

    async with http_session.post(
        url,
        data=form,
        headers=headers,
    ) as response:

        raw = await response.read()

        if response.status >= 400:
            details = raw.decode(
                "utf-8", errors="replace"
            )[:1500]

            log.error(
                "Cloudflare HTTP %s: %s",
                response.status,
                details,
            )

            raise RuntimeError(
                f"Cloudflare HTTP {response.status}: {details}"
            )

        content_type = response.headers.get(
            "Content-Type", ""
        ).lower()

        # Direct image response
        if content_type.startswith("image/"):
            return raw

        # Also recognize raw PNG/JPEG responses
        if raw.startswith(b"\x89PNG") or raw.startswith(b"\xff\xd8"):
            return raw

        # Some errors and responses are JSON
        try:
            data = json.loads(raw.decode("utf-8"))
        except Exception as exc:
            raise RuntimeError(
                f"Unexpected Cloudflare response: {raw[:300]!r}"
            ) from exc

        if data.get("success") is False or data.get("errors"):
            details = json.dumps(
                data.get("errors") or data.get("messages"),
                ensure_ascii=False,
            )[:1000]

            raise RuntimeError(
                f"Cloudflare rejected parameters: {details}"
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
            "Cloudflare response did not contain an image"
        )


# =========================================================
# GENERATION / EDITING
# =========================================================

def prompt_is_disallowed(prompt):
    """
    Do not use the bot to undress real people.
    Clothing/style edits are permitted.
    """
    blocked_terms = [
        r"\bundress\b",
        r"\bnaked\b",
        r"\bnude\b",
        r"раздеть",
        r"раздевает",
        r"снять одежду",
        r"без одежды",
        r"обнажить",
        r"голая",
        r"голый",
        r"голое тело",
    ]

    lowered = prompt.lower()

    return any(
        re.search(term, lowered)
        for term in blocked_terms
    )


async def send_generated(
    message,
    prompt,
    kind="text",
    image_bytes=None,
    charge_id=None,
    stars_paid=0,
):
    user_id = message.from_user.id

    status_message = await message.answer(
        "⏳ Создаю изображение..."
    )

    try:
        image = await cloudflare_generate(
            prompt,
            image_bytes=image_bytes,
        )

        if not image or len(image) < 100:
            raise RuntimeError("Empty image response")

        sent = await message.answer_photo(
            BufferedInputFile(
                image,
                filename="marketphoto.png",
            ),
            caption=(
                "✨ Готово!\n"
                "Если изображение испорчено, нажми «Брак»."
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
                user_id,
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

        await status_message.delete()

        await message.answer(
            f"📊 Бесплатных генераций осталось: "
            f"<b>{free_left(user_id) if user_id != ADMIN_ID else '∞'}</b>"
        )

        return True

    except Exception as exc:
        log.exception(
            "Generation failed for user_id=%s",
            user_id,
        )

        await status_message.edit_text(
            "❌ Не удалось создать изображение.\n\n"
            "Попробуй изменить описание. Если ошибка повторяется, "
            "проверь Render Logs.\n\n"
            f"<code>{html.escape(str(exc)[:700])}</code>"
        )

        return False


async def create_stars_invoice(
    message,
    prompt,
    kind,
    image_bytes=None,
):
    payload = "mp:" + uuid.uuid4().hex

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
            title="MarketPhoto AI — генерация",
            description="Одна генерация или редактирование фотографии",
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
        db.execute(
            "DELETE FROM orders WHERE payload=?",
            (payload,),
        )
        db.commit()
        log.exception("Could not create Stars invoice")

        await message.answer(
            "Не удалось создать счёт Telegram Stars. "
            "Проверь настройки платежей и Render Logs."
        )


async def start_generation(
    message,
    prompt,
    kind="text",
    image_bytes=None,
):
    user_id = message.from_user.id

    if prompt_is_disallowed(prompt):
        await message.answer(
            "Я не могу помогать с раздеванием реальных людей. "
            "Можно изменить одежду, цвет, фасон, причёску, "
            "освещение или фон."
        )
        return

    # Admin bypasses the daily limit and payment.
    if user_id == ADMIN_ID:
        async with generation_lock:
            await send_generated(
                message,
                prompt,
                kind,
                image_bytes,
            )
        return

    if used_today(user_id) < FREE_LIMIT:
        async with generation_lock:
            # Recheck after waiting for the lock
            if used_today(user_id) < FREE_LIMIT:
                success = await send_generated(
                    message,
                    prompt,
                    kind,
                    image_bytes,
                )

                if success:
                    use_one_free_generation(user_id)

                return

    await create_stars_invoice(
        message,
        prompt,
        kind,
        image_bytes,
    )


# =========================================================
# START / MENU / COMMANDS
# =========================================================

@dp.message(CommandStart())
async def cmd_start(message: Message):
    register_user(message.from_user)

    await message.answer(
        "👋 <b>MarketPhoto AI</b>\n\n"
        "🎨 Генерация изображений по описанию\n"
        "🖼 Редактирование загруженных фотографий\n"
        f"🎁 {FREE_LIMIT} бесплатных генераций ежедневно\n"
        "⭐ Дополнительные генерации за Telegram Stars\n"
        "❌ Кнопка «Брак» под каждым результатом\n\n"
        "Администратор имеет безлимит.\n"
        "Используй редактирование ответственно и не создавай "
        "интимные изображения реальных людей без их согласия.",
        reply_markup=main_menu(),
    )


@dp.message(Command("help"))
async def cmd_help(message: Message):
    await message.answer(
        "Как пользоваться:\n\n"
        "1. Напиши описание, чтобы создать изображение.\n"
        "2. Чтобы отредактировать фото, отправь изображение "
        "и следующим сообщением опиши изменения.\n"
        "3. После бесплатного лимита бот предложит оплату Stars.\n"
        "4. Если изображение испорчено, нажми «Брак» под ним.\n\n"
        "Команда /cancel отменяет ожидание фото."
    )


@dp.message(Command("cancel"))
async def cmd_cancel(message: Message):
    db.execute(
        "DELETE FROM pending_edits WHERE user_id=?",
        (message.from_user.id,),
    )
    db.commit()

    await message.answer(
        "Ожидание фотографии отменено.",
        reply_markup=main_menu(),
    )


@dp.message(Command("admin"))
async def cmd_admin(message: Message):
    if message.from_user.id != ADMIN_ID:
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

    revenue = db.execute(
        "SELECT COALESCE(SUM(stars), 0) AS n FROM payments WHERE refunded=0"
    ).fetchone()["n"]

    await message.answer(
        "🛠 <b>Панель администратора</b>\n\n"
        f"Пользователей: {users}\n"
        f"Генераций: {generations}\n"
        f"Ожидающих жалоб: {complaints}\n"
        f"Stars по не возвращённым платежам: {revenue}\n\n"
        "Ваш лимит генераций: ∞"
    )


@dp.callback_query(F.data == "generate")
async def menu_generate(call: CallbackQuery):
    await call.answer()
    await call.message.answer(
        "Напиши описание изображения, которое хочешь создать."
    )


@dp.callback_query(F.data == "edit")
async def menu_edit(call: CallbackQuery):
    await call.answer()
    await call.message.answer(
        "Отправь фотографию, которую хочешь изменить. "
        "Затем напиши, что именно нужно изменить."
    )


@dp.callback_query(F.data == "limit")
async def menu_limit(call: CallbackQuery):
    await call.answer()

    user_id = call.from_user.id

    if user_id == ADMIN_ID:
        await call.message.answer(
            "👑 Ваш лимит: <b>без ограничений</b>."
        )
    else:
        await call.message.answer(
            f"Сегодня использовано: {used_today(user_id)}/{FREE_LIMIT}\n"
            f"Осталось: {free_left(user_id)}"
        )


@dp.callback_query(F.data == "buy")
async def menu_buy(call: CallbackQuery):
    await call.answer()

    if call.from_user.id == ADMIN_ID:
        await call.message.answer(
            "Для администратора все генерации бесплатны."
        )
    else:
        await call.message.answer(
            f"После бесплатного лимита одна генерация "
            f"стоит {PRICE_STARS} Telegram Stars. "
            "Напиши свой запрос, и бот предложит оплату."
        )


# =========================================================
# PHOTO UPLOAD AND TEXT PROMPTS
# =========================================================

@dp.message(F.photo)
async def receive_photo(message: Message):
    register_user(message.from_user)

    photo = message.photo[-1]

    try:
        telegram_file = await bot.get_file(photo.file_id)

        buffer = io.BytesIO()

        await bot.download_file(
            telegram_file.file_path,
            destination=buffer,
        )

        raw_image = buffer.getvalue()

        if len(raw_image) > 10 * 1024 * 1024:
            await message.answer(
                "Фото слишком большое. Отправь изображение до 10 МБ."
            )
            return

        # Store uploaded image until the user's instruction arrives.
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
                raw_image,
                int(time.time()),
            ),
        )
        db.commit()

        await message.answer(
            "📷 Фото получено!\n\n"
            "Теперь напиши, что изменить: одежду, причёску, "
            "фон, освещение, цвета или художественный стиль."
        )

    except Exception:
        log.exception("Failed to download uploaded photo")
        await message.answer(
            "Не удалось загрузить фото. Попробуй отправить его ещё раз."
        )


@dp.message(F.text)
async def receive_text(message: Message):
    text = message.text.strip()

    if not text or text.startswith("/"):
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
            text,
            kind="edit",
            image_bytes=image_bytes,
        )

    else:
        await start_generation(
            message,
            text,
            kind="text",
        )


# =========================================================
# TELEGRAM STARS PAYMENTS
# =========================================================

@dp.pre_checkout_query()
async def pre_checkout(pre_checkout_query: PreCheckoutQuery):
    payload = pre_checkout_query.invoice_payload

    order = db.execute(
        "SELECT * FROM orders WHERE payload=?",
        (payload,),
    ).fetchone()

    if (
        not order
        or order["user_id"] != pre_checkout_query.from_user.id
    ):
        await pre_checkout_query.answer(
            ok=False,
            error_message="Заказ не найден или принадлежит другому пользователю.",
        )
        return

    await pre_checkout_query.answer(ok=True)


async def refund_stars(charge_id, user_id):
    """
    Uses Telegram's Stars refund method.
    The call succeeds before the database is marked refunded.
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

    order = db.execute(
        "SELECT * FROM orders WHERE payload=?",
        (payload,),
    ).fetchone()

    if not order or order["user_id"] != user_id:
        log.error(
            "Payment received without a valid order: user=%s payload=%s",
            user_id,
            payload,
        )

        try:
            await refund_stars(charge_id, user_id)
            await message.answer(
                "Заказ не найден. Оплата возвращена автоматически."
            )
        except Exception:
            log.exception("Could not refund orphaned Stars payment")
            await bot.send_message(
                ADMIN_ID,
                "⚠️ Получен платёж без найденного заказа.\n"
                f"User ID: <code>{user_id}</code>\n"
                f"Charge ID: <code>{html.escape(charge_id)}</code>",
            )

        return

    db.execute(
        """
        INSERT OR IGNORE INTO payments(
            charge_id, user_id, payload, stars, refunded, created_at
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

    await message.answer("⭐ Оплата получена. Создаю изображение...")

    async with generation_lock:
        ok = await send_generated(
            message,
            order["prompt"],
            kind=order["kind"],
            image_bytes=order["image_bytes"],
            charge_id=charge_id,
            stars_paid=payment.total_amount,
        )

    if ok:
        db.execute(
            "DELETE FROM orders WHERE payload=?",
            (payload,),
        )
        db.commit()
        return

    # Generation failed after payment: attempt automatic refund.
    try:
        await refund_stars(charge_id, user_id)

        await message.answer(
            "Платное создание не удалось, поэтому Stars возвращены."
        )

    except Exception:
        log.exception(
            "Automatic refund failed for charge_id=%s",
            charge_id,
        )

        await message.answer(
            "Генерация не удалась, а автоматическое возвращение Stars "
            "не прошло. Администратор проверит платёж вручную."
        )

        await bot.send_message(
            ADMIN_ID,
            "⚠️ Не удалось автоматически вернуть Stars.\n"
            f"User ID: <code>{user_id}</code>\n"
            f"Charge ID: <code>{html.escape(charge_id)}</code>\n"
            f"Количество Stars: {payment.total_amount}",
        )


# =========================================================
# DEFECT REPORT BUTTON
# =========================================================

@dp.callback_query(F.data.startswith("defect:"))
async def defect_button(call: CallbackQuery):
    try:
        generation_id = int(call.data.split(":")[1])
    except (ValueError, IndexError):
        await call.answer("Некорректный запрос.", show_alert=True)
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
            "Жалобу на это изображение уже отправлено.",
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

    await call.answer("Жалобу отправлено администратору.")

    username = (
        "@" + call.from_user.username
        if call.from_user.username
        else "без username"
    )

    caption = (
        f"🚨 <b>Жалоба на изображение #{generation_id}</b>\n\n"
        f"Жалоба: <code>{complaint_id}</code>\n"
        f"Пользователь: {html.escape(username)}\n"
        f"User ID: <code>{call.from_user.id}</code>\n"
        f"Тип: {html.escape(generation['kind'])}\n"
        f"Запрос: {html.escape(generation['prompt'][:500])}\n"
        f"Оплачено Stars: {generation['stars_paid']}"
    )

    has_paid = bool(
        generation["charge_id"]
        and generation["stars_paid"] > 0
        and not generation["refunded"]
    )

    try:
        await bot.send_photo(
            chat_id=ADMIN_ID,
            photo=generation["photo_file_id"],
            caption=caption,
            reply_markup=admin_complaint_keyboard(
                complaint_id,
                has_paid,
            ),
        )

    except Exception:
        log.exception("Could not forward complaint to admin")

        await bot.send_message(
            ADMIN_ID,
            caption,
            reply_markup=admin_complaint_keyboard(
                complaint_id,
                has_paid,
            ),
        )


# =========================================================
# ADMIN COMPLAINT HANDLING
# =========================================================

async def resolve_complaint(call, decision):
    if call.from_user.id != ADMIN_ID:
        await call.answer(
            "Только администратор может обрабатывать жалобы.",
            show_alert=True,
        )
        return

    complaint_id = int(call.data.split(":")[1])

    complaint = db.execute(
        "SELECT * FROM complaints WHERE id=?",
        (complaint_id,),
    ).fetchone()

    if not complaint:
        await call.answer("Жалоба не найдена.", show_alert=True)
        return

    if complaint["status"] not in ("pending",):
        await call.answer(
            "Жалоба уже обработана.",
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
        "UPDATE generations SET complaint_status=? WHERE id=?",
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
            "❌ Администратор проверил жалобу, но не подтвердил брак.",
        )

    has_paid = bool(
        decision == "accepted"
        and generation
        and generation["charge_id"]
        and generation["stars_paid"] > 0
        and not generation["refunded"]
    )

    try:
        if has_paid:
            await call.message.edit_reply_markup(
                reply_markup=refund_only_keyboard(complaint_id)
            )
        else:
            await call.message.edit_reply_markup(
                reply_markup=None
            )
    except Exception:
        log.exception("Could not update complaint keyboard")

    await call.answer("Решение сохранено.")


@dp.callback_query(F.data.startswith("accept:"))
async def accept_complaint(call: CallbackQuery):
    await resolve_complaint(call, "accepted")


@dp.callback_query(F.data.startswith("reject:"))
async def reject_complaint(call: CallbackQuery):
    await resolve_complaint(call, "rejected")


@dp.callback_query(F.data.startswith("refund:"))
async def refund_complaint(call: CallbackQuery):
    if call.from_user.id != ADMIN_ID:
        await call.answer(
            "Только администратор может возвращать Stars.",
            show_alert=True,
        )
        return

    complaint_id = int(call.data.split(":")[1])

    complaint = db.execute(
        "SELECT * FROM complaints WHERE id=?",
        (complaint_id,),
    ).fetchone()

    if not complaint:
        await call.answer("Жалоба не найдена.", show_alert=True)
        return

    if complaint["status"] not in ("pending", "accepted"):
        await call.answer(
            "Эту жалобу нельзя использовать для возврата.",
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
            "У этой генерации нет связанного платежа Stars.",
            show_alert=True,
        )
        return

    payment = db.execute(
        "SELECT * FROM payments WHERE charge_id=?",
        (generation["charge_id"],),
    ).fetchone()

    if not payment or payment["refunded"] or generation["refunded"]:
        await call.answer(
            "Платёж уже возвращён или не найден.",
            show_alert=True,
        )
        return

    try:
        await refund_stars(
            generation["charge_id"],
            generation["user_id"],
        )

        db.execute(
            "UPDATE complaints SET status='refunded' WHERE id=?",
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

        await call.message.edit_reply_markup(reply_markup=None)
        await call.answer("Stars возвращены.")

    except Exception as exc:
        log.exception("Stars refund failed")

        await call.answer(
            f"Telegram не выполнил возврат: {str(exc)[:150]}",
            show_alert=True,
        )


# =========================================================
# START BOT
# =========================================================

async def main():
    init_db()

    log.info(
        "Starting MarketPhoto AI. Model=%s",
        MODEL,
    )

    try:
        await dp.start_polling(bot)

    finally:
        if http_session and not http_session.closed:
            await http_session.close()

        await bot.session.close()
        db.close()


if __name__ == "__main__":
    asyncio.run(main())


