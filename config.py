import os


class Config:
    """Loads and validates environment variables."""

    def __init__(self):
        # Required
        self.telegram_bot_token = os.environ.get("TELEGRAM_BOT_TOKEN", "")
        self.anthropic_api_key = os.environ.get("ANTHROPIC_API_KEY", "")
        self.clickup_api_key = os.environ.get("CLICKUP_API_KEY", "")
        self.clickup_list_id = os.environ.get("CLICKUP_LIST_ID", "")

        # Optional — proactive morning digest
        self.telegram_user_chat_id = os.environ.get("TELEGRAM_USER_CHAT_ID", "")

        # Optional — voice transcription via Whisper
        self.openai_api_key = os.environ.get("OPENAI_API_KEY", "")

        missing = [
            name
            for name, value in [
                ("TELEGRAM_BOT_TOKEN", self.telegram_bot_token),
                ("ANTHROPIC_API_KEY", self.anthropic_api_key),
                ("CLICKUP_API_KEY", self.clickup_api_key),
                ("CLICKUP_LIST_ID", self.clickup_list_id),
            ]
            if not value
        ]

        if missing:
            raise ValueError(
                f"Missing required environment variables: {', '.join(missing)}\n"
                f"Please set them in your .env file or Railway Variables."
            )
