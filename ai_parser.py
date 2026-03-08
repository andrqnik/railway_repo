import json
import re
import logging
from datetime import datetime, timedelta
import anthropic

logger = logging.getLogger(__name__)

WEEKDAY_RU = ["понедельник", "вторник", "среда", "четверг", "пятница", "суббота", "воскресенье"]
MONTHS_RU = ["января", "февраля", "марта", "апреля", "мая", "июня",
             "июля", "августа", "сентября", "октября", "ноября", "декабря"]


def _format_date_ru(dt: datetime) -> str:
    return f"{dt.day} {MONTHS_RU[dt.month - 1]} {dt.year}"


def _build_week_calendar(today: datetime) -> str:
    """Build an explicit calendar of the next 14 days so Claude never guesses."""
    lines = []
    for i in range(14):
        day = today + timedelta(days=i)
        label = "сегодня" if i == 0 else ("завтра" if i == 1 else WEEKDAY_RU[day.weekday()])
        lines.append(f"  {label} = {day.strftime('%Y-%m-%d')} ({_format_date_ru(day)})")
    return "\n".join(lines)


class AIParser:
    """Parses free-form task descriptions using Claude AI."""

    def __init__(self, api_key: str):
        self.client = anthropic.AsyncAnthropic(api_key=api_key)

    async def parse(self, text: str) -> dict:
        today = datetime.now()
        today_str = today.strftime("%Y-%m-%d")
        calendar = _build_week_calendar(today)

        prompt = f"""You are a task parser for a Russian-speaking user. Extract task information from the text below.

Today is {today_str}. Use this exact calendar for ALL date calculations — never guess:
{calendar}

Task text: "{text}"

Return ONLY a valid JSON object with these fields:
- "name": concise task title in Russian (string, max 100 chars, required)
- "description": additional context or details (string, empty string if none)
- "due_date_str": deadline as a date string in YYYY-MM-DD format taken directly from the calendar above (string or null if no deadline mentioned)
- "priority": 1=срочно/urgent, 2=высокий/high, 3=обычный/normal, 4=низкий/low (integer, default 3)

Rules:
- For due_date_str copy the YYYY-MM-DD value EXACTLY from the calendar above — do not calculate or invent dates
- "до конца недели" / "к концу недели" = ближайшее воскресенье из календаря
- "на следующей неделе" = следующий понедельник из календаря
- If no deadline is mentioned, set due_date_str to null
- Keep the name short and clear — it's the task title
- Return ONLY the JSON object — no markdown fences, no explanation"""

        message = await self.client.messages.create(
            model="claude-opus-4-6",
            max_tokens=512,
            messages=[{"role": "user", "content": prompt}],
        )

        response_text = message.content[0].text.strip()
        logger.debug(f"AI response: {response_text}")

        # Strip markdown code blocks if present
        if "```" in response_text:
            match = re.search(r"```(?:json)?\s*(.*?)```", response_text, re.DOTALL)
            if match:
                response_text = match.group(1).strip()

        task_data = json.loads(response_text)

        # Ensure required fields have safe defaults
        task_data.setdefault("name", text[:100])
        task_data.setdefault("description", "")
        task_data.setdefault("due_date_str", None)
        task_data.setdefault("priority", 3)

        # Convert YYYY-MM-DD string → Unix timestamp in ms (Python does this, not Claude)
        due_date_str = task_data.pop("due_date_str", None)
        if due_date_str:
            try:
                dt = datetime.strptime(due_date_str, "%Y-%m-%d")
                task_data["due_date"] = int(dt.timestamp() * 1000)
                task_data["due_date_formatted"] = _format_date_ru(dt)
            except ValueError:
                logger.warning(f"Could not parse date string: {due_date_str}")
                task_data["due_date"] = None
                task_data["due_date_formatted"] = None
        else:
            task_data["due_date"] = None
            task_data["due_date_formatted"] = None

        # Clamp priority to valid range
        task_data["priority"] = max(1, min(4, int(task_data["priority"])))

        return task_data
