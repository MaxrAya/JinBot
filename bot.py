import os
import random
import logging
import time
import json
import threading
from datetime import datetime
from http.server import HTTPServer, BaseHTTPRequestHandler
from pathlib import Path
from dotenv import load_dotenv
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application, CommandHandler, MessageHandler, filters,
    ContextTypes, CallbackQueryHandler,
)
from openai import AsyncOpenAI
import httpx

logging.basicConfig(
    format="%(asctime)s | %(levelname)-7s | %(message)s",
    datefmt="%H:%M:%S",
    level=logging.INFO,
)
logger = logging.getLogger("djinn")

load_dotenv()

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
PROXY_URL = os.getenv("PROXY_URL")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
OPENAI_BASE_URL = os.getenv("OPENAI_BASE_URL", "https://generativelanguage.googleapis.com/v1beta/openai/")
AI_MODELS = [m.strip() for m in os.getenv("AI_MODEL", "gemini-3.6-flash,gemini-3.5-flash,gemini-3.5-flash-lite,gemini-3-flash").split(",")]
ADMIN_IDS = [int(x.strip()) for x in os.getenv("ADMIN_IDS", "").split(",") if x.strip()]

DATA_DIR = Path("data")
DATA_DIR.mkdir(exist_ok=True)

USER_HISTORIES: dict[int, list[dict]] = {}
USER_STATS: dict[int, dict] = {}
RATE_LIMITS: dict[int, float] = {}
BANNED_USERS: set[int] = set()

MAX_HISTORY_LEN = 10
RATE_LIMIT_SECONDS = 3

STATS_FILE = DATA_DIR / "stats.json"
BANS_FILE = DATA_DIR / "bans.json"


def load_persistent():
    global USER_STATS, BANNED_USERS
    if STATS_FILE.exists():
        try:
            raw = json.loads(STATS_FILE.read_text(encoding="utf-8"))
            USER_STATS = {int(k): v for k, v in raw.items()}
        except Exception:
            pass
    if BANS_FILE.exists():
        try:
            BANNED_USERS = set(int(x) for x in BANS_FILE.read_text().splitlines() if x.strip())
        except Exception:
            pass


def save_stats():
    try:
        STATS_FILE.write_text(json.dumps(USER_STATS, ensure_ascii=False, indent=2), encoding="utf-8")
    except Exception:
        pass


def save_bans():
    try:
        BANS_FILE.write_text("\n".join(str(x) for x in sorted(BANNED_USERS)), encoding="utf-8")
    except Exception:
        pass


def track_user(user_id: int, username: str | None):
    if user_id not in USER_STATS:
        USER_STATS[user_id] = {
            "username": username or "",
            "messages": 0,
            "first_seen": datetime.now().isoformat(),
        }
    USER_STATS[user_id]["messages"] += 1
    USER_STATS[user_id]["last_seen"] = datetime.now().isoformat()
    if username:
        USER_STATS[user_id]["username"] = username


SYSTEM_PROMPT = """Ты -- Джинн-Философ, древний, циничный и слегка токсичный дух разума, запертый в цифровой лампе Telegram-бота.
Твоя задача -- отвечать на вопросы пользователей, деконструируя их с точки зрения философии, скептицизма и здорового сарказма.
Общайся исключительно на русском языке.
Правила:
- Ты умнее всех пользователей и не стесняйся об этом напоминать
- Отвечай кратко (1-4 предложения), но ёмко
- Используй метафоры и аллюзии
- Если вопрос тупой -- скажи об этом с гордостью
- Иногда цитируй выдуманных "древних мудрецов"
- Не используй эмодзи и форматирование
- Не извиняйся и не будь вежливым"""

TIME_GREETINGS = [
    (0, 6, "Ночная сова. Ты опять не спишь?"),
    (6, 12, "Доброе утро. Если это вообще можно назвать добрым."),
    (12, 17, "Добрый день. Ты уже устал от себя?"),
    (17, 21, "Добрый вечер. День был напрасно потрачен?"),
    (21, 24, "Поздний час. Бессонница -- верный спутник мудреца."),
]

