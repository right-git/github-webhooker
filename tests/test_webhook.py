import asyncio
import hashlib
import hmac
import json
import shlex
import sys
import tempfile
import unittest
from pathlib import Path

from fastapi import HTTPException

from app.models.webhook import CommandResult, CommandsConfig, GitHubWebhookCommand
from app.routers import webhook
from app.utils.webhook import (
    CommandExecutionError,
    execute_commands,
    load_commands_config,
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

    def test_handle_github_webhook_returns_success_response(self):
        scenario = GitHubWebhookCommand(
            name="backend-deploy",
            route="/webhooks/github/backend",
            secret="secret",
            commands=["echo ok"],
        )
        body = b'{"ref":"refs/heads/master"}'
        signature = github_signature("secret", body)

        async def runner(commands):
            self.assertEqual(commands, ["echo ok"])
            return []

        async def call_handler():
            return await webhook.handle_github_webhook(
                scenario=scenario,
                body=body,
                signature_header=signature,
                runner=runner,
            )

        response = asyncio.run(call_handler())

        self.assertEqual(response["status"], "success")
        self.assertEqual(response["name"], "backend-deploy")

    def test_handle_github_webhook_returns_conflict_when_scenario_is_running(self):
        scenario = GitHubWebhookCommand(
            name="backend-deploy",
            route="/webhooks/github/backend",
            secret="secret",
            commands=["sleep 1"],
        )
        body = b"{}"
        signature = github_signature("secret", body)
        first_started = asyncio.Event()
        release_first = asyncio.Event()

        async def runner(commands):
            first_started.set()
            await release_first.wait()
            return []

        async def call_twice():
            first_call = asyncio.create_task(
                webhook.handle_github_webhook(
                    scenario=scenario,
                    body=body,
                    signature_header=signature,
                    runner=runner,
                )
            )
            await first_started.wait()

            try:
                with self.assertRaises(HTTPException) as error:
                    await webhook.handle_github_webhook(
                        scenario=scenario,
                        body=body,
                        signature_header=signature,
                        runner=runner,
                    )
                self.assertEqual(error.exception.status_code, 409)
            finally:
                release_first.set()
                await first_call

        asyncio.run(call_twice())

    def test_handle_github_webhook_maps_command_error_to_http_error(self):
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

        async def runner(commands):
            raise CommandExecutionError(result=result, completed_results=[result])

        async def call_handler():
            return await webhook.handle_github_webhook(
                scenario=scenario,
                body=body,
                signature_header=signature,
                runner=runner,
            )

        with self.assertRaises(HTTPException) as error:
            asyncio.run(call_handler())

        self.assertEqual(error.exception.status_code, 500)
        self.assertEqual(error.exception.detail["status"], "error")
