import asyncio
import base64
import binascii
import html
import hmac
import json
import re
import threading
import time
from dataclasses import dataclass
from typing import Any, Awaitable, Callable, Sequence

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
from app.service.notification import NotificationService
from app.utils.webhook import (
    CommandExecutionError,
    load_commands_config,
    run_commands_async,
    verify_github_signature,
)

WebhookRunner = Callable[[Sequence[str]], Awaitable[list[CommandResult]]]
_scenario_locks: dict[str, threading.Lock] = {}
_manual_rate_limits: dict[tuple[str, str], list[float]] = {}
notification_service = NotificationService.from_telegram(
    settings.bot_token, settings.chat_id
)


@dataclass(frozen=True)
class GitHubEventContext:
    event: str
    branch: str | None = None
    commit_message: str | None = None
    author_name: str | None = None
    author_email: str | None = None


@dataclass(frozen=True)
class GitHubEventDecision:
    should_run: bool
    reason: str
    context: GitHubEventContext | None = None


async def _run_configured_commands(commands: Sequence[str]) -> list[CommandResult]:
    return await run_commands_async(commands, settings.command_timeout_seconds)


async def handle_github_webhook(
    scenario: GitHubWebhookCommand,
    body: bytes,
    signature_header: str | None,
    event_header: str | None = None,
    background_tasks: BackgroundTasks | None = None,
    runner: WebhookRunner = _run_configured_commands,
) -> dict:
    if not verify_github_signature(scenario.secret, body, signature_header):
        logger.warning("Invalid GitHub signature: {}", scenario.name)
        raise HTTPException(
            status_code=401,
            detail={"status": "error", "message": "Invalid GitHub signature"},
        )

    payload = _load_github_payload(body)
    decision = _github_event_decision(scenario, event_header, payload)
    if not decision.should_run:
        logger.info(
            "GitHub webhook ignored: name={} event={} reason={}",
            scenario.name,
            event_header,
            decision.reason,
        )
        return {
            "status": "ignored",
            "message": "GitHub event ignored",
            "name": scenario.name,
            "reason": decision.reason,
        }

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
    background_tasks.add_task(
        _run_webhook_commands, scenario, lock, runner, decision.context
    )
    return {
        "status": "success",
        "message": "Webhook scenario accepted",
        "name": scenario.name,
    }


def _load_github_payload(body: bytes) -> dict[str, Any]:
    try:
        payload = json.loads(body.decode("utf-8") or "{}")
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise HTTPException(
            status_code=400,
            detail={"status": "error", "message": "Invalid GitHub JSON payload"},
        ) from exc

    if not isinstance(payload, dict):
        raise HTTPException(
            status_code=400,
            detail={"status": "error", "message": "Invalid GitHub JSON payload"},
        )
    return payload


def _github_event_decision(
    scenario: GitHubWebhookCommand, event: str | None, payload: dict[str, Any]
) -> GitHubEventDecision:
    if event == "push":
        return _push_event_decision(scenario, payload)
    if event == "pull_request":
        return _pull_request_event_decision(scenario, payload)
    return GitHubEventDecision(False, "unsupported-event")


def _push_event_decision(
    scenario: GitHubWebhookCommand, payload: dict[str, Any]
) -> GitHubEventDecision:
    if payload.get("created") or payload.get("deleted"):
        return GitHubEventDecision(False, "branch-created-or-deleted")

    branch = _branch_from_ref(payload.get("ref"))
    if not branch:
        return GitHubEventDecision(False, "missing-branch")
    if not _branch_allowed(branch, scenario.push_branches):
        return GitHubEventDecision(False, "push-branch-not-allowed")

    commit = _mapping(payload.get("head_commit"))
    author = _mapping(commit.get("author"))
    return GitHubEventDecision(
        True,
        "push-branch-allowed",
        GitHubEventContext(
            event="push",
            branch=branch,
            commit_message=_short_text(commit.get("message")),
            author_name=_string_or_none(author.get("name")),
            author_email=_string_or_none(author.get("email")),
        ),
    )


def _pull_request_event_decision(
    scenario: GitHubWebhookCommand, payload: dict[str, Any]
) -> GitHubEventDecision:
    pull_request = _mapping(payload.get("pull_request"))
    if payload.get("action") != "closed" or pull_request.get("merged") is not True:
        return GitHubEventDecision(False, "pull-request-not-merged")

    base = _mapping(pull_request.get("base"))
    branch = _normalize_branch(_string_or_none(base.get("ref")))
    if not branch:
        return GitHubEventDecision(False, "missing-merge-branch")
    if not _branch_allowed(branch, scenario.merge_branches):
        return GitHubEventDecision(False, "merge-branch-not-allowed")

    merged_by = _mapping(pull_request.get("merged_by"))
    user = _mapping(pull_request.get("user"))
    return GitHubEventDecision(
        True,
        "merge-branch-allowed",
        GitHubEventContext(
            event="pull_request",
            branch=branch,
            commit_message=_short_text(pull_request.get("title")),
            author_name=_string_or_none(merged_by.get("login"))
            or _string_or_none(user.get("login")),
        ),
    )


