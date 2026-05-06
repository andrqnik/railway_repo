import logging
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import aiohttp

logger = logging.getLogger(__name__)

DUBAI_TZ = ZoneInfo("Asia/Dubai")


def _today_dubai_range_ms() -> tuple:
    """Returns (start_ms, end_ms) for today in Dubai TZ as inclusive UTC ms timestamps."""
    now = datetime.now(DUBAI_TZ)
    start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    end = start + timedelta(days=1) - timedelta(milliseconds=1)
    return int(start.timestamp() * 1000), int(end.timestamp() * 1000)


class ClickUpClient:
    """Async client for the ClickUp API v2."""

    BASE_URL = "https://api.clickup.com/api/v2"

    def __init__(self, api_key: str, list_id: str):
        self.api_key = api_key
        self.list_id = list_id

    @property
    def _json_headers(self) -> dict:
        return {
            "Authorization": self.api_key,
            "Content-Type": "application/json",
        }

    async def create_task(
        self,
        task_data: dict,
        file_content: bytes = None,
        file_name: str = None,
        tags: list = None,
        list_id: str = None,
    ) -> dict:
        """Create a task. If list_id is given, creates there; else uses default (inbox)."""
        target_list = list_id or self.list_id
        payload = {
            "name": task_data["name"],
            "description": task_data.get("description", ""),
            "priority": task_data.get("priority", 3),
            "notify_all": False,
        }

        if task_data.get("due_date"):
            payload["due_date"] = int(task_data["due_date"])
            payload["due_date_time"] = True

        if tags:
            payload["tags"] = tags

        async with aiohttp.ClientSession() as session:
            task = await self._create_task_request(session, target_list, payload)

            if file_content and file_name and task.get("id"):
                try:
                    await self._upload_attachment(session, task["id"], file_content, file_name)
                except Exception as e:
                    logger.warning(f"File attachment failed (task was still created): {e}")

        return task

    async def get_inbox_tasks(self, include_closed: bool = False) -> list:
        """Fetch open tasks from the inbox list."""
        url = f"{self.BASE_URL}/list/{self.list_id}/task"
        params = {"archived": "false"}
        if include_closed:
            params["include_closed"] = "true"

        async with aiohttp.ClientSession() as session:
            async with session.get(url, headers=self._json_headers, params=params) as resp:
                response_text = await resp.text()
                if resp.status != 200:
                    logger.error(f"ClickUp get tasks error {resp.status}: {response_text}")
                    if resp.status == 401:
                        raise Exception("Неверный ClickUp API ключ (401).")
                    elif resp.status == 404:
                        raise Exception("Inbox-список не найден (404). Проверь CLICKUP_LIST_ID.")
                    else:
                        raise Exception(f"Ошибка ClickUp API ({resp.status}): {response_text[:200]}")
                data = await resp.json()
                return data.get("tasks", [])

    async def get_allowed_targets(
        self,
        allowed_folder_ids: list,
        exclude_list_ids: list = None,
    ) -> dict:
        """Fetch ONLY whitelisted folders + their non-archived lists."""
        exclude = set(exclude_list_ids or [])
        folders = []

        async with aiohttp.ClientSession() as session:
            for folder_id in allowed_folder_ids:
                url = f"{self.BASE_URL}/folder/{folder_id}"
                try:
                    async with session.get(url, headers=self._json_headers) as resp:
                        if resp.status != 200:
                            response_text = await resp.text()
                            logger.warning(
                                f"Folder {folder_id} fetch failed ({resp.status}): "
                                f"{response_text[:120]}; skipping"
                            )
                            continue
                        data = await resp.json()
                except Exception as e:
                    logger.warning(f"Folder {folder_id} fetch exception: {e}; skipping")
                    continue

                folder_name = data.get("name", "?")
                lists = []
                for lst in data.get("lists", []):
                    if lst.get("archived"):
                        continue
                    list_id = lst.get("id")
                    if not list_id or list_id in exclude:
                        continue
                    lists.append({
                        "id": list_id,
                        "name": lst.get("name", "?"),
                        "task_count": lst.get("task_count", 0),
                    })

                folders.append({
                    "id": folder_id,
                    "name": folder_name,
                    "lists": lists,
                })

        return {"folders": folders}

    async def get_tasks_due_today(
        self,
        allowed_folder_ids: list,
        exclude_list_ids: list = None,
    ) -> list:
        """Fetch open tasks due today across all whitelisted lists.

        Per-list iteration — each call is hard-scoped to a single list_id from
        the whitelist. Cannot leak into other teams' folders by construction.

        Returns flat list of tasks with extra '_folder_name' and '_list_name'
        keys for display purposes.
        """
        targets = await self.get_allowed_targets(allowed_folder_ids, exclude_list_ids)

        start_ms, end_ms = _today_dubai_range_ms()
        # Use exclusive bounds with -1/+1 trick so midnight and 23:59:59.999 are inclusive
        gt = str(start_ms - 1)
        lt = str(end_ms + 1)

        all_tasks = []

        async with aiohttp.ClientSession() as session:
            for folder in targets["folders"]:
                for lst in folder["lists"]:
                    url = f"{self.BASE_URL}/list/{lst['id']}/task"
                    params = {
                        "due_date_gt": gt,
                        "due_date_lt": lt,
                        "include_closed": "false",
                        "archived": "false",
                    }
                    try:
                        async with session.get(url, headers=self._json_headers, params=params) as resp:
                            if resp.status != 200:
                                response_text = await resp.text()
                                logger.warning(
                                    f"List {lst['id']} ({folder['name']}/{lst['name']}) "
                                    f"due-today fetch failed ({resp.status}): {response_text[:120]}"
                                )
                                continue
                            data = await resp.json()
                    except Exception as e:
                        logger.warning(f"List {lst['id']} due-today fetch exception: {e}")
                        continue

                    for task in data.get("tasks", []):
                        task["_folder_name"] = folder["name"]
                        task["_list_name"] = lst["name"]
                        all_tasks.append(task)

        return all_tasks

    async def get_task(self, task_id: str) -> dict:
        """Fetch full task data by id."""
        url = f"{self.BASE_URL}/task/{task_id}"
        async with aiohttp.ClientSession() as session:
            async with session.get(url, headers=self._json_headers) as resp:
                response_text = await resp.text()
                if resp.status != 200:
                    logger.error(f"ClickUp get_task error {resp.status}: {response_text}")
                    raise Exception(f"Не удалось получить задачу ({resp.status}): {response_text[:200]}")
                return await resp.json()

    async def delete_task(self, task_id: str) -> None:
        """Delete a task by id."""
        url = f"{self.BASE_URL}/task/{task_id}"
        async with aiohttp.ClientSession() as session:
            async with session.delete(url, headers=self._json_headers) as resp:
                if resp.status not in (200, 204):
                    response_text = await resp.text()
                    logger.error(f"ClickUp delete_task error {resp.status}: {response_text}")
                    raise Exception(f"Не удалось удалить задачу ({resp.status}): {response_text[:200]}")

    async def _create_task_request(
        self,
        session: aiohttp.ClientSession,
        list_id: str,
        payload: dict,
    ) -> dict:
        url = f"{self.BASE_URL}/list/{list_id}/task"

        async with session.post(url, headers=self._json_headers, json=payload) as resp:
            response_text = await resp.text()

            if resp.status not in (200, 201):
                logger.error(f"ClickUp create task error {resp.status} (list {list_id}): {response_text}")
                if resp.status == 401:
                    raise Exception("Неверный ClickUp API ключ (401 Unauthorized).")
                elif resp.status == 404:
                    raise Exception(f"Список ClickUp не найден (404, list_id={list_id}).")
                else:
                    raise Exception(f"Ошибка ClickUp API ({resp.status}): {response_text[:200]}")

            return await resp.json()

    async def _upload_attachment(
        self,
        session: aiohttp.ClientSession,
        task_id: str,
        file_content: bytes,
        file_name: str,
    ):
        url = f"{self.BASE_URL}/task/{task_id}/attachment"
        headers = {"Authorization": self.api_key}

        form = aiohttp.FormData()
        form.add_field(
            "attachment",
            file_content,
            filename=file_name,
            content_type="application/octet-stream",
        )

        async with session.post(url, headers=headers, data=form) as resp:
            if resp.status not in (200, 201):
                response_text = await resp.text()
                raise Exception(f"Attachment upload failed ({resp.status}): {response_text[:200]}")

        logger.info(f"File '{file_name}' successfully attached to task {task_id}")
