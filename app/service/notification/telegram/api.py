import html
import json
import random
import time
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

        draft_id = random.randint(1, 2**63 - 1)
        current_text = ""
        for chunk in message.text.split():
            current_text = f"{current_text}{chunk} "
            if not self._telegram(
                "sendMessageDraft",
                {
                    "chat_id": self._config.chat_id,
                    "draft_id": draft_id,
                    "text": html.escape(current_text.strip(), quote=False),
                    "parse_mode": "HTML",
                },
            ):
                return False
            time.sleep(0.2)

        return self._telegram(
            "sendMessage",
            {
                "chat_id": self._config.chat_id,
                "text": html.escape(message.text, quote=False),
                "parse_mode": "HTML",
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
