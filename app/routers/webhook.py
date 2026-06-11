import asyncio
import base64
import binascii
import hmac
import threading
import time
from typing import Awaitable, Callable, Sequence

from fastapi import APIRouter, BackgroundTasks, HTTPException, Request
from fastapi.responses import HTMLResponse
from loguru import logger

from app.config.config import settings
from app.models.webhook import (
    CommandResult,
    CommandsConfig,
    GitHubWebhookCommand,
    ManualWebhookCommand,
    WebhookCommandBase,
)
from app.utils.webhook import (
    CommandExecutionError,
    load_commands_config,
    run_commands_async,
    send_telegram_message,
    verify_github_signature,
)

WebhookRunner = Callable[[Sequence[str]], Awaitable[list[CommandResult]]]
_scenario_locks: dict[str, threading.Lock] = {}
_manual_rate_limits: dict[tuple[str, str], list[float]] = {}


async def _run_configured_commands(commands: Sequence[str]) -> list[CommandResult]:
    return await run_commands_async(commands, settings.command_timeout_seconds)


async def handle_github_webhook(
    scenario: GitHubWebhookCommand,
    body: bytes,
    signature_header: str | None,
    background_tasks: BackgroundTasks | None = None,
    runner: WebhookRunner = _run_configured_commands,
) -> dict:
    if not verify_github_signature(scenario.secret, body, signature_header):
        logger.warning("Invalid GitHub signature: {}", scenario.name)
        raise HTTPException(
            status_code=401,
            detail={"status": "error", "message": "Invalid GitHub signature"},
        )

    lock = _scenario_locks.setdefault(scenario.route, threading.Lock())
    if not lock.acquire(blocking=False):
        logger.warning("Webhook is already running: {}", scenario.name)
        raise HTTPException(
            status_code=409,
            detail={
                "status": "error",
                "message": "Webhook scenario is already running",
                "name": scenario.name,
            },
        )

    if background_tasks is None:
        background_tasks = BackgroundTasks()

    logger.info("GitHub webhook accepted: {}", scenario.name)
    background_tasks.add_task(_run_webhook_commands, scenario, lock, runner)
    return {
        "status": "success",
        "message": "Webhook scenario accepted",
        "name": scenario.name,
    }


async def handle_manual_webhook(
    scenario: ManualWebhookCommand,
    authorization_header: str | None,
    client_id: str,
    background_tasks: BackgroundTasks | None = None,
    runner: WebhookRunner = _run_configured_commands,
) -> dict:
    if not _allow_manual_request(scenario, client_id):
        raise HTTPException(
            status_code=429,
            detail={"status": "error", "message": "Rate limit exceeded"},
        )

    if not _verify_basic_password(scenario.password, authorization_header):
        raise _manual_auth_error()

    lock = _scenario_locks.setdefault(scenario.route, threading.Lock())
    if not lock.acquire(blocking=False):
        logger.warning("Manual webhook is already running: {}", scenario.name)
        raise HTTPException(
            status_code=409,
            detail={
                "status": "error",
                "message": "Webhook scenario is already running",
                "name": scenario.name,
            },
        )

    if background_tasks is None:
        background_tasks = BackgroundTasks()

    logger.info("Manual webhook accepted: {}", scenario.name)
    background_tasks.add_task(_run_webhook_commands, scenario, lock, runner)
    return {
        "status": "success",
        "message": "Manual webhook scenario accepted",
        "name": scenario.name,
    }


def _allow_manual_request(scenario: ManualWebhookCommand, client_id: str) -> bool:
    now = time.monotonic()
    key = (scenario.route, client_id)
    window_start = now - scenario.rate_limit.seconds
    attempts = [
        attempt for attempt in _manual_rate_limits.get(key, []) if attempt > window_start
    ]
    if len(attempts) >= scenario.rate_limit.requests:
        _manual_rate_limits[key] = attempts
        return False

    attempts.append(now)
    _manual_rate_limits[key] = attempts
    return True