def _mapping(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _string_or_none(value: Any) -> str | None:
    return value if isinstance(value, str) and value.strip() else None


def _branch_from_ref(ref: Any) -> str | None:
    if not isinstance(ref, str):
        return None
    if not ref.startswith("refs/heads/"):
        return None
    return _normalize_branch(ref)


def _normalize_branch(branch: str | None) -> str | None:
    if not branch:
        return None
    if branch.startswith("refs/heads/"):
        return branch.removeprefix("refs/heads/")
    return branch


def _branch_allowed(branch: str, allowed_branches: Sequence[str] | None) -> bool:
    if allowed_branches is None:
        return True
    allowed = {_normalize_branch(item) for item in allowed_branches}
    return branch in allowed


def _short_text(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    compact = " ".join(line.strip() for line in value.splitlines() if line.strip())
    if not compact:
        return None

    sentences = re.split(r"(?<=[.!?])\s+", compact)
    short = " ".join(sentences[:2])
    if len(short) > 240:
        return f"{short[:237].rstrip()}..."
    return short


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
        attempt
        for attempt in _manual_rate_limits.get(key, [])
        if attempt > window_start
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
    context: GitHubEventContext | None = None,
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
        await _notify(_failure_message(scenario, exc.result, context))
    except Exception as exc:
        logger.exception("Webhook failed: name={} error={}", scenario.name, exc)
        await _notify(_unexpected_failure_message(scenario, exc, context))
    else:
        logger.info("Webhook completed: {}", scenario.name)
        await _notify(_success_message(scenario, results, context))
    finally:
        lock.release()


async def _notify(text: str) -> None:
    await asyncio.to_thread(notification_service.send_text, text)


def _success_message(
    scenario: WebhookCommandBase,
    results: Sequence[CommandResult],
    context: GitHubEventContext | None,
) -> str:
    lines = [
        "✅ <b>Webhook completed</b>",
        "",
        f"🚀 <b>Deploy:</b> <code>{_html(scenario.name)}</code>",
        f"⚙️ <b>Commands executed:</b> <code>{len(results)}</code>",
    ]
    lines.extend(_context_lines(context, include_commit=True))
    return "\n".join(lines)


def _failure_message(
    scenario: WebhookCommandBase,
    result: CommandResult,
    context: GitHubEventContext | None,
) -> str:
    lines = [
        "❌ <b>Webhook failed</b>",
        "",
        f"🚀 <b>Deploy:</b> <code>{_html(scenario.name)}</code>",
        f"⚙️ <b>Command:</b> <code>{_html(result.command)}</code>",
    ]
    lines.extend(_context_lines(context, include_commit=True))
    lines.extend(_error_lines(result.stderr or str(result.returncode)))
    return "\n".join(lines)


def _unexpected_failure_message(
    scenario: WebhookCommandBase,
    exc: Exception,
    context: GitHubEventContext | None,
) -> str:
    lines = [
        "❌ <b>Webhook failed</b>",
        "",
        f"🚀 <b>Deploy:</b> <code>{_html(scenario.name)}</code>",
    ]
    lines.extend(_context_lines(context, include_commit=True))
    lines.extend(_error_lines(str(exc)))
    return "\n".join(lines)


def _context_lines(
    context: GitHubEventContext | None, include_commit: bool = False
) -> list[str]:
    if context is None:
        return []

    lines = []
    if context.branch:
        lines.append(f"🌿 <b>Branch:</b> <code>{_html(context.branch)}</code>")
    if include_commit and context.commit_message:
        lines.append("")
        lines.append("📝 <b>Commit:</b>")
        lines.append(
            f"<blockquote>{_html(_compact_text(context.commit_message))}</blockquote>"
        )

    if context.author_name or context.author_email:
        lines.append("")
    if context.author_name:
        lines.append(f"👤 <b>Author:</b> {_html(context.author_name)}")
    if context.author_email:
        lines.append(f"📧 <code>{_html(context.author_email)}</code>")
    return lines


def _error_lines(error: str) -> list[str]:
    return ["", "⚠️ <b>Error:</b>", f"<blockquote>{_html(error)}</blockquote>"]


def _html(value: str) -> str:
    return html.escape(value, quote=False)


def _compact_text(value: str) -> str:
    return " ".join(line.strip() for line in value.splitlines() if line.strip())


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
            event_header=request.headers.get("X-GitHub-Event"),
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
