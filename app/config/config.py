from pathlib import Path
from typing import List, Optional
from pydantic_settings import BaseSettings
from pydantic import Field


class Settings(BaseSettings):
    """Application settings using Pydantic BaseSettings."""

    # Environment configuration
    env: str = Field(default="dev", description="Environment (dev/prod)")
    debug: bool = Field(default=False, description="Enable verbose request logging")
    commands_config_path: Path = Field(
        default=Path("commands.json"), description="Webhook commands config path"
    )

    # API documentation
    docs_url: Optional[str] = Field(default="/docs", description="Swagger docs URL")
    redoc_url: Optional[str] = Field(default="/redoc", description="ReDoc URL")

    # CORS origins
    origins: List[str] = Field(
        default_factory=lambda: ["*"], description="CORS allowed origins"
    )

    class Config:
        env_file = ".env"
        env_file_encoding = "utf-8"
        case_sensitive = False

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
