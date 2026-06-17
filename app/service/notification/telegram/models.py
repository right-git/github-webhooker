from dataclasses import dataclass


@dataclass(frozen=True)
class TelegramMessage:
    text: str
