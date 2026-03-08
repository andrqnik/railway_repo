import os


class Config:
    """Loads and validates required environment variables."""

    def __init__(self):
        self.telegram_bot_token = os.environ.get("TELEGRAM_BOT_TOKEN", "")
        self.anthropic_api_key = os.environ.get("ANTHROPIC_API_KEY", "")
        self.clickup_api_key = os.environ.get("CLICKUP_API_KEY", "")
        self.clickup_list_id = os.environ.get("CLICKUP_LIST_ID", "")

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
