import asyncio
import base64
import hashlib
import hmac
import json
import shlex
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi import BackgroundTasks
from fastapi import HTTPException

from app.models.webhook import (
    CommandResult,
    CommandsConfig,
    GitHubWebhookCommand,
    ManualWebhookCommand,
    RateLimitConfig,
)
from app.routers import webhook
from app.utils.webhook import (
    CommandExecutionError,
    execute_commands,
    load_commands_config,
    verify_github_signature,
)
from app.service.notification.telegram.api import TelegramNotificationService
from app.service.notification.telegram.config import TelegramConfig


def github_signature(secret: str, body: bytes) -> str:
    digest = hmac.new(secret.encode("utf-8"), body, hashlib.sha256).hexdigest()
    return f"sha256={digest}"


class WebhookConfigTests(unittest.TestCase):
    def test_load_commands_config_reads_github_scenarios(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "commands.json"
            path.write_text(
                json.dumps(
                    {
                        "github": [
                            {
                                "name": "backend-deploy",
                                "route": "/webhooks/github/backend",
                                "secret": "secret",
                                "push_branches": ["master"],
                                "merge_branches": ["master"],
                                "commands": ["echo ok"],
                            }
                        ]
                    }
                ),
                encoding="utf-8",
            )

            config = load_commands_config(path)

        self.assertEqual(len(config.github), 1)
        self.assertEqual(config.github[0].name, "backend-deploy")
        self.assertEqual(config.github[0].route, "/webhooks/github/backend")
        self.assertEqual(config.github[0].push_branches, ["master"])
        self.assertEqual(config.github[0].merge_branches, ["master"])

    def test_missing_commands_config_loads_empty_config(self):
        config = load_commands_config(Path("missing-commands.json"))

        self.assertEqual(config.github, [])

    def test_github_scenario_route_must_be_absolute(self):
        with self.assertRaises(ValueError):
            GitHubWebhookCommand(
                name="bad-route",
                route="webhooks/github/bad",
                secret="secret",
                commands=["echo ok"],
            )

    def test_create_router_registers_configured_routes_and_fallback(self):
        config = CommandsConfig(
            github=[
                GitHubWebhookCommand(
                    name="backend-deploy",
                    route="/webhooks/github/backend",
                    secret="secret",
                    commands=["echo ok"],
                )
            ],
            manual=[
                ManualWebhookCommand(
                    name="manual-deploy",
                    route="/manual/backend",
                    password="password",
                    commands=["echo ok"],
                )
            ],
        )

        test_router = webhook.create_router(config)
        route_paths = {route.path for route in test_router.routes}

        self.assertIn("/webhooks/github/backend", route_paths)
        self.assertIn("/manual/backend", route_paths)
        self.assertIn("/webhooks/github/{unknown_path:path}", route_paths)


class GithubSignatureTests(unittest.TestCase):
    def test_verify_github_signature_uses_sha256_hmac(self):
        body = b'{"ref":"refs/heads/master"}'
        signature = github_signature("secret", body)

        self.assertTrue(verify_github_signature("secret", body, signature))
        self.assertFalse(verify_github_signature("secret", body, "sha256=bad"))
        self.assertFalse(verify_github_signature("secret", body, None))


class CommandExecutionTests(unittest.TestCase):
    def test_execute_commands_runs_in_order_and_supports_cd(self):
        with tempfile.TemporaryDirectory() as tmp:
            cwd_name = Path(tmp).name
            python_cmd = (
                f"{shlex.quote(sys.executable)} -c "
                "'from pathlib import Path; print(Path.cwd().name)'"
            )

            results = execute_commands([f"cd {shlex.quote(tmp)}", python_cmd])

        self.assertEqual([result.returncode for result in results], [0, 0])
        self.assertEqual(results[-1].stdout.strip(), cwd_name)

    def test_execute_commands_stops_after_first_failure(self):
        fail_cmd = f"{shlex.quote(sys.executable)} -c 'import sys; sys.exit(7)'"
        skipped_cmd = f"{shlex.quote(sys.executable)} -c 'print(\"should not run\")'"

        with self.assertRaises(CommandExecutionError) as error:
            execute_commands([fail_cmd, skipped_cmd])

        self.assertEqual(error.exception.result.returncode, 7)
        self.assertEqual(len(error.exception.completed_results), 1)

    def test_execute_commands_supports_shell_background_syntax(self):
        with tempfile.TemporaryDirectory() as tmp:
            pid_path = Path(tmp) / "backend.pid"
            results = execute_commands(
                [
                    f"cd {shlex.quote(tmp)}",
                    "sleep 0.1 > backend.log 2>&1 & echo $! > backend.pid",
                ],
                timeout_seconds=1,
            )

            self.assertEqual([result.returncode for result in results], [0, 0])
            self.assertTrue(pid_path.read_text(encoding="utf-8").strip())


class WebhookHandlerTests(unittest.TestCase):
    def setUp(self):
        webhook._manual_rate_limits.clear()

    def test_handle_github_webhook_rejects_invalid_signature(self):
        scenario = GitHubWebhookCommand(
            name="backend-deploy",
            route="/webhooks/github/backend",
            secret="secret",
            commands=["echo ok"],
        )

        async def runner(commands):
            raise AssertionError("runner must not be called")

        async def call_handler():
            return await webhook.handle_github_webhook(
                scenario=scenario,
                body=b"{}",
                signature_header="sha256=bad",
                runner=runner,
            )

        with self.assertRaises(HTTPException) as error:
            asyncio.run(call_handler())

        self.assertEqual(error.exception.status_code, 401)

    def test_handle_github_webhook_schedules_background_task_and_returns_success(self):
        scenario = GitHubWebhookCommand(
            name="backend-deploy",
            route="/webhooks/github/backend",
            secret="secret",
            push_branches=["master"],
            commands=["echo ok"],
        )
        body = json.dumps(
            {
                "ref": "refs/heads/master",
                "created": False,
                "deleted": False,
                "head_commit": {
                    "message": "Deploy backend\n\nKeep it short.",
                    "author": {
                        "name": "Alice Example",
                        "email": "alice@example.com",
                    },
                },
            }
        ).encode("utf-8")
        signature = github_signature("secret", body)
        calls = []
        notifications = []

        async def runner(commands):
            calls.append(commands)
            return []

        async def call_handler():
            background_tasks = BackgroundTasks()
            response = await webhook.handle_github_webhook(
                scenario=scenario,
                body=body,
                signature_header=signature,
                event_header="push",
                background_tasks=background_tasks,
                runner=runner,
            )
            self.assertEqual(calls, [])
            with patch.object(
                webhook.notification_service,
                "send_text",
                side_effect=lambda text: notifications.append(text) or True,
            ):
                await background_tasks()
            return response

        response = asyncio.run(call_handler())

        self.assertEqual(response["status"], "success")
        self.assertEqual(response["name"], "backend-deploy")
        self.assertEqual(response["message"], "Webhook scenario accepted")
        self.assertEqual(calls, [["echo ok"]])
        self.assertIn("Deploy backend", notifications[0])
        self.assertIn("👤 <b>Author:</b> Alice Example", notifications[0])
        self.assertIn("📧 <code>alice@example.com</code>", notifications[0])

    def test_success_message_uses_html_template_and_escapes_context(self):
        scenario = GitHubWebhookCommand(
            name="backend <deploy>",
            route="/webhooks/github/backend",
            secret="secret",
            commands=["echo ok"],
        )
        context = webhook.GitHubEventContext(
            event="push",
            branch="master <prod>",
            commit_message="Deploy <backend>\n\nDo it safely.",
            author_name="Alice <Example>",
            author_email="alice@example.com",
        )

        message = webhook._success_message(
            scenario=scenario,
            results=[
                CommandResult(command="echo ok", returncode=0, stdout="ok", stderr="")
            ],
            context=context,
        )

        self.assertEqual(
            message,
            "\n".join(
                [
                    "✅ <b>Webhook completed</b>",
                    "",
                    "🚀 <b>Deploy:</b> <code>backend &lt;deploy&gt;</code>",
                    "⚙️ <b>Commands executed:</b> <code>1</code>",
                    "🌿 <b>Branch:</b> <code>master &lt;prod&gt;</code>",
                    "",
                    "📝 <b>Commit:</b>",
                    "<blockquote>Deploy &lt;backend&gt; Do it safely.</blockquote>",
                    "",
                    "👤 <b>Author:</b> Alice &lt;Example&gt;",
                    "📧 <code>alice@example.com</code>",
                ]
            ),
        )

    def test_handle_github_webhook_ignores_new_branch_push(self):
        scenario = GitHubWebhookCommand(
            name="backend-deploy",
            route="/webhooks/github/backend",
            secret="secret",
            push_branches=["master"],
            commands=["echo ok"],
        )
        body = json.dumps(
            {"ref": "refs/heads/master", "created": True, "deleted": False}
        ).encode("utf-8")
        signature = github_signature("secret", body)

        async def runner(commands):
            raise AssertionError("runner must not be called")

        async def call_handler():
            background_tasks = BackgroundTasks()
            response = await webhook.handle_github_webhook(
                scenario=scenario,
                body=body,
                signature_header=signature,
                event_header="push",
                background_tasks=background_tasks,
                runner=runner,
            )
            await background_tasks()
            return response

        response = asyncio.run(call_handler())

        self.assertEqual(response["status"], "ignored")

    def test_handle_github_webhook_ignores_unconfigured_push_branch(self):
        scenario = GitHubWebhookCommand(
            name="backend-deploy",
            route="/webhooks/github/backend",
            secret="secret",
            push_branches=["master"],
            commands=["echo ok"],
        )
        body = json.dumps(
            {"ref": "refs/heads/feature", "created": False, "deleted": False}
        ).encode("utf-8")
        signature = github_signature("secret", body)

        async def runner(commands):
            raise AssertionError("runner must not be called")

        async def call_handler():
            response = await webhook.handle_github_webhook(
                scenario=scenario,
                body=body,
                signature_header=signature,
                event_header="push",
                background_tasks=BackgroundTasks(),
                runner=runner,
            )
            return response

        response = asyncio.run(call_handler())

        self.assertEqual(response["status"], "ignored")

    def test_handle_github_webhook_ignores_non_merge_pull_request_event(self):
        scenario = GitHubWebhookCommand(
            name="backend-deploy",
            route="/webhooks/github/backend",
            secret="secret",
            merge_branches=["master"],
            commands=["echo ok"],
        )
        body = json.dumps(
            {
                "action": "opened",
                "pull_request": {
                    "merged": False,
                    "base": {"ref": "master"},
                    "title": "Update backend",
                },
            }
        ).encode("utf-8")
        signature = github_signature("secret", body)

        async def runner(commands):
            raise AssertionError("runner must not be called")

        async def call_handler():
            response = await webhook.handle_github_webhook(
                scenario=scenario,
                body=body,
                signature_header=signature,
                event_header="pull_request",
                background_tasks=BackgroundTasks(),
                runner=runner,
            )
            return response

        response = asyncio.run(call_handler())

        self.assertEqual(response["status"], "ignored")

    def test_handle_github_webhook_accepts_configured_merge_event(self):
        scenario = GitHubWebhookCommand(
            name="backend-deploy",
            route="/webhooks/github/backend",
            secret="secret",
            merge_branches=["master"],
            commands=["echo ok"],
        )
        body = json.dumps(
            {
                "action": "closed",
                "pull_request": {
                    "merged": True,
                    "base": {"ref": "master"},
                    "title": "Deploy backend",
                    "user": {"login": "alice"},
                },
            }
        ).encode("utf-8")
        signature = github_signature("secret", body)
        calls = []

        async def runner(commands):
            calls.append(commands)
            return []

        async def call_handler():
            background_tasks = BackgroundTasks()
            response = await webhook.handle_github_webhook(
                scenario=scenario,
                body=body,
                signature_header=signature,
                event_header="pull_request",
                background_tasks=background_tasks,
                runner=runner,
            )
            await background_tasks()
            return response

        response = asyncio.run(call_handler())

        self.assertEqual(response["status"], "success")
        self.assertEqual(calls, [["echo ok"]])

    def test_handle_github_webhook_returns_conflict_while_background_task_is_pending(
        self,
    ):
        scenario = GitHubWebhookCommand(
            name="backend-deploy",
            route="/webhooks/github/backend",
            secret="secret",
            commands=["sleep 1"],
        )
        body = b'{"ref":"refs/heads/master","created":false,"deleted":false}'
        signature = github_signature("secret", body)

        async def runner(commands):
            return []

        async def call_twice():
            background_tasks = BackgroundTasks()
            await webhook.handle_github_webhook(
                scenario=scenario,
                body=body,
                signature_header=signature,
                event_header="push",
                background_tasks=background_tasks,
                runner=runner,
            )

            with self.assertRaises(HTTPException) as error:
                await webhook.handle_github_webhook(
                    scenario=scenario,
                    body=body,
                    signature_header=signature,
                    event_header="push",
                    background_tasks=BackgroundTasks(),
                    runner=runner,
                )
            self.assertEqual(error.exception.status_code, 409)

            await background_tasks()

        asyncio.run(call_twice())

    def test_background_command_error_releases_scenario_lock(self):
        scenario = GitHubWebhookCommand(
            name="backend-deploy",
            route="/webhooks/github/backend",
            secret="secret",
            commands=["exit 1"],
        )
        body = b'{"ref":"refs/heads/master","created":false,"deleted":false}'
        signature = github_signature("secret", body)
        result = CommandResult(
            command="exit 1",
            returncode=1,
            stdout="",
            stderr="failed",
        )

        async def failing_runner(commands):
            raise CommandExecutionError(result=result, completed_results=[result])

        async def successful_runner(commands):
            return []

        async def run_test():
            background_tasks = BackgroundTasks()
            response = await webhook.handle_github_webhook(
                scenario=scenario,
                body=body,
                signature_header=signature,
                event_header="push",
                background_tasks=background_tasks,
                runner=failing_runner,
            )
            await background_tasks()
            self.assertEqual(response["status"], "success")

            next_tasks = BackgroundTasks()
            next_response = await webhook.handle_github_webhook(
                scenario=scenario,
                body=body,
                signature_header=signature,
                event_header="push",
                background_tasks=next_tasks,
                runner=successful_runner,
            )
            await next_tasks()
            self.assertEqual(next_response["status"], "success")

        asyncio.run(run_test())


class ManualWebhookHandlerTests(unittest.TestCase):
    def setUp(self):
        webhook._manual_rate_limits.clear()

    def test_handle_manual_webhook_requires_basic_password_prompt(self):
        scenario = ManualWebhookCommand(
            name="manual-deploy",
            route="/manual/backend",
            password="password",
            commands=["echo ok"],
        )

        async def runner(commands):
            raise AssertionError("runner must not be called")

        async def call_handler():
            return await webhook.handle_manual_webhook(
                scenario=scenario,
                authorization_header=None,
                client_id="127.0.0.1",
                runner=runner,
            )

        with self.assertRaises(HTTPException) as error:
            asyncio.run(call_handler())

        self.assertEqual(error.exception.status_code, 401)
        self.assertEqual(
            error.exception.headers["WWW-Authenticate"],
            'Basic realm="github-webhooker"',
        )

    def test_handle_manual_webhook_schedules_background_task_after_password(self):
        scenario = ManualWebhookCommand(
            name="manual-deploy",
            route="/manual/backend",
            password="password",
            commands=["echo ok"],
        )
        authorization = "Basic " + base64.b64encode(b"user:password").decode("ascii")
        calls = []

        async def runner(commands):
            calls.append(commands)
            return []

        async def call_handler():
            background_tasks = BackgroundTasks()
            response = await webhook.handle_manual_webhook(
                scenario=scenario,
                authorization_header=authorization,
                client_id="127.0.0.1",
                background_tasks=background_tasks,
                runner=runner,
            )
            self.assertEqual(calls, [])
            await background_tasks()
            return response

        response = asyncio.run(call_handler())

        self.assertEqual(response["status"], "success")
        self.assertEqual(response["name"], "manual-deploy")
        self.assertEqual(calls, [["echo ok"]])

    def test_handle_manual_webhook_rate_limits_password_attempts(self):
        scenario = ManualWebhookCommand(
            name="manual-deploy",
            route="/manual/backend",
            password="password",
            commands=["echo ok"],
            rate_limit=RateLimitConfig(requests=1, seconds=60),
        )
        authorization = "Basic " + base64.b64encode(b"user:wrong").decode("ascii")

        async def runner(commands):
            raise AssertionError("runner must not be called")

        async def call_handler():
            for expected_status in (401, 429):
                with self.assertRaises(HTTPException) as error:
                    await webhook.handle_manual_webhook(
                        scenario=scenario,
                        authorization_header=authorization,
                        client_id="127.0.0.1",
                        runner=runner,
                    )
                self.assertEqual(error.exception.status_code, expected_status)

        asyncio.run(call_handler())


class TelegramNotificationTests(unittest.TestCase):
    def test_telegram_service_skips_missing_settings(self):
        self.assertFalse(
            TelegramNotificationService(
                TelegramConfig(bot_token=None, chat_id="1")
            ).send_text("ok")
        )
        self.assertFalse(
            TelegramNotificationService(
                TelegramConfig(bot_token="token", chat_id=None)
            ).send_text("ok")
        )

    def test_telegram_service_sends_single_html_message(self):
        service = TelegramNotificationService(
            TelegramConfig(bot_token="token", chat_id="123")
        )

        with patch("app.service.notification.telegram.api.urlopen") as urlopen:
            self.assertTrue(service.send_text("deploy <b>ok</b>"))

        self.assertEqual(urlopen.call_count, 1)
        request = urlopen.call_args.args[0]
        self.assertEqual(
            request.full_url,
            "https://api.telegram.org/bottoken/sendMessage",
        )
        self.assertEqual(
            json.loads(request.data.decode("utf-8")),
            {
                "chat_id": "123",
                "text": "deploy <b>ok</b>",
                "parse_mode": "HTML",
                "disable_web_page_preview": True,
            },
        )
