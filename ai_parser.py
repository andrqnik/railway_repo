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
    lines = []
    for i in range(14):
        day = today + timedelta(days=i)
        label = "сегодня" if i == 0 else ("завтра" if i == 1 else WEEKDAY_RU[day.weekday()])
        lines.append(f"  {label} = {day.strftime('%Y-%m-%d')} ({_format_date_ru(day)})")
    return "\n".join(lines)


def _strip_json_fence(text: str) -> str:
    """Strip ``` markdown fences if Claude wraps JSON in them."""
    if "```" in text:
        match = re.search(r"```(?:json)?\s*(.*?)```", text, re.DOTALL)
        if match:
            return match.group(1).strip()
    return text


class AIParser:
    """Parses free-form task descriptions and classifies them via Claude AI."""

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
            model="claude-sonnet-4-6",
            max_tokens=512,
            messages=[{"role": "user", "content": prompt}],
        )

        response_text = _strip_json_fence(message.content[0].text.strip())
        logger.debug(f"AI parse response: {response_text}")

        task_data = json.loads(response_text)

        task_data.setdefault("name", text[:100])
        task_data.setdefault("description", "")
        task_data.setdefault("due_date_str", None)
        task_data.setdefault("priority", 3)

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

        task_data["priority"] = max(1, min(4, int(task_data["priority"])))

        return task_data

    async def classify_target(self, task_data: dict, targets: dict) -> dict:
        """Suggest the best target list for an already-parsed task.

        Args:
            task_data: dict with at least 'name' and 'description'
            targets: {"folders": [{"id", "name", "lists": [{"id", "name", ...}]}]}

        Returns:
            {
              "list_id": str|None,
              "folder_name": str,
              "list_name": str,
              "confidence": "high"|"medium"|"low"|"none",
              "reasoning": str,
            }
            list_id=None means no clear match.
        """
        options = []
        for folder in targets.get("folders", []):
            for lst in folder.get("lists", []):
                options.append({
                    "id": lst["id"],
                    "folder": folder["name"],
                    "list": lst["name"],
                })

        if not options:
            return {"list_id": None, "confidence": "none", "reasoning": "Нет доступных списков."}

        options_str = "\n".join([
            f'  - id={o["id"]} | {o["folder"]} → {o["list"]}'
            for o in options
        ])

        name = task_data.get("name", "")
        description = task_data.get("description", "") or "(нет дополнительного контекста)"

        prompt = f"""Андрей запускает пиццерии по франшизе в нескольких странах. У него ClickUp с папками по странам и подсписками внутри.

Задача: {name}
Контекст: {description}

Доступные списки:
{options_str}

Подсказка про названия (могут встречаться):
- "запуск" — задачи pre-opening (стройка, лицензии, оборудование, найм)
- "general" — общие операционные вопросы по стране
- "проектировка" / "проектирование" — дизайн помещения, чертежи
- "Dubai", "JLT launch", "List", "Business Trip" — конкретные точки/проекты
- "New country launch" — задачи по странам, где ещё нет своей папки

Верни JSON одной строкой, без markdown:
{{"list_id": "ID_из_списка_выше_или_null", "confidence": "high|medium|low", "reasoning": "одно короткое предложение по-русски почему этот список"}}

- list_id должен быть ровно из списка выше (ID, не имя). null если задача не очевидно подходит ни под один.
- confidence: high если уверен (>=90%), medium (50-90%), low (<50%).
- reasoning: 5-15 слов на русском, объясняющие выбор."""

        try:
            message = await self.client.messages.create(
                model="claude-sonnet-4-6",
                max_tokens=300,
                messages=[{"role": "user", "content": prompt}],
            )
            response_text = _strip_json_fence(message.content[0].text.strip())
            logger.debug(f"AI classify response: {response_text}")
            result = json.loads(response_text)
        except Exception as e:
            logger.warning(f"AI classify failed: {e}")
            return {"list_id": None, "confidence": "none", "reasoning": ""}

        list_id = result.get("list_id")
        result.setdefault("confidence", "low")
        result.setdefault("reasoning", "")
        result["list_id"] = list_id

        # Resolve folder/list names from the matched id
        if list_id:
            for o in options:
                if o["id"] == list_id:
                    result["folder_name"] = o["folder"]
                    result["list_name"] = o["list"]
                    break
            else:
                # Claude returned an id not in the options — invalidate
                logger.warning(f"AI returned non-existent list_id={list_id}; ignoring")
                result["list_id"] = None
                result["confidence"] = "none"

        return result
