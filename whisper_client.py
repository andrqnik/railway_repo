import logging
import aiohttp

logger = logging.getLogger(__name__)


class WhisperClient:
    """Async client for OpenAI Whisper transcription API."""

    URL = "https://api.openai.com/v1/audio/transcriptions"

    def __init__(self, api_key: str):
        self.api_key = api_key

    async def transcribe(
        self,
        audio_bytes: bytes,
        file_name: str = "voice.ogg",
        language: str = "ru",
    ) -> str:
        """Transcribe audio. Returns the transcribed text (stripped)."""
        headers = {"Authorization": f"Bearer {self.api_key}"}

        form = aiohttp.FormData()
        form.add_field("file", audio_bytes, filename=file_name, content_type="audio/ogg")
        form.add_field("model", "whisper-1")
        form.add_field("language", language)
        form.add_field("response_format", "json")

        async with aiohttp.ClientSession() as session:
            async with session.post(self.URL, headers=headers, data=form) as resp:
                response_text = await resp.text()
                if resp.status != 200:
                    logger.error(f"Whisper error {resp.status}: {response_text}")
                    if resp.status == 401:
                        raise Exception("Неверный OPENAI_API_KEY (401).")
                    elif resp.status == 413:
                        raise Exception("Голосовое слишком большое для Whisper (>25 MB).")
                    else:
                        raise Exception(f"Whisper API error ({resp.status}): {response_text[:200]}")
                data = await resp.json()
                return data.get("text", "").strip()
