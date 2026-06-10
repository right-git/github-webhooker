from pathlib import Path
from typing import List, Optional
from pydantic_settings import BaseSettings, SettingsConfigDict
from pydantic import Field


class Settings(BaseSettings):
    """Application settings using Pydantic BaseSettings."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    # Environment configuration
    env: str = Field(default="dev", description="Environment (dev/prod)")
    port: int = Field(default=8081, description="Application port")
    debug: bool = Field(
        default=False,
        validation_alias="APP_DEBUG",
        description="Enable verbose request logging",
    )
    commands_config_path: Path = Field(
        default=Path("commands.json"), description="Webhook commands config path"
    )
    bot_token: Optional[str] = Field(default=None, description="Telegram bot token")
    chat_id: Optional[str] = Field(default=None, description="Telegram chat id")
    command_timeout_seconds: int = Field(
        default=600, description="Command execution timeout in seconds"
    )

    # API documentation
    docs_url: Optional[str] = Field(default="/docs", description="Swagger docs URL")
    redoc_url: Optional[str] = Field(default="/redoc", description="ReDoc URL")

    # CORS origins
    origins: List[str] = Field(
        default_factory=lambda: ["*"], description="CORS allowed origins"
    )

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self._configure_environment_specific_settings()

    def _configure_environment_specific_settings(self):
        """Configure settings based on environment."""
        if self.env == "prod":
            # Production settings
            self.docs_url = None
            self.redoc_url = None


# Create settings instance
settings = Settings()
