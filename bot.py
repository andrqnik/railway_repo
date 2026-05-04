import os
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

load_dotenv()

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

config = Config()
ai_parser = AIParser(config.anthropic_api_key)
clickup = ClickUpClient(config.clickup_api_key, config.clickup_list_id)

DUBAI_TZ = ZoneInfo("Asia/Dubai")

PRIORITY_LABELS = {
    1: "🔴 Срочный",
    2: "🟠 Высокий",
    3: "🟡 Обычный",
    4: "🔵 Низкий",
}

MONTHS_RU = ["января", "февраля", "марта", "апреля", "мая", "июня",
             "июля", "августа", "сентября", "октября", "ноября", "декабря"]


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
    """Escape HTML special characters."""
    return (
        str(text)
        .replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
    )


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


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_html(
        "👋 <b>Привет!</b> Я разбираю твои сообщения и помогаю с задачами в ClickUp.\n\n"
        "Просто напиши задачу в свободной форме — я разберу и предложу:\n"
        "📥 <b>В инбокс</b> — отложить, разберём вместе утром\n"
        "✅ <b>Разобрать</b> — создать задачу прямо сейчас\n\n"
        "Каждое утро в 8:15 (Дубай) пришлю список того что в инбоксе.\n\n"
        "<b>Команды:</b>\n"
        "/inbox — показать инбокс прямо сейчас\n"
        "/help — подробная справка"
    )


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_html(
        "<b>Как пользоваться:</b>\n\n"
        "1. Напиши задачу в свободной форме\n"
        "2. Я разберу — покажу превью с дедлайном и приоритетом\n"
        "3. Выбери: 📥 В инбокс (отложить) или ✅ Разобрать (создать сейчас)\n"
        "4. Можно прикрепить файл — попадёт в задачу\n\n"
        "<b>Команды:</b>\n"
        "/inbox — показать что не разобрано\n"
        "/myid — узнать свой chat_id (для настройки утренних дайджестов)\n"
        "/test_morning — проверить утренний дайджест прямо сейчас\n\n"
        "<b>Утренний дайджест:</b>\n"
        "Каждый день в 8:15 по Дубаю присылаю список того что в инбоксе. "
        "Если инбокс пустой — молчу.\n\n"
        "<b>Примеры задач:</b>\n"
        "• <code>Подготовить договор с ООО Ромашка, дедлайн 25 марта, приоритет высокий</code>\n"
        "• <code>Срочно позвонить в банк насчёт счёта</code>"
    )


async def inbox_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Show current inbox tasks on demand."""
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
    """Returns the user's chat_id — needed once to enable proactive morning digests."""
    chat_id = update.effective_chat.id
    await update.message.reply_html(
        f"Твой <b>chat_id</b>: <code>{chat_id}</code>\n\n"
        f"Чтобы включить утренние дайджесты:\n"
        f"1. Зайди в Railway → Variables\n"
        f"2. Добавь переменную <code>TELEGRAM_USER_CHAT_ID</code> со значением <code>{chat_id}</code>\n"
        f"3. Railway автоматически перезапустит бота\n"
        f"4. Проверь через /test_morning что работает"
    )


async def morning_inbox_digest(context: ContextTypes.DEFAULT_TYPE):
    """Daily 8:15 Asia/Dubai — push inbox if non-empty. Silent on empty days."""
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
    """Run the morning digest right now (for testing without waiting until 8:15)."""
    if not config.telegram_user_chat_id:
        await update.message.reply_html(
            "⚠ Сначала добавь <code>TELEGRAM_USER_CHAT_ID</code> в Railway Variables.\n"
            "Узнать свой chat_id: /myid"
        )
        return
    await update.message.reply_text("🧪 Запускаю утренний дайджест прямо сейчас...")
    await morning_inbox_digest(context)


