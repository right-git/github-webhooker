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

Commands are split with `shlex` and executed through `subprocess.run` without
`shell=True`. Use simple commands with arguments. `cd /path` is supported and
changes the working directory for following commands.

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

## Local Run

```bash
uv run --with uvicorn uvicorn app.main:app --reload
```

If you keep the config somewhere else:

```bash
COMMANDS_CONFIG_PATH=/etc/github-webhooker/commands.json uv run --with uvicorn uvicorn app.main:app --reload
```