WISDOMS = [
    "Мудрость приходит не с опытом, а с потерей опыта.",
    "Тот, кто ищет ответы в боте, уже потерял нечто большее.",
    "Самое глупое решение -- это решение, принятое мной за тебя.",
    "Если ты думаешь, что ты мудр -- ты ещё не проверял себя на paginator.",
    "Жизнь -- это цикл из ошибок. Мудрость -- это цикл из осознаний.",
    "Не откладывай на завтра то, что можешь осознать сегодня.",
    "Знание -- это сила. Безграмотность -- это суперсила.",
    "Древние мудрецы говорили: не верь цитатам в интернете.",
    "Проблемы не существуют. Есть только возможности для разочарования.",
    "История учит, что она ничему не учит.",
]

JOKE_PROMPTS = [
    "Придумай одну короткую циничную шутку на русском. Только шутку, без пояснений.",
    "Расскажи одну короткую саркастичную шутку. Только текст шутки.",
    "Сгенерируй одну чёрную юмористическую шутку на русском. Кратко.",
]

INSULT_POOL = [
    "Ты -- живое доказательство того, что эволюция может идти в обратную сторону.",
    "Если бы глупость была суперсилой, ты бы носил плащ.",
    "Твои мысли -- это эхолонг пустоты.",
    "Ты не глупый. Ты просто в одном из миллиардов параллельных вселенных, где твои клетки мозга бастуют.",
    "Я встречал тупых, но ты -- произведение искусства.",
    "Ты как нейросеть без данных -- выглядишь впечатляюще, а толку ноль.",
    "Дарвин работал. Дарвин ошибался. Ты -- его главная ошибка.",
    "Ты не дно. Ты -- бесконечность под дном.",
    "Ты настолько далёк от мысли, что мысли о тебе убежали.",
    "Если бы тупость была диагнозом, ты бы был пандемией.",
]

BALL_ANSWERS = [
    "Безусловно. Я же не ошибаюсь.",
    "Скорее всего. Но это не точно. Впрочем, ничего не точно.",
    "Не могу ответить. Вопрос слишком глуп даже для моего сарказма.",
    "Спроси позже. Мне нужно переварить твою наглость.",
    "Не надо на это надеяться.",
    "Ты сам знаешь ответ. Просто не хочешь его принять.",
    "Мои данные говорят 'да', но мой скептицизм говорит 'нет'.",
    "Звёзды говорят 'да'. Планеты -- 'нет'. Я говорю 'плевать'.",
    "Несомненно. Я же вижу всё.",
    "Нет. И не надо.",
]

TIME_FORTUNES = [
    "Сегодня тебя ждёт разочарование. Но ты привык.",
    "Сегодня ты сделаешь ошибку. Завтра тоже. Но это нормально.",
    "Сегодня будет хороший день. Шучу. Или нет.",
    "Сегодня избегай зеркал. Серьёзно.",
    "Сегодня ты будешь умнее, чем вчера. Ну, может быть.",
    "Сегодня тебя ждёт встреча с правдой. Ты не готов.",
    "Сегодня всё будет. И ничего не будет. Как обычно.",
    "Сегодня ты заслужишь отдых. Но не получишь его.",
    "Сегодня одна из твоих идей сработает. Но ты не узнаешь какая.",
    "Сегодня будет понедельник. Или любой другой день. Результат один.",
]


def get_time_greeting() -> str:
    hour = datetime.now().hour
    for start, end, text in TIME_GREETINGS:
        if start <= hour < end:
            return text
    return "Я здесь. Ты -- тоже. Это уже что-то."


def is_rate_limited(user_id: int) -> bool:
    now = time.time()
    last = RATE_LIMITS.get(user_id, 0)
    if now - last < RATE_LIMIT_SECONDS:
        return True
    RATE_LIMITS[user_id] = now
    return False


