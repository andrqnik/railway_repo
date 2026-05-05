import os
import time
import logging
from datetime import time as dt_time, datetime
from zoneinfo import ZoneInfo
from dotenv import load_dotenv
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    CallbackQueryHandler,
    filters,
    ContextTypes,
)
from config import Config
from ai_parser import AIParser
from clickup_client import ClickUpClient
from whisper_client import WhisperClient

load_dotenv()

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

config = Config()
ai_parser = AIParser(config.anthropic_api_key)
clickup = ClickUpClient(config.clickup_api_key, config.clickup_list_id)
whisper = WhisperClient(config.openai_api_key) if config.openai_api_key else None

DUBAI_TZ = ZoneInfo("Asia/Dubai")

PRIORITY_LABELS = {
    1: "🔴 Срочный",
    2: "🟠 Высокий",
    3: "🟡 Обычный",
    4: "🔵 Низкий",
}

MONTHS_RU = ["января", "февраля", "марта", "апреля", "мая", "июня",
             "июля", "августа", "сентября", "октября", "ноября", "декабря"]

# In-memory cache for ClickUp Space targets (folders + lists). Refreshed every 10 min.
_targets_cache = {"data": None, "expires_at": 0.0}
TARGETS_CACHE_TTL_SEC = 600


def _plural_tasks(n: int) -> str:
    if n % 10 == 1 and n % 100 != 11:
        return "задача"
    elif 2 <= n % 10 <= 4 and not (12 <= n % 100 <= 14):
        return "задачи"
    else:
        return "задач"


def _format_date_short(dt: datetime) -> str:
    return f"{dt.day} {MONTHS_RU[dt.month - 1]}"


def _escape(text: str) -> str:
    return (
        str(text)
        .replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
    )


def _truncate(text: str, limit: int = 200) -> str:
    return text if len(text) <= limit else text[:limit] + "..."


def _format_inbox_list(tasks: list, header: str) -> str:
    lines = [header]
    for i, task in enumerate(tasks, 1):
        name = task.get("name", "?")
        url = task.get("url", "")
        due = task.get("due_date")
        due_str = ""
        if due:
            try:
                due_dt = datetime.fromtimestamp(int(due) / 1000)
                due_str = f" · 📅 {_format_date_short(due_dt)}"
            except (ValueError, TypeError):
                pass
        if url:
            line = f'{i}. <a href="{url}">{_escape(name)}</a>{due_str}'
        else:
            line = f'{i}. {_escape(name)}{due_str}'
        lines.append(line)
    return "\n".join(lines)


# ─────────────────────────────────────────────────────────────────────────────
# Targets (Step 4) — fetched on demand, cached in memory
# ─────────────────────────────────────────────────────────────────────────────

async def _get_targets() -> dict:
    """Returns {"folders": [...]} from cache or fresh fetch.

    Hard-scoped to CLICKUP_ALLOWED_FOLDER_IDS only. The Inbox list
    (CLICKUP_LIST_ID) is always excluded from targets.
    """
    now = time.time()
    if _targets_cache["data"] and now < _targets_cache["expires_at"]:
        return _targets_cache["data"]

    folder_ids = config.allowed_folder_id_list
    if not folder_ids:
        return {"folders": []}

    data = await clickup.get_allowed_targets(
        allowed_folder_ids=folder_ids,
        exclude_list_ids=[config.clickup_list_id],
    )
    _targets_cache["data"] = data
    _targets_cache["expires_at"] = now + TARGETS_CACHE_TTL_SEC
    return data


def _allowed_list_ids(targets: dict) -> set:
    """Set of all valid target list IDs (for hard guard before create)."""
    ids = set()
    for folder in targets.get("folders", []):
        for lst in folder.get("lists", []):
            ids.add(lst["id"])
    return ids


