from dataclasses import dataclass


@dataclass(frozen=True)
class TelegramConfig:
    bot_token: str | None = None
    chat_id: str | None = None

    @property
    def is_configured(self) -> bool:
        return bool(self.bot_token and self.chat_id)
