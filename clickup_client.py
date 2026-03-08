import logging
import aiohttp

logger = logging.getLogger(__name__)


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
    ) -> dict:
        """Create a task in ClickUp and optionally attach a file."""
        payload = {
            "name": task_data["name"],
            "description": task_data.get("description", ""),
            "priority": task_data.get("priority", 3),
            "notify_all": False,
        }

        if task_data.get("due_date"):
            payload["due_date"] = int(task_data["due_date"])
            payload["due_date_time"] = True

        async with aiohttp.ClientSession() as session:
            task = await self._create_task_request(session, payload)

            # Upload file attachment if provided
            if file_content and file_name and task.get("id"):
                try:
                    await self._upload_attachment(session, task["id"], file_content, file_name)
                except Exception as e:
                    # Don't fail the whole operation if attachment upload fails
                    logger.warning(f"File attachment failed (task was still created): {e}")

        return task

    async def _create_task_request(self, session: aiohttp.ClientSession, payload: dict) -> dict:
        url = f"{self.BASE_URL}/list/{self.list_id}/task"

        async with session.post(url, headers=self._json_headers, json=payload) as resp:
            response_text = await resp.text()

            if resp.status not in (200, 201):
                logger.error(f"ClickUp create task error {resp.status}: {response_text}")

                # Give user a human-readable error
                if resp.status == 401:
                    raise Exception("Неверный ClickUp API ключ (401 Unauthorized). Проверьте CLICKUP_API_KEY.")
                elif resp.status == 404:
                    raise Exception("Список ClickUp не найден (404). Проверьте CLICKUP_LIST_ID.")
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
        headers = {"Authorization": self.api_key}  # No Content-Type — multipart

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