def _build_preview_lines(task_data: dict, file_name: str = None, voice_transcript: str = None) -> list:
    """Build preview text lines (used for initial preview AND cancel-back-to-preview)."""
    lines = []
    if voice_transcript:
        lines.append(f"🎙 <i>«{_escape(_truncate(voice_transcript))}»</i>\n")
    lines.append("📝 <b>Распознал:</b>\n")
    lines.append(f"📌 <b>{_escape(task_data['name'])}</b>")
    if task_data.get("due_date_formatted"):
        lines.append(f"📅 {_escape(task_data['due_date_formatted'])}")
    priority = task_data.get("priority", 3)
    lines.append(f"⚡ {PRIORITY_LABELS.get(priority, '🟡 Обычный')}")
    if task_data.get("description"):
        desc = task_data["description"]
        if len(desc) > 120:
            desc = desc[:120] + "..."
        lines.append(f"\n{_escape(desc)}")
    if file_name:
        lines.append(f"\n📎 {_escape(file_name)}")
    lines.append("\n<i>Пока ничего не создано. Что делаем?</i>")
    return lines


def _initial_keyboard(msg_id: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[
        InlineKeyboardButton("📥 В инбокс", callback_data=f"defer:{msg_id}"),
        InlineKeyboardButton("✅ Разобрать", callback_data=f"now:{msg_id}"),
    ]])


# ─────────────────────────────────────────────────────────────────────────────
# Commands
# ─────────────────────────────────────────────────────────────────────────────

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_html(
        "👋 <b>Привет!</b> Я разбираю твои сообщения и помогаю с задачами в ClickUp.\n\n"
        "Напиши задачу текстом, пришли голосовое или прикрепи файл — я разберу и предложу:\n"
        "📥 <b>В инбокс</b> — отложить, разберём вместе утром\n"
        "✅ <b>Разобрать</b> — выбрать папку и список прямо сейчас\n\n"
        "Каждое утро в 10:00 (Дубай) пришлю список того что в инбоксе.\n\n"
        "<b>Команды:</b>\n"
        "/inbox — показать инбокс прямо сейчас\n"
        "/help — подробная справка"
    )


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    voice_status = "✅ работают" if whisper else "⚠ не настроены (нужен OPENAI_API_KEY)"
    targets_status = (
        f"✅ настроен ({len(config.allowed_folder_id_list)} папок)"
        if config.allowed_folder_id_list else
        "⚠ не настроен (нужен CLICKUP_ALLOWED_FOLDER_IDS)"
    )
    await update.message.reply_html(
        "<b>Как пользоваться:</b>\n\n"
        "1. Напиши задачу текстом, пришли голосовое или приложи файл\n"
        "2. Я разберу — покажу превью с дедлайном и приоритетом\n"
        "3. Выбери: 📥 В инбокс или ✅ Разобрать (выбор списка)\n\n"
        f"<b>Голосовые:</b> {voice_status}\n"
        f"<b>Target picker:</b> {targets_status}\n\n"
        "<b>Команды:</b>\n"
        "/inbox — показать что не разобрано\n"
        "/myid — узнать chat_id (для утренних дайджестов)\n"
        "/test_morning — проверить утренний дайджест прямо сейчас\n\n"
        "<b>Утренний дайджест:</b>\n"
        "Каждый день в 10:00 по Дубаю присылаю список того что в инбоксе. "
        "Если инбокс пустой — молчу."
    )


async def inbox_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = await update.message.reply_text("⏳ Забираю инбокс из ClickUp...")
    try:
        tasks = await clickup.get_inbox_tasks()
        if not tasks:
            await msg.edit_text("📭 Инбокс пуст. Чистый старт.")
            return
        n = len(tasks)
        header = f"📥 <b>В инбоксе {n} {_plural_tasks(n)}:</b>\n"
        text = _format_inbox_list(tasks, header)
        await msg.edit_text(text, parse_mode="HTML", disable_web_page_preview=True)
    except Exception as e:
        logger.error(f"Inbox fetch error: {e}", exc_info=True)
        await msg.edit_text(f"❌ Не удалось получить инбокс.\n\nОшибка: {str(e)}")


