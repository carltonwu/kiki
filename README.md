# Kiki

Discord chat bot. Monorepo layout:

| Path | What |
|---|---|
| `services/ingester/` | Rust service: Discord Gateway, message ingestion, conversation routing |
| `tools/backfill/` | Python one-off: backfill a guild's history into SQLite |
| `.railway/railway.ts` | Railway infrastructure as code (Postgres + services) |
| `docker-compose.yml` | Local Postgres |

## Local development

```sh
cp .env.example .env
docker compose up -d --wait           # Postgres 18 on localhost:54329
cargo run -p ingester -- migrate      # apply migrations
cargo run -p ingester                 # run; health check at http://localhost:8080/health
```

Add a migration: create `services/ingester/migrations/NNNN_description.sql`. Migrations are
embedded in the binary and applied by `ingester migrate`, which Railway runs before each deploy.

## Deploying (Railway)

Infrastructure is defined in `.railway/railway.ts`. Services build from GitHub on push to `main`.

```sh
cd .railway && npm install && cd ..
railway link                          # select the project
railway config plan                   # review changes
railway config apply                  # apply them
railway variable set --service ingester DISCORD_BOT_TOKEN=... DISCORD_GUILD_ID=...
```

Only one ingester instance holds the Discord Gateway connection at a time: instances take a
Postgres advisory lock first, so during a deploy the new instance waits until the old one has
shut down.
