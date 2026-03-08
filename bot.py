import os
import logging
from dotenv import load_dotenv
from telegram import Update
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
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


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_html(
        "👋 <b>Привет!</b> Я создаю задачи в ClickUp по твоим сообщениям.\n\n"
        "Просто напиши задачу в свободной форме:\n"
        "• <i>«Просмотреть презентацию до пятницы»</i>\n"
        "• <i>«Позвонить клиенту Иванову до 15 марта»</i>\n"
        "• <i>«Подготовить отчёт срочно — нужно добавить данные за Q1»</i>\n\n"
        "Можно прикрепить файл — он автоматически добавится к задаче.\n\n"
        "/help — подробная справка"
    )


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_html(
        "<b>Как пользоваться ботом:</b>\n\n"
        "1. Напишите задачу в свободной форме\n"
        "2. Укажите дедлайн если нужен (<i>«до 20 марта»</i>, <i>«к пятнице»</i>)\n"
        "3. При желании укажите приоритет (<i>«срочно»</i>, <i>«высокий приоритет»</i>)\n"
        "4. Можно прикрепить файл — он добавится к задаче в ClickUp\n\n"
        "<b>Примеры:</b>\n"
        "• <code>Посмотреть презентацию Маркетинг Q1 до конца недели</code>\n"
        "• <code>Подготовить договор с ООО Ромашка, дедлайн 25 марта, приоритет высокий</code>\n"
        "• <code>Срочно позвонить в банк насчёт счёта</code>\n\n"
        "<b>Поддерживаемые форматы файлов:</b> любые (PDF, DOCX, PPTX, изображения и т.д.)"
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
            # Take the highest quality photo variant
            photo = message.photo[-1]
            file = await photo.get_file()
            file_content = bytes(await file.download_as_bytearray())
            file_name = f"photo_{photo.file_id}.jpg"

        # Create task in ClickUp
        task = await clickup.create_task(
            task_data=task_data,
            file_content=file_content,
            file_name=file_name,
        )

        # Build confirmation message
        lines = ["✅ <b>Задача создана в ClickUp!</b>\n"]
        lines.append(f"📌 <b>{_escape(task_data['name'])}</b>")

        if task_data.get("due_date_formatted"):
            lines.append(f"📅 Дедлайн: {_escape(task_data['due_date_formatted'])}")

        priority = task_data.get("priority", 3)
        lines.append(f"⚡ Приоритет: {PRIORITY_LABELS.get(priority, '🟡 Обычный')}")

        if task_data.get("description"):
            desc = task_data["description"]
            if len(desc) > 120:
                desc = desc[:120] + "..."
            lines.append(f"📝 {_escape(desc)}")

        if file_name:
            lines.append(f"📎 Файл: <i>{_escape(file_name)}</i>")

        task_url = task.get("url", "")
        if task_url:
            lines.append(f'\n<a href="{task_url}">🔗 Открыть задачу в ClickUp</a>')

        await status_msg.edit_text("\n".join(lines), parse_mode="HTML")

    except Exception as e:
        logger.error(f"Error creating task: {e}", exc_info=True)
        await status_msg.edit_text(
            f"❌ Не удалось создать задачу.\n\n"
            f"Ошибка: {str(e)}\n\n"
            f"Проверьте настройки бота (/start) или попробуйте снова."
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

    logger.info("Bot is running...")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