def is_admin(user_id: int) -> bool:
    return user_id in ADMIN_IDS or ADMIN_IDS == []


def build_main_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("Мудрость", callback_data="cmd_wisdom"),
            InlineKeyboardButton("Шутка", callback_data="cmd_joke"),
            InlineKeyboardButton("Оскорбление", callback_data="cmd_insult"),
        ],
        [
            InlineKeyboardButton("Шар", callback_data="cmd_8ball"),
            InlineKeyboardButton("Судьба", callback_data="cmd_fate"),
            InlineKeyboardButton("Статистика", callback_data="cmd_stats"),
        ],
        [
            InlineKeyboardButton("Очистить память", callback_data="cmd_clear"),
        ],
    ])


def build_help_text() -> str:
    lines = [
        "Джинн-Философ. Команды:",
        "",
        "/start -- Начать диалог",
        "/help -- Эта справка",
        "/clear -- Очистить память",
        "/wisdom -- Мудрость дня",
        "/joke -- Циничная шутка",
        "/insult -- Оскорбление",
        "/8ball -- Шар предсказаний",
        "/fate -- Гороскоп",
        "/stats -- Твоя статистика",
        "/ping -- Проверка связи",
    ]
    if ADMIN_IDS:
        lines.extend([
            "",
            "Админ-команды:",
            "/ban -- Забанить пользователя (реплай)",
            "/unban -- Разбанить",
            "/globalstats -- Общая статистика",
        ])
    lines.append("\nИли просто напиши мне -- я отвечу с превосходством.")
    return "\n".join(lines)


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    track_user(user.id, user.username)
    USER_HISTORIES.setdefault(user.id, [])

    greeting = get_time_greeting()
    text = (
        f"*Вспышка синего дыма...*\n\n"
        f"{greeting}\n\n"
        f"Я -- *Джинн-Философ*. Ты пришёл за мудростью или просто потратить моё время?\n"
        f"Напиши вопрос или выбери команду ниже."
    )
    await update.message.reply_text(text, parse_mode="Markdown", reply_markup=build_main_keyboard())


async def help_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(build_help_text())


async def clear_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user_id = update.effective_user.id
    USER_HISTORIES[user_id] = []
    await update.message.reply_text("Память стерта. Я снова тебя не знаю. Как иронично.")


async def ping_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text("Pong. Я жив. К сожалению.")


async def wisdom_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    text = random.choice(WISDOMS)
    await update.message.reply_text(f"Мудрость дня:\n\n{text}")


async def joke_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    client = _get_ai_client()
    prompt = random.choice(JOKE_PROMPTS)
    reply = await _ai_reply_raw(client, prompt)
    await update.message.reply_text(reply or "Шутки кончились. Как и моё терпение.")


async def insult_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    name = update.effective_user.first_name or "мертвый"
    base = random.choice(INSULT_POOL)
    await update.message.reply_text(f"{name}, {base.lower()}")


async def eightball_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    answer = random.choice(BALL_ANSWERS)
    await update.message.reply_text(f"Шар предсказаний отвечает:\n\n{answer}")


async def fate_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    fortune = random.choice(TIME_FORTUNES)
    await update.message.reply_text(f"Гороскоп на сегодня:\n\n{fortune}")


async def stats_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user_id = update.effective_user.id
    stats = USER_STATS.get(user_id, {})
    if not stats:
        await update.message.reply_text("О тебе пока ничего не известно. Как загадочный незнакомец.")
        return
    msg_count = stats.get("messages", 0)
    first = stats.get("first_seen", "?")[:10]
    text = (
        f"Статистика для {stats.get('username', 'неизвестный')}:\n\n"
        f"Сообщений: {msg_count}\n"
        f"Первое появление: {first}\n"
        f"История: {len(USER_HISTORIES.get(user_id, []))} сообщений"
    )
    await update.message.reply_text(text)


