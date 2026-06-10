from typing import List

from pydantic import BaseModel, Field, field_validator


class GitHubWebhookCommand(BaseModel):
    name: str = Field(min_length=1)
    route: str = Field(min_length=1)
    secret: str = Field(min_length=1)
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


class CommandsConfig(BaseModel):
    github: List[GitHubWebhookCommand] = Field(default_factory=list)


class CommandResult(BaseModel):
    command: str
    returncode: int
    stdout: str
    stderr: str