def _verify_basic_password(password: str, authorization_header: str | None) -> bool:
    if not authorization_header or not authorization_header.startswith("Basic "):
        return False

    try:
        encoded = authorization_header.removeprefix("Basic ").strip()
        decoded = base64.b64decode(encoded).decode("utf-8")
    except (binascii.Error, UnicodeDecodeError):
        return False

    entered_password = decoded.split(":", 1)[1] if ":" in decoded else decoded
    return hmac.compare_digest(password, entered_password)


def _manual_auth_error() -> HTTPException:
    return HTTPException(
        status_code=401,
        detail={"status": "error", "message": "Password required"},
        headers={"WWW-Authenticate": 'Basic realm="github-webhooker"'},
    )


async def _run_webhook_commands(
    scenario: WebhookCommandBase,
    lock: threading.Lock,
    runner: WebhookRunner,
) -> None:
    try:
        logger.info("Webhook background started: {}", scenario.name)
        results = await runner(scenario.commands)
    except CommandExecutionError as exc:
        logger.error(
            "Webhook command failed: name={} command='{}'",
            scenario.name,
            exc.result.command,
        )
        await _notify(
            f"Webhook failed: {scenario.name}\n"
            f"Command: {exc.result.command}\n"
            f"Error: {exc.result.stderr or exc.result.returncode}"
        )
    except Exception as exc:
        logger.exception("Webhook failed: name={} error={}", scenario.name, exc)
        await _notify(f"Webhook failed: {scenario.name}\nError: {exc}")
    else:
        logger.info("Webhook completed: {}", scenario.name)
        await _notify(
            f"Webhook completed: {scenario.name}\n"
            f"Commands executed: {len(results)}"
        )
    finally:
        lock.release()


async def _notify(text: str) -> None:
    await asyncio.to_thread(
        send_telegram_message, settings.bot_token, settings.chat_id, text
    )


def create_router(config: CommandsConfig) -> APIRouter:
    webhook_router = APIRouter()
    routes: set[str] = set()

    for scenario in config.github:
        if scenario.route in routes:
            raise ValueError(f"Duplicate webhook route: {scenario.route}")

        routes.add(scenario.route)
        webhook_router.add_api_route(
            scenario.route,
            _github_endpoint(scenario),
            methods=["POST"],
            name=scenario.name,
        )
        logger.info("Registered GitHub webhook route: {}", scenario.route)

    for scenario in config.manual:
        if scenario.route in routes:
            raise ValueError(f"Duplicate webhook route: {scenario.route}")

        routes.add(scenario.route)
        webhook_router.add_api_route(
            scenario.route,
            _manual_endpoint(scenario),
            methods=["GET"],
            name=scenario.name,
            response_class=HTMLResponse,
        )
        logger.info("Registered manual webhook route: {}", scenario.route)

    webhook_router.add_api_route(
        "/webhooks/github/{unknown_path:path}",
        _unknown_github_webhook,
        methods=["POST"],
        name="unknown-github-webhook",
    )
    return webhook_router


def _github_endpoint(scenario: GitHubWebhookCommand):
    async def endpoint(request: Request, background_tasks: BackgroundTasks):
        return await handle_github_webhook(
            scenario=scenario,
            body=await request.body(),
            signature_header=request.headers.get("X-Hub-Signature-256"),
            background_tasks=background_tasks,
        )

    endpoint.__name__ = f"github_webhook_{scenario.name.replace('-', '_')}"
    return endpoint


def _manual_endpoint(scenario: ManualWebhookCommand):
    async def endpoint(request: Request, background_tasks: BackgroundTasks):
        client_id = request.client.host if request.client else "unknown"
        response = await handle_manual_webhook(
            scenario=scenario,
            authorization_header=request.headers.get("Authorization"),
            client_id=client_id,
            background_tasks=background_tasks,
        )
        return HTMLResponse(
            f"<html><body><h1>{response['message']}</h1>"
            f"<p>{response['name']}</p></body></html>"
        )

    endpoint.__name__ = f"manual_webhook_{scenario.name.replace('-', '_')}"
    return endpoint


async def _unknown_github_webhook(unknown_path: str):
    logger.warning("Unknown GitHub webhook route requested: {}", unknown_path)
    raise HTTPException(
        status_code=404,
        detail={
            "status": "error",
            "message": "GitHub webhook route is not configured",
        },
    )


router = create_router(load_commands_config(settings.commands_config_path))
