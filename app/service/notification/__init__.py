from typing import Protocol, Sequence

from app.service.notification.telegram.api import TelegramNotificationService
from app.service.notification.telegram.config import TelegramConfig


class TextNotificationChannel(Protocol):
    def send_text(self, text: str) -> bool: ...


class NotificationService:
    def __init__(self, channels: Sequence[TextNotificationChannel]) -> None:
        self._channels = list(channels)

    @classmethod
    def from_telegram(
        cls, bot_token: str | None, chat_id: str | None
    ) -> "NotificationService":
        telegram = TelegramNotificationService(
            TelegramConfig(bot_token=bot_token, chat_id=chat_id)
        )
        return cls([telegram])

    def send_text(self, text: str) -> bool:
        sent = False
        for channel in self._channels:
            sent = channel.send_text(text) or sent
        return sent


__all__ = ["NotificationService"]