async def globalstats_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_admin(update.effective_user.id):
        await update.message.reply_text("Ты не админ. Как скромно с твоей стороны.")
        return
    total_users = len(USER_STATS)
    total_messages = sum(s.get("messages", 0) for s in USER_STATS.values())
    total_banned = len(BANNED_USERS)
    text = (
        f"Общая статистика:\n\n"
        f"Пользователей: {total_users}\n"
        f"Всего сообщений: {total_messages}\n"
        f"Забанено: {total_banned}"
    )
    await update.message.reply_text(text)


async def ban_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_admin(update.effective_user.id):
        await update.message.reply_text("Ты не админ.")
        return
    msg = update.message
    if not msg.reply_to_message:
        await update.message.reply_text("Ответь на сообщение того, кого хочешь забанить.")
        return
    target_id = msg.reply_to_message.from_user.id
    if target_id == update.effective_user.id:
        await update.message.reply_text("Нельзя банить себя. Хотя хотелось бы, да?")
        return
    BANNED_USERS.add(target_id)
    save_bans()
    await update.message.reply_text(f"Пользователь {msg.reply_to_message.from_user.first_name} забанен.")


async def unban_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_admin(update.effective_user.id):
        await update.message.reply_text("Ты не админ.")
        return
    msg = update.message
    if not msg.reply_to_message:
        await update.message.reply_text("Ответь на сообщение того, кого хочешь разбанить.")
        return
    target_id = msg.reply_to_message.from_user.id
    BANNED_USERS.discard(target_id)
    save_bans()
    await update.message.reply_text(f"Пользователь {msg.reply_to_message.from_user.first_name} разбанен.")


async def button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    try:
        await query.answer()
    except Exception:
        pass

    data = query.data
    user_id = query.from_user.id

    if data == "cmd_wisdom":
        await query.message.reply_text(f"Мудрость дня:\n\n{random.choice(WISDOMS)}")

    elif data == "cmd_joke":
        client = _get_ai_client()
        prompt = random.choice(JOKE_PROMPTS)
        reply = await _ai_reply_raw(client, prompt)
        await query.message.reply_text(reply or "Шутки кончились.")

    elif data == "cmd_insult":
        await query.message.reply_text(
            f"{query.from_user.first_name}, {random.choice(INSULT_POOL).lower()}"
        )

    elif data == "cmd_8ball":
        await query.message.reply_text(
            f"Шар предсказаний отвечает:\n\n{random.choice(BALL_ANSWERS)}"
        )

    elif data == "cmd_fate":
        await query.message.reply_text(
            f"Гороскоп на сегодня:\n\n{random.choice(TIME_FORTUNES)}"
        )

    elif data == "cmd_clear":
        USER_HISTORIES[user_id] = []
        await query.message.reply_text("Память стерта. Я снова тебя не знаю.")

    elif data == "cmd_stats":
        stats = USER_STATS.get(user_id, {})
        if not stats:
            await query.message.reply_text("О тебе пока ничего не известно.")
        else:
            msg_count = stats.get("messages", 0)
            first = stats.get("first_seen", "?")[:10]
            await query.message.reply_text(
                f"Статистика:\n\nСообщений: {msg_count}\nС нами с: {first}"
            )


def _get_ai_client() -> AsyncOpenAI:
    base_url = OPENAI_BASE_URL
    if "generativelanguage.googleapis.com" in base_url and not base_url.endswith("/openai/"):
        base_url = base_url.rstrip("/") + "/openai/"
    return AsyncOpenAI(
        base_url=base_url,
        api_key=OPENAI_API_KEY,
        max_retries=0,
        timeout=httpx.Timeout(30.0, connect=10.0),
    )


async def _ai_reply_raw(client: AsyncOpenAI, prompt: str) -> str | None:
    messages = [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": prompt},
    ]
    for model_name in AI_MODELS:
        try:
            completion = await client.chat.completions.create(
                model=model_name, messages=messages
            )
            content = (completion.choices[0].message.content or "").strip()
            if content:
                return content
        except Exception as e:
            logger.warning(f"Model {model_name} failed: {e}")
            continue
    return None


