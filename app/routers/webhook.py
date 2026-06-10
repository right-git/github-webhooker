import threading
from typing import Awaitable, Callable, Sequence

from fastapi import APIRouter, HTTPException, Request
from loguru import logger

from app.config.config import settings
from app.models.webhook import CommandResult, CommandsConfig, GitHubWebhookCommand
from app.utils.webhook import (
    CommandExecutionError,
    load_commands_config,
    run_commands_async,
    verify_github_signature,
)

WebhookRunner = Callable[[Sequence[str]], Awaitable[list[CommandResult]]]
_scenario_locks: dict[str, threading.Lock] = {}


async def handle_github_webhook(
    scenario: GitHubWebhookCommand,
    body: bytes,
    signature_header: str | None,
    runner: WebhookRunner = run_commands_async,
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

    try:
        logger.info("GitHub webhook triggered: {}", scenario.name)
        results = await runner(scenario.commands)
    except CommandExecutionError as exc:
        logger.error(
            "GitHub webhook command failed: name={} command='{}'",
            scenario.name,
            exc.result.command,
        )
        raise HTTPException(
            status_code=500,
            detail={
                "status": "error",
                "message": "Webhook command failed",
                "name": scenario.name,
                "failed_command": exc.result.model_dump(),
                "completed_results": [
                    result.model_dump() for result in exc.completed_results
                ],
            },
        ) from exc
    finally:
        lock.release()

    logger.info("GitHub webhook completed: {}", scenario.name)
    return {
        "status": "success",
        "message": "Webhook scenario executed",
        "name": scenario.name,
        "commands_executed": len(results),
        "results": [result.model_dump() for result in results],
    }


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

    webhook_router.add_api_route(
        "/webhooks/github/{unknown_path:path}",
        _unknown_github_webhook,
        methods=["POST"],
        name="unknown-github-webhook",
    )
    return webhook_router


def _github_endpoint(scenario: GitHubWebhookCommand):
    async def endpoint(request: Request):
        return await handle_github_webhook(
            scenario=scenario,
            body=await request.body(),
            signature_header=request.headers.get("X-Hub-Signature-256"),
        )

    endpoint.__name__ = f"github_webhook_{scenario.name.replace('-', '_')}"
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
