import json
from urllib.request import Request, urlopen

from loguru import logger

from app.service.notification.telegram.config import TelegramConfig
from app.service.notification.telegram.models import TelegramMessage


class TelegramNotificationService:
    def __init__(self, config: TelegramConfig) -> None:
        self._config = config

    def send_text(self, text: str) -> bool:
        return self.send(TelegramMessage(text=text))

    def send(self, message: TelegramMessage) -> bool:
        if not self._config.is_configured:
            return False

        return self._telegram(
            "sendMessage",
            {
                "chat_id": self._config.chat_id,
                "text": message.text,
                "parse_mode": "HTML",
                "disable_web_page_preview": True,
            },
        )

    def _telegram(self, method: str, payload: dict) -> bool:
        data = json.dumps(payload).encode("utf-8")
        request = Request(
            f"https://api.telegram.org/bot{self._config.bot_token}/{method}",
            data=data,
            headers={"Content-Type": "application/json"},
            method="POST",
        )

        try:
            with urlopen(request, timeout=10):
                return True
        except OSError as exc:
            logger.warning("Telegram notification failed: {}", exc)
            return False