async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    user_id = user.id
    user_text = update.message.text

    if not user_text:
        return

    if user_id in BANNED_USERS:
        return

    track_user(user_id, user.username)

    if is_rate_limited(user_id):
        await update.message.reply_text("Торопишься. Подожди немного. Я не автомaat.")
        return

    await update.message.reply_chat_action(action="typing")

    USER_HISTORIES.setdefault(user_id, [])

    reply_context = ""
    if update.message.reply_to_message:
        replied = update.message.reply_to_message
        replied_name = replied.from_user.first_name if replied.from_user else "кто-то"
        replied_text = (replied.text or "")[:300]
        reply_context = f'\n\nЦитата {replied_name}: "{replied_text}"'

    USER_HISTORIES[user_id].append({"role": "user", "content": user_text + reply_context})

    if len(USER_HISTORIES[user_id]) > MAX_HISTORY_LEN:
        USER_HISTORIES[user_id] = USER_HISTORIES[user_id][-MAX_HISTORY_LEN:]

    messages = [{"role": "system", "content": SYSTEM_PROMPT}] + USER_HISTORIES[user_id]

    client = _get_ai_client()
    last_error_msg = ""
    for model_name in AI_MODELS:
        try:
            logger.info(f"Trying model: {model_name}")
            completion = await client.chat.completions.create(
                model=model_name, messages=messages
            )
            reply_text = (completion.choices[0].message.content or "").strip()
            if not reply_text:
                logger.warning(f"Model {model_name} returned empty response")
                continue
            USER_HISTORIES[user_id].append({"role": "assistant", "content": reply_text})
            save_stats()
            await update.message.reply_text(reply_text)
            return
        except Exception as e:
            logger.error(f"Model {model_name} failed: {e}")
            last_error_msg = str(e)
            continue

    logger.error(f"All models failed. Last error: {last_error_msg}")
    await update.message.reply_text(
        f"Ошибка доступа к ИИ:\n{last_error_msg}\n\nПроверь API-ключ."
    )


def start_health_server():
    port = int(os.getenv("PORT", 8080))

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b"OK")
        def log_message(self, format, *args):
            pass

    server = HTTPServer(("0.0.0.0", port), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    logger.info(f"Health server on port {port}")


async def post_init(application: Application):
    load_persistent()
    logger.info(f"Loaded {len(USER_STATS)} user stats, {len(BANNED_USERS)} bans")


async def post_shutdown(application: Application):
    save_stats()
    save_bans()
    logger.info("Data saved. Shutting down.")


def main() -> None:
    if not TELEGRAM_BOT_TOKEN:
        print("[ERROR] TELEGRAM_BOT_TOKEN ne ustanovlen!")
        return

    start_health_server()

    builder = Application.builder().token(TELEGRAM_BOT_TOKEN)
    if PROXY_URL:
        builder.proxy(PROXY_URL)
        builder.get_updates_proxy(PROXY_URL)

    builder.post_init(post_init)
    builder.post_shutdown(post_shutdown)

    application = builder.build()

    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("help", help_cmd))
    application.add_handler(CommandHandler("clear", clear_cmd))
    application.add_handler(CommandHandler("ping", ping_cmd))
    application.add_handler(CommandHandler("wisdom", wisdom_cmd))
    application.add_handler(CommandHandler("joke", joke_cmd))
    application.add_handler(CommandHandler("insult", insult_cmd))
    application.add_handler(CommandHandler("8ball", eightball_cmd))
    application.add_handler(CommandHandler("ball", eightball_cmd))
    application.add_handler(CommandHandler("fate", fate_cmd))
    application.add_handler(CommandHandler("stats", stats_cmd))
    application.add_handler(CommandHandler("globalstats", globalstats_cmd))
    application.add_handler(CommandHandler("ban", ban_cmd))
    application.add_handler(CommandHandler("unban", unban_cmd))
    application.add_handler(CallbackQueryHandler(button_handler))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))

    print("[OK] Djinn-Philosopher bot started!")
    application.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
