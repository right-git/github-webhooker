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
      "push_branches": ["master"],
      "merge_branches": ["master"],
      "commands": [
        "cd /path/to/project",
        "git pull origin master",
        "sudo -n /usr/bin/systemctl restart my-backend"
      ]
    }
  ],
  "manual": [
    {
      "name": "manual-backend-deploy",
      "route": "/manual/backend",
      "password": "CHANGE_ME",
      "rate_limit": {
        "requests": 5,
        "seconds": 60
      },
      "commands": [
        "cd /path/to/project",
        "git pull origin master",
        "nohup bash run.sh > logs/backend.log 2>&1 & echo $! > .backend.pid"
      ]
    }
  ]
}
```

Fields:

- `name` - readable scenario name used in logs and API responses.
- `route` - path FastAPI registers for this scenario.
- `secret` - GitHub webhook secret. Use the same value in GitHub settings.
- `push_branches` - optional list of branch names that can trigger commands on
  GitHub `push` events. If omitted, push events to existing branches are allowed.
- `merge_branches` - optional list of target branches that can trigger commands
  on merged GitHub pull requests. If omitted, pull request events are ignored.
- `commands` - commands executed in order after the signature is valid.

GitHub branch filters prevent accidental deploys:

- New branch pushes are ignored.
- Deleted branch pushes are ignored.
- Pull request events are ignored unless the action is a real merge into a
  configured `merge_branches` target.
- If both GitHub push and pull request events are enabled for the same route,
  configure only the trigger you actually want to avoid duplicate deploys.

`manual` entries create browser-openable routes. Open the configured `route` in
the browser, enter any username and the configured `password`, and the service
will schedule the commands in the background. Manual routes have an in-memory
rate limit per route and client IP:

- `rate_limit.requests` - allowed attempts in the window.
- `rate_limit.seconds` - window size in seconds.

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

Completion/failure notifications include the scenario name, command count or
failed command, branch, a short commit message, and the commit author's name and
email when GitHub sends them in the payload.

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