async def myid_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    await update.message.reply_html(
        f"Твой <b>chat_id</b>: <code>{chat_id}</code>\n\n"
        f"Чтобы включить утренние дайджесты, добавь в Railway Variables:\n"
        f"<code>TELEGRAM_USER_CHAT_ID={chat_id}</code>"
    )


async def morning_inbox_digest(context: ContextTypes.DEFAULT_TYPE):
    if not config.telegram_user_chat_id:
        logger.warning("TELEGRAM_USER_CHAT_ID not set — skipping morning digest")
        return
    try:
        tasks = await clickup.get_inbox_tasks()
        if not tasks:
            logger.info("Morning digest: inbox empty, staying silent")
            return
        n = len(tasks)
        header = f"☀️ <b>Доброе утро.</b>\nВ инбоксе {n} {_plural_tasks(n)} — разберём?\n"
        text = _format_inbox_list(tasks, header)
        text += "\n\n<i>Открой ClickUp и пройди по списку. Или напиши /inbox чтобы посмотреть позже.</i>"
        await context.bot.send_message(
            chat_id=int(config.telegram_user_chat_id),
            text=text,
            parse_mode="HTML",
            disable_web_page_preview=True,
        )
        logger.info(f"Morning digest sent: {n} tasks")
    except Exception as e:
        logger.error(f"Morning digest error: {e}", exc_info=True)


async def test_morning_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not config.telegram_user_chat_id:
        await update.message.reply_html(
            "⚠ Сначала добавь <code>TELEGRAM_USER_CHAT_ID</code> в Railway Variables.\n"
            "Узнать chat_id: /myid"
        )
        return
    await update.message.reply_text("🧪 Запускаю утренний дайджест прямо сейчас...")
    await morning_inbox_digest(context)


# ─────────────────────────────────────────────────────────────────────────────
# Capture pipeline (text + voice → preview with buttons)
# ─────────────────────────────────────────────────────────────────────────────

async def _show_task_preview(
    message,
    context: ContextTypes.DEFAULT_TYPE,
    text: str,
    status_msg,
    voice_transcript: str = None,
    file_content: bytes = None,
    file_name: str = None,
):
    task_data = await ai_parser.parse(text)

    cache_key = f"task_{message.message_id}"
    context.user_data[cache_key] = {
        "task_data": task_data,
        "file_content": file_content,
        "file_name": file_name,
        "voice_transcript": voice_transcript,
    }

    lines = _build_preview_lines(task_data, file_name=file_name, voice_transcript=voice_transcript)
    keyboard = _initial_keyboard(str(message.message_id))

    await status_msg.edit_text(
        "\n".join(lines),
        parse_mode="HTML",
        reply_markup=keyboard,
    )