async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    message = update.message
    text = message.text or message.caption or ""

    if not text.strip():
        await message.reply_text(
            "📝 Пожалуйста, напиши описание задачи.\n"
            "Можно отправить текст вместе с файлом или просто текст."
        )
        return

    status_msg = await message.reply_text("⏳ Разбираю задачу...")

    try:
        task_data = await ai_parser.parse(text)

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

        cache_key = f"task_{message.message_id}"
        context.user_data[cache_key] = {
            "task_data": task_data,
            "file_content": file_content,
            "file_name": file_name,
        }

        lines = ["📝 <b>Распознал:</b>\n"]
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

        keyboard = InlineKeyboardMarkup([
            [
                InlineKeyboardButton("📥 В инбокс", callback_data=f"defer:{message.message_id}"),
                InlineKeyboardButton("✅ Разобрать", callback_data=f"now:{message.message_id}"),
            ]
        ])

        await status_msg.edit_text(
            "\n".join(lines),
            parse_mode="HTML",
            reply_markup=keyboard,
        )

    except Exception as e:
        logger.error(f"Error parsing task: {e}", exc_info=True)
        await status_msg.edit_text(
            f"❌ Не удалось разобрать задачу.\n\n"
            f"Ошибка: {str(e)}\n\n"
            f"Попробуй сформулировать иначе."
        )


async def handle_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    try:
        action, msg_id = query.data.split(":", 1)
    except ValueError:
        await query.edit_message_text("⚠ Неверный callback.")
        return

    cache_key = f"task_{msg_id}"
    cached = context.user_data.get(cache_key)

    if not cached:
        await query.edit_message_text(
            "⚠ Данные о задаче потерялись (бот мог перезапуститься).\n"
            "Пришли задачу ещё раз — я её разберу."
        )
        return

    task_data = cached["task_data"]
    file_content = cached["file_content"]
    file_name = cached["file_name"]

    await query.edit_message_text("⏳ Создаю задачу...")

    try:
        if action == "defer":
            header = "📥 <b>Сохранил в инбокс. Разберём утром.</b>\n"
        elif action == "now":
            header = "✅ <b>Задача создана.</b>\n"
        else:
            await query.edit_message_text("⚠ Неизвестное действие.")
            return

        task = await clickup.create_task(
            task_data=task_data,
            file_content=file_content,
            file_name=file_name,
        )

        lines = [header]
        lines.append(f"📌 <b>{_escape(task_data['name'])}</b>")

        if task_data.get("due_date_formatted"):
            lines.append(f"📅 {_escape(task_data['due_date_formatted'])}")

        priority = task_data.get("priority", 3)
        lines.append(f"⚡ {PRIORITY_LABELS.get(priority, '🟡 Обычный')}")

        if file_name:
            lines.append(f"📎 <i>{_escape(file_name)}</i>")

        task_url = task.get("url", "")
        if task_url:
            lines.append(f'\n<a href="{task_url}">🔗 Открыть в ClickUp</a>')

        await query.edit_message_text(
            "\n".join(lines),
            parse_mode="HTML",
        )

        context.user_data.pop(cache_key, None)

    except Exception as e:
        logger.error(f"Error creating task: {e}", exc_info=True)
        await query.edit_message_text(
            f"❌ Не удалось создать задачу.\n\n"
            f"Ошибка: {str(e)}\n\n"
            f"Пришли задачу ещё раз."
        )


def main():
    app = Application.builder().token(config.telegram_bot_token).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", help_command))
    app.add_handler(CommandHandler("inbox", inbox_command))
    app.add_handler(CommandHandler("myid", myid_command))
    app.add_handler(CommandHandler("test_morning", test_morning_command))
    app.add_handler(
        MessageHandler(
            (filters.TEXT | filters.Document.ALL | filters.PHOTO) & ~filters.COMMAND,
            handle_message,
        )
    )
    app.add_handler(CallbackQueryHandler(handle_callback))

    # Schedule morning digest at 8:15 Asia/Dubai
    app.job_queue.run_daily(
        morning_inbox_digest,
        time=dt_time(hour=8, minute=15, tzinfo=DUBAI_TZ),
        name="morning_inbox_digest",
    )

    logger.info("Bot is running... morning digest scheduled at 8:15 Asia/Dubai")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
