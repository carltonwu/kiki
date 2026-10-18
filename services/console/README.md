# Kiki console

Web console for Kiki: per-server dashboards (activity, latency, failures, costs, every run with
its prompt), a master view for operators, settings/prompt/fact/lingo editing, and chat with Kiki
outside Discord. Design, auth model and the build-vs-reuse decision:
[docs/console-hld.md](../../docs/console-hld.md).

Server-rendered (FastAPI + Jinja), no client-side scripts. Reads Postgres directly for
dashboards and writes facts, lingo, prompts, sessions and the audit log. Settings and chat go
through the ingester's private admin API, so they use the Rust settings registry and Kiki's real
prompt.

## Running locally

```sh
# Ingester: enable the admin API (any 32+ character secret, shared with the console)
ADMIN_API_TOKEN=... cargo run -p ingester

# Console, on http://localhost:8095
cd services/console
DATABASE_URL=postgres://kiki:kiki@localhost:54329/kiki \
CONSOLE_URL=http://localhost:8095 \
DISCORD_CLIENT_ID=... DISCORD_CLIENT_SECRET=... \
CONSOLE_OPERATOR_IDS=<your Discord user ID> \
INGESTER_ADMIN_URL=http://localhost:8080 ADMIN_API_TOKEN=... \
uv run kiki-console
```

Add `http://localhost:8095/auth/callback` as a redirect in the Discord application's OAuth2
settings. `CONSOLE_URL` must be https anywhere but localhost; cookies are `Secure` then.

| Variable | Meaning |
|---|---|
| `DATABASE_URL` | Kiki's Postgres |
| `CONSOLE_URL` | Public base URL (OAuth redirect, cookie flags) |
| `DISCORD_CLIENT_ID`, `DISCORD_CLIENT_SECRET` | Kiki's Discord application (OAuth2) |
| `CONSOLE_OPERATOR_IDS` | Comma-separated Discord user IDs with the master view and every server |
| `INGESTER_ADMIN_URL`, `ADMIN_API_TOKEN` | The ingester's admin API; without them settings and chat are unavailable |
| `PORT` | Default 8095 |

## Tests

```sh
TEST_DATABASE_URL=postgres://kiki:kiki@localhost:54329/kiki uv run pytest
uv run ruff check . && uv run ruff format --check .
```