async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Text messages with optional document/photo attachment."""
    message = update.message
    text = message.text or message.caption or ""

    if not text.strip():
        await message.reply_text(
            "📝 Пожалуйста, напиши описание задачи.\n"
            "Или пришли голосовое — я его расшифрую."
        )
        return

    status_msg = await message.reply_text("⏳ Разбираю задачу...")

    try:
        file_content = None
        file_name = None

        if message.document:
            file = await message.document.get_file()
            file_content = bytes(await file.download_as_bytearray())
            file_name = message.document.file_name
        elif message.photo:
            photo = message.photo[-1]
            file = await photo.get_file()
            file_content = bytes(await file.download_as_bytearray())
            file_name = f"photo_{photo.file_id}.jpg"

        await _show_task_preview(
            message, context, text, status_msg,
            file_content=file_content,
            file_name=file_name,
        )

    except Exception as e:
        logger.error(f"Error parsing task: {e}", exc_info=True)
        await status_msg.edit_text(
            f"❌ Не удалось разобрать задачу.\n\n"
            f"Ошибка: {str(e)}\n\n"
            f"Попробуй сформулировать иначе."
        )


async def handle_voice(update: Update, context: ContextTypes.DEFAULT_TYPE):
    message = update.message

    if not whisper:
        await message.reply_html(
            "🎙 Голосовые пока не настроены.\n"
            "Добавь <code>OPENAI_API_KEY</code> в Railway Variables — взять можно "
            "на <a href=\"https://platform.openai.com/api-keys\">platform.openai.com/api-keys</a>."
        )
        return

    status_msg = await message.reply_text("🎙 Слушаю голосовое...")

    try:
        voice_obj = message.voice or message.audio
        file = await voice_obj.get_file()
        audio_bytes = bytes(await file.download_as_bytearray())

        await status_msg.edit_text("🎙 Транскрибирую через Whisper...")

        text = await whisper.transcribe(audio_bytes, file_name="voice.ogg")

        if not text.strip():
            await status_msg.edit_text(
                "🎙 Не получилось разобрать — пустая транскрипция.\n"
                "Попробуй ещё раз чуть чётче."
            )
            return

        await status_msg.edit_text(
            f"🎙 <i>«{_escape(_truncate(text))}»</i>\n\n⏳ Разбираю задачу...",
            parse_mode="HTML",
        )

        await _show_task_preview(
            message, context, text, status_msg,
            voice_transcript=text,
        )

    except Exception as e:
        logger.error(f"Voice handling error: {e}", exc_info=True)
        await status_msg.edit_text(
            f"❌ Не удалось обработать голосовое.\n\n"
            f"Ошибка: {str(e)}"
        )


# ─────────────────────────────────────────────────────────────────────────────
# Callback dispatcher (defer / now / folder picker / list picker / back / cancel)
# ─────────────────────────────────────────────────────────────────────────────

async def handle_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    parts = query.data.split(":")
    if not parts:
        await query.edit_message_text("⚠ Неверный callback.")
        return
    action = parts[0]

    try:
        if action == "defer":
            await _do_defer(query, context, parts[1])
        elif action == "now":
            await _show_folder_picker(query, context, parts[1])
        elif action == "f":
            await _show_list_picker(query, context, folder_id=parts[1], msg_id=parts[2])
        elif action == "l":
            await _create_in_target(query, context, list_id=parts[1], msg_id=parts[2])
        elif action == "b":
            await _show_folder_picker(query, context, parts[1])
        elif action == "c":
            await _restore_initial(query, context, parts[1])
        else:
            await query.edit_message_text("⚠ Неизвестное действие.")
    except IndexError:
        await query.edit_message_text("⚠ Неверный callback.")


async def _do_defer(query, context, msg_id):
    cache_key = f"task_{msg_id}"
    cached = context.user_data.get(cache_key)
    if not cached:
        await query.edit_message_text(
            "⚠ Данные о задаче потерялись (бот мог перезапуститься).\n"
            "Пришли задачу ещё раз."
        )
        return

    await query.edit_message_text("⏳ Сохраняю в инбокс...")

    try:
        task = await clickup.create_task(
            task_data=cached["task_data"],
            file_content=cached["file_content"],
            file_name=cached["file_name"],
        )
        await _send_creation_confirmation(query, cached, task, header="📥 <b>Сохранил в инбокс. Разберём утром.</b>\n")
        context.user_data.pop(cache_key, None)
    except Exception as e:
        logger.error(f"Defer error: {e}", exc_info=True)
        await query.edit_message_text(f"❌ Не удалось создать: {str(e)}")


async def _show_folder_picker(query, context, msg_id):
    cache_key = f"task_{msg_id}"
    cached = context.user_data.get(cache_key)
    if not cached:
        await query.edit_message_text("⚠ Данные потерялись. Пришли задачу ещё раз.")
        return

    # Fall back to legacy "create in inbox" behavior if whitelist not set
    if not config.allowed_folder_id_list:
        await query.edit_message_text("⏳ Создаю задачу...")
        try:
            task = await clickup.create_task(
                task_data=cached["task_data"],
                file_content=cached["file_content"],
                file_name=cached["file_name"],
            )
            await _send_creation_confirmation(query, cached, task, header="✅ <b>Задача создана.</b>\n")
            context.user_data.pop(cache_key, None)
        except Exception as e:
            logger.error(f"Now (no-whitelist) error: {e}", exc_info=True)
            await query.edit_message_text(f"❌ Не удалось создать: {str(e)}")
        return

    # Whitelist active — show folder picker
    try:
        targets = await _get_targets()
    except Exception as e:
        logger.error(f"Targets fetch error: {e}", exc_info=True)
        await query.edit_message_text(f"❌ Не удалось получить структуру папок: {str(e)}")
        return

    folders = targets.get("folders", [])
    if not folders:
        await query.edit_message_text(
            "⚠ Не удалось получить папки. Проверь CLICKUP_ALLOWED_FOLDER_IDS в Railway."
        )
        return

    task_data = cached["task_data"]
    text = (
        f"📌 <b>{_escape(task_data['name'])}</b>\n\n"
        f"<i>Куда положить?</i>"
    )

    rows = []
    pair = []
    for folder in folders:
        pair.append(InlineKeyboardButton(
            f"📁 {folder['name']}",
            callback_data=f"f:{folder['id']}:{msg_id}",
        ))
        if len(pair) == 2:
            rows.append(pair)
            pair = []
    if pair:
        rows.append(pair)
    rows.append([InlineKeyboardButton("⬅ Отмена", callback_data=f"c:{msg_id}")])

    await query.edit_message_text(
        text,
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(rows),
    )


async def _show_list_picker(query, context, folder_id, msg_id):
    cache_key = f"task_{msg_id}"
    cached = context.user_data.get(cache_key)
    if not cached:
        await query.edit_message_text("⚠ Данные потерялись. Пришли задачу ещё раз.")
        return

    targets = await _get_targets()
    folder = next((f for f in targets.get("folders", []) if f["id"] == folder_id), None)
    if not folder:
        await query.edit_message_text("⚠ Папка не в whitelist. Проверь CLICKUP_ALLOWED_FOLDER_IDS.")
        return

    if not folder["lists"]:
        # Folder is empty — re-show folder picker so user picks again
        await _show_folder_picker(query, context, msg_id)
        return

    task_data = cached["task_data"]
    text = (
        f"📌 <b>{_escape(task_data['name'])}</b>\n\n"
        f"📁 <b>{_escape(folder['name'])}</b> → выбери список:"
    )

    rows = []
    for lst in folder["lists"]:
        count = lst.get("task_count", 0)
        label = lst["name"]
        if count:
            label += f" ({count})"
        rows.append([InlineKeyboardButton(
            label,
            callback_data=f"l:{lst['id']}:{msg_id}",
        )])
    rows.append([
        InlineKeyboardButton("⬅ Назад", callback_data=f"b:{msg_id}"),
        InlineKeyboardButton("⬅ Отмена", callback_data=f"c:{msg_id}"),
    ])

    await query.edit_message_text(
        text,
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(rows),
    )


async def _create_in_target(query, context, list_id, msg_id):
    cache_key = f"task_{msg_id}"
    cached = context.user_data.get(cache_key)
    if not cached:
        await query.edit_message_text("⚠ Данные потерялись. Пришли задачу ещё раз.")
        return

    # HARD GUARD — verify list_id is in whitelist before any API call
    targets = await _get_targets()
    allowed = _allowed_list_ids(targets)
    if list_id not in allowed:
        logger.error(f"BLOCKED: attempt to create task in non-whitelisted list_id={list_id}")
        await query.edit_message_text(
            "❌ Этот список не в whitelist. Задача не создана.\n"
            "Это защита от случайных операций в чужих папках."
        )
        return

    # Find folder/list names for the confirmation message
    target_folder = None
    target_list = None
    for f in targets["folders"]:
        for lst in f["lists"]:
            if lst["id"] == list_id:
                target_folder = f
                target_list = lst
                break
        if target_list:
            break

    await query.edit_message_text("⏳ Создаю задачу...")

    try:
        task = await clickup.create_task(
            task_data=cached["task_data"],
            file_content=cached["file_content"],
            file_name=cached["file_name"],
            list_id=list_id,
        )
        location = (
            f"{target_folder['name']} → {target_list['name']}"
            if target_folder and target_list else
            "указанный список"
        )
        header = f"✅ <b>Задача в {_escape(location)}.</b>\n"
        await _send_creation_confirmation(query, cached, task, header=header)
        context.user_data.pop(cache_key, None)
    except Exception as e:
        logger.error(f"Create-in-target error: {e}", exc_info=True)
        await query.edit_message_text(f"❌ Не удалось создать: {str(e)}")


async def _restore_initial(query, context, msg_id):
    """User cancelled picker — restore original preview with [📥/✅] buttons."""
    cache_key = f"task_{msg_id}"
    cached = context.user_data.get(cache_key)
    if not cached:
        await query.edit_message_text("⚠ Данные потерялись. Пришли задачу ещё раз.")
        return

    lines = _build_preview_lines(
        cached["task_data"],
        file_name=cached.get("file_name"),
        voice_transcript=cached.get("voice_transcript"),
    )
    await query.edit_message_text(
        "\n".join(lines),
        parse_mode="HTML",
        reply_markup=_initial_keyboard(msg_id),
    )


async def _send_creation_confirmation(query, cached, task, header):
    """Format and send the post-creation confirmation message."""
    task_data = cached["task_data"]
    lines = [header]
    lines.append(f"📌 <b>{_escape(task_data['name'])}</b>")
    if task_data.get("due_date_formatted"):
        lines.append(f"📅 {_escape(task_data['due_date_formatted'])}")
    priority = task_data.get("priority", 3)
    lines.append(f"⚡ {PRIORITY_LABELS.get(priority, '🟡 Обычный')}")
    if cached.get("file_name"):
        lines.append(f"📎 <i>{_escape(cached['file_name'])}</i>")
    task_url = task.get("url", "")
    if task_url:
        lines.append(f'\n<a href="{task_url}">🔗 Открыть в ClickUp</a>')
    await query.edit_message_text("\n".join(lines), parse_mode="HTML")


# ─────────────────────────────────────────────────────────────────────────────

def main():
    app = Application.builder().token(config.telegram_bot_token).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", help_command))
    app.add_handler(CommandHandler("inbox", inbox_command))
    app.add_handler(CommandHandler("myid", myid_command))
    app.add_handler(CommandHandler("test_morning", test_morning_command))

    app.add_handler(MessageHandler(filters.VOICE | filters.AUDIO, handle_voice))
    app.add_handler(
        MessageHandler(
            (filters.TEXT | filters.Document.ALL | filters.PHOTO) & ~filters.COMMAND,
            handle_message,
        )
    )
    app.add_handler(CallbackQueryHandler(handle_callback))

    app.job_queue.run_daily(
        morning_inbox_digest,
        time=dt_time(hour=10, minute=0, tzinfo=DUBAI_TZ),
        name="morning_inbox_digest",
    )

    voice_status = "ON" if whisper else "OFF"
    targets_status = f"ON ({len(config.allowed_folder_id_list)} folders)" if config.allowed_folder_id_list else "OFF (fallback to inbox)"
    logger.info(
        f"Bot is running... morning digest at 10:00 Asia/Dubai · "
        f"voice: {voice_status} · target picker: {targets_status}"
    )
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
