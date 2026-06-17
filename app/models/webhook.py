from typing import List, Optional

from pydantic import BaseModel, Field, field_validator


class WebhookCommandBase(BaseModel):
    name: str = Field(min_length=1)
    route: str = Field(min_length=1)
    commands: List[str] = Field(min_length=1)

    @field_validator("route")
    @classmethod
    def route_must_be_absolute(cls, value: str) -> str:
        if not value.startswith("/"):
            raise ValueError("route must start with '/'")
        return value

    @field_validator("commands")
    @classmethod
    def commands_must_not_be_blank(cls, value: List[str]) -> List[str]:
        for command in value:
            if not command.strip():
                raise ValueError("commands must not contain blank values")
        return value


class RateLimitConfig(BaseModel):
    requests: int = Field(default=5, ge=1)
    seconds: int = Field(default=60, ge=1)


class GitHubWebhookCommand(WebhookCommandBase):
    secret: str = Field(min_length=1)
    push_branches: Optional[List[str]] = None
    merge_branches: List[str] = Field(default_factory=list)

    @field_validator("push_branches", "merge_branches")
    @classmethod
    def branches_must_not_be_blank(
        cls, value: Optional[List[str]]
    ) -> Optional[List[str]]:
        if value is None:
            return value
        for branch in value:
            if not branch.strip():
                raise ValueError("branches must not contain blank values")
        return value


class ManualWebhookCommand(WebhookCommandBase):
    password: str = Field(min_length=1)
    rate_limit: RateLimitConfig = Field(default_factory=RateLimitConfig)


class CommandsConfig(BaseModel):
    github: List[GitHubWebhookCommand] = Field(default_factory=list)
    manual: List[ManualWebhookCommand] = Field(default_factory=list)


class CommandResult(BaseModel):
    command: str
    returncode: int
    stdout: str
    stderr: str
