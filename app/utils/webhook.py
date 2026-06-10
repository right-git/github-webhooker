import asyncio
import hashlib
import hmac
import json
import shlex
import subprocess
from pathlib import Path
from typing import Sequence
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from loguru import logger
from pydantic import ValidationError

from app.models.webhook import CommandResult, CommandsConfig


class CommandExecutionError(RuntimeError):
    def __init__(
        self, result: CommandResult, completed_results: list[CommandResult]
    ) -> None:
        super().__init__(f"Command failed: {result.command}")
        self.result = result
        self.completed_results = completed_results


def load_commands_config(path: Path | str) -> CommandsConfig:
    config_path = Path(path)
    if not config_path.exists():
        logger.warning("Webhook commands config not found: {}", config_path)
        return CommandsConfig()

    try:
        return CommandsConfig.model_validate(
            json.loads(config_path.read_text(encoding="utf-8"))
        )
    except json.JSONDecodeError as exc:
        logger.error("Invalid webhook commands JSON in {}: {}", config_path, exc)
        raise ValueError(f"Invalid JSON in {config_path}") from exc
    except ValidationError as exc:
        logger.error("Invalid webhook commands config in {}: {}", config_path, exc)
        raise ValueError(f"Invalid commands config in {config_path}") from exc


def verify_github_signature(
    secret: str, body: bytes, signature_header: str | None
) -> bool:
    if not signature_header or not signature_header.startswith("sha256="):
        return False

    digest = hmac.new(secret.encode("utf-8"), body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(f"sha256={digest}", signature_header)


async def run_commands_async(
    commands: Sequence[str], timeout_seconds: int = 600
) -> list[CommandResult]:
    return await asyncio.to_thread(execute_commands, commands, timeout_seconds)


def execute_commands(
    commands: Sequence[str], timeout_seconds: int = 600
) -> list[CommandResult]:
    cwd = Path.cwd()
    results: list[CommandResult] = []

    for command in commands:
        result, cwd = _run_command(command, cwd, timeout_seconds)
        results.append(result)
        _log_command_result(result)

        if result.returncode != 0:
            raise CommandExecutionError(result=result, completed_results=results)

    return results


def _run_command(
    command: str, cwd: Path, timeout_seconds: int
) -> tuple[CommandResult, Path]:
    logger.info("Executing webhook command: {}", command)
    args = shlex.split(command)
    if not args:
        return (
            CommandResult(
                command=command, returncode=2, stdout="", stderr="Blank command"
            ),
            cwd,
        )

    if args[0] == "cd":
        return _change_dir(command, args, cwd)

    try:
        completed = subprocess.run(
            args,
            cwd=cwd,
            capture_output=True,
            check=False,
            stdin=subprocess.DEVNULL,
            text=True,
            timeout=timeout_seconds,
        )
        result = CommandResult(
            command=command,
            returncode=completed.returncode,
            stdout=completed.stdout,
            stderr=completed.stderr,
        )
    except OSError as exc:
        result = CommandResult(
            command=command, returncode=127, stdout="", stderr=str(exc)
        )
    except subprocess.TimeoutExpired as exc:
        result = CommandResult(
            command=command,
            returncode=124,
            stdout=exc.stdout or "",
            stderr=f"Command timed out after {timeout_seconds} seconds",
        )

    return result, cwd


def _change_dir(
    command: str, args: list[str], cwd: Path
) -> tuple[CommandResult, Path]:
    if len(args) != 2:
        return (
            CommandResult(
                command=command,
                returncode=2,
                stdout="",
                stderr="cd command must have exactly one path",
            ),
            cwd,
        )

    target = Path(args[1]).expanduser()
    if not target.is_absolute():
        target = cwd / target
    target = target.resolve()

    if not target.is_dir():
        return (
            CommandResult(
                command=command,
                returncode=1,
                stdout="",
                stderr=f"Directory does not exist: {target}",
            ),
            cwd,
        )

    return (
        CommandResult(
            command=command,
            returncode=0,
            stdout=f"cwd={target}",
            stderr="",
        ),
        target,
    )


def _log_command_result(result: CommandResult) -> None:
    logger.info(
        "Webhook command finished: command='{}' returncode={}",
        result.command,
        result.returncode,
    )
    if result.stdout:
        logger.info("Webhook command stdout: {}", result.stdout.strip())
    if result.stderr:
        logger.warning("Webhook command stderr: {}", result.stderr.strip())


def send_telegram_message(
    bot_token: str | None, chat_id: str | None, text: str
) -> bool:
    if not bot_token or not chat_id:
        return False

    data = urlencode({"chat_id": chat_id, "text": text}).encode("utf-8")
    request = Request(
        f"https://api.telegram.org/bot{bot_token}/sendMessage",
        data=data,
        method="POST",
    )

    try:
        with urlopen(request, timeout=10):
            return True
    except OSError as exc:
        logger.warning("Telegram notification failed: {}", exc)
        return False
