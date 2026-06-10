# GitHub Webhooker

Minimal FastAPI service for running configured shell commands from signed GitHub
webhooks.

## Configuration

Create the local config from the committed example:

```bash
cp commands.json.example commands.json
```

`commands.json` is ignored by git because it contains webhook secrets and real
server paths.

Example:

```json
{
  "github": [
    {
      "name": "backend-deploy",
      "route": "/webhooks/github/backend",
      "secret": "CHANGE_ME",
      "commands": [
        "cd /path/to/project",
        "git pull origin master",
        "systemctl restart my-backend"
      ]
    }
  ]
}
```

Fields:

- `name` - readable scenario name used in logs and API responses.
- `route` - POST path FastAPI registers for this scenario.
- `secret` - GitHub webhook secret. Use the same value in GitHub settings.
- `commands` - commands executed in order after the signature is valid.

Simple commands are split with `shlex` and executed without `shell=True`.
Commands that contain shell operators such as `>`, `2>&1`, `&`, `$`, `|`, or
`;` are executed through `/bin/bash`, so deploy commands like this are supported:

```json
"nohup bash run.sh > logs/backend.log 2>&1 & echo $! > .backend.pid"
```

`cd /path` is supported and changes the working directory for following
commands.

## GitHub Setup

1. In the repository, open `Settings -> Webhooks -> Add webhook`.
2. Set `Payload URL` to your public server URL plus the configured route, for
   example `https://example.com/webhooks/github/backend`.
3. Set `Content type` to `application/json`.
4. Set `Secret` to the same value as `secret` in `commands.json`.
5. Select the events you need, usually `Just the push event`.
6. Save the webhook.

The service verifies `X-Hub-Signature-256` with HMAC SHA-256 before running any
command. Invalid signatures return `401`. If the same scenario is already
running, the endpoint returns `409`.

After a valid request the API returns success immediately and runs configured
commands in FastAPI `BackgroundTasks`. Command output, errors, and completion are
logged after the response is already sent.

## Telegram Notifications

Telegram notifications are optional. Add these values to `.env` when you need
completion/failure messages:

```bash
BOT_TOKEN=123456:telegram-bot-token
CHAT_ID=123456789
```

If either value is empty, notifications are skipped.

## Commands Requiring Root

Do not run this webhook service with `sudo uv run`. That gives root permissions
to the whole HTTP process. Give sudo permission only to the exact command that
needs it.

Example for `systemctl reload nginx`:

```bash
command -v systemctl
sudo visudo -f /etc/sudoers.d/github-webhooker
```

Add a rule for the user that runs this service, replacing `/usr/bin/systemctl`
with the path from `command -v systemctl`:

```text
a1 ALL=(root) NOPASSWD: /usr/bin/systemctl reload nginx
```

Then use a non-interactive sudo command in `commands.json`:

```json
"sudo -n /usr/bin/systemctl reload nginx"
```

`-n` makes sudo fail instead of asking for a password. The command runner also
closes stdin and has a timeout, so password prompts should not hang the webhook
forever.

## Local Run

```bash
uv run --with uvicorn uvicorn app.main:app --reload
```

If you keep the config somewhere else:

```bash
COMMANDS_CONFIG_PATH=/etc/github-webhooker/commands.json uv run --with uvicorn uvicorn app.main:app --reload
```
