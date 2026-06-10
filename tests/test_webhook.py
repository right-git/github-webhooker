import asyncio
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

from app.models.webhook import CommandResult, CommandsConfig, GitHubWebhookCommand
from app.routers import webhook
from app.utils.webhook import (
    CommandExecutionError,
    execute_commands,
    load_commands_config,
    send_telegram_message,
    verify_github_signature,
)


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
            ]
        )

        test_router = webhook.create_router(config)
        route_paths = {route.path for route in test_router.routes}

        self.assertIn("/webhooks/github/backend", route_paths)
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


class WebhookHandlerTests(unittest.TestCase):
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
            commands=["echo ok"],
        )
        body = b'{"ref":"refs/heads/master"}'
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
                background_tasks=background_tasks,
                runner=runner,
            )
            self.assertEqual(calls, [])
            await background_tasks()
            return response

        response = asyncio.run(call_handler())

        self.assertEqual(response["status"], "success")
        self.assertEqual(response["name"], "backend-deploy")
        self.assertEqual(response["message"], "Webhook scenario accepted")
        self.assertEqual(calls, [["echo ok"]])

    def test_handle_github_webhook_returns_conflict_while_background_task_is_pending(self):
        scenario = GitHubWebhookCommand(
            name="backend-deploy",
            route="/webhooks/github/backend",
            secret="secret",
            commands=["sleep 1"],
        )
        body = b"{}"
        signature = github_signature("secret", body)

        async def runner(commands):
            return []

        async def call_twice():
            background_tasks = BackgroundTasks()
            await webhook.handle_github_webhook(
                scenario=scenario,
                body=body,
                signature_header=signature,
                background_tasks=background_tasks,
                runner=runner,
            )

            with self.assertRaises(HTTPException) as error:
                await webhook.handle_github_webhook(
                    scenario=scenario,
                    body=body,
                    signature_header=signature,
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
        body = b"{}"
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
                background_tasks=next_tasks,
                runner=successful_runner,
            )
            await next_tasks()
            self.assertEqual(next_response["status"], "success")

        asyncio.run(run_test())


class TelegramNotificationTests(unittest.TestCase):
    def test_send_telegram_message_skips_missing_settings(self):
        self.assertFalse(send_telegram_message(None, "1", "ok"))
        self.assertFalse(send_telegram_message("token", None, "ok"))

    def test_send_telegram_message_posts_to_telegram(self):
        with patch("app.utils.webhook.urlopen") as urlopen:
            self.assertTrue(send_telegram_message("token", "123", "deploy ok"))

        request = urlopen.call_args.args[0]
        self.assertEqual(
            request.full_url, "https://api.telegram.org/bottoken/sendMessage"
        )
        self.assertIn(b"chat_id=123", request.data)
        self.assertIn(b"text=deploy+ok", request.data)
