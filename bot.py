import os
import logging
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

PRIORITY_LABELS = {
    1: "🔴 Срочный",
    2: "🟠 Высокий",
    3: "🟡 Обычный",
    4: "🔵 Низкий",
}

INBOX_TAG = "inbox-deferred"


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_html(
        "👋 <b>Привет!</b> Я разбираю твои сообщения и помогаю с задачами в ClickUp.\n\n"
        "Просто напиши задачу в свободной форме:\n"
        "• <i>«Просмотреть презентацию до пятницы»</i>\n"
        "• <i>«Позвонить клиенту Иванову до 15 марта»</i>\n"
        "• <i>«Подготовить отчёт срочно — нужно добавить данные за Q1»</i>\n\n"
        "Я разберу её и предложу два варианта:\n"
        "📥 <b>В инбокс</b> — отложить, разберём вместе утром\n"
        "✅ <b>Разобрать</b> — создать задачу прямо сейчас\n\n"
        "Можно прикрепить файл — он добавится к задаче.\n\n"
        "/help — подробная справка"
    )


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_html(
        "<b>Как пользоваться:</b>\n\n"
        "1. Напиши задачу в свободной форме\n"
        "2. Я разберу — покажу превью с дедлайном и приоритетом\n"
        "3. Выбери: 📥 В инбокс (отложить) или ✅ Разобрать (создать сейчас)\n"
        "4. Можно прикрепить файл — попадёт в задачу\n\n"
        "<b>Что делает каждая кнопка:</b>\n"
        "• 📥 <b>В инбокс</b> → задача создаётся с тегом <code>inbox-deferred</code>, "
        "разберём вместе утром\n"
        "• ✅ <b>Разобрать</b> → задача создаётся сразу, без тега\n\n"
        "<b>Примеры:</b>\n"
        "• <code>Подготовить договор с ООО Ромашка, дедлайн 25 марта, приоритет высокий</code>\n"
        "• <code>Срочно позвонить в банк насчёт счёта</code>\n\n"
        "<b>Файлы:</b> любые форматы (PDF, DOCX, PPTX, изображения)"
    )


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
        # Parse task via Claude AI
        task_data = await ai_parser.parse(text)

        # Download file attachment if present
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

        # Stash for the callback handler (in-memory; cleared on bot restart)
        cache_key = f"task_{message.message_id}"
        context.user_data[cache_key] = {
            "task_data": task_data,
            "file_content": file_content,
            "file_name": file_name,
        }

        # Build preview message
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
            tags = [INBOX_TAG]
            header = "📥 <b>Сохранил в инбокс. Разберём утром.</b>\n"
        elif action == "now":
            tags = None
            header = "✅ <b>Задача создана.</b>\n"
        else:
            await query.edit_message_text("⚠ Неизвестное действие.")
            return

        task = await clickup.create_task(
            task_data=task_data,
            file_content=file_content,
            file_name=file_name,
            tags=tags,
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

        # Clean up cache
        context.user_data.pop(cache_key, None)

    except Exception as e:
        logger.error(f"Error creating task: {e}", exc_info=True)
        await query.edit_message_text(
            f"❌ Не удалось создать задачу.\n\n"
            f"Ошибка: {str(e)}\n\n"
            f"Пришли задачу ещё раз."
        )


def _escape(text: str) -> str:
    """Escape HTML special characters."""
    return (
        str(text)
        .replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
    )


def main():
    app = Application.builder().token(config.telegram_bot_token).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", help_command))
    app.add_handler(
        MessageHandler(
            (filters.TEXT | filters.Document.ALL | filters.PHOTO) & ~filters.COMMAND,
            handle_message,
        )
    )
    app.add_handler(CallbackQueryHandler(handle_callback))

    logger.info("Bot is running...")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
