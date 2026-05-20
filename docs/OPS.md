# Operations Guide — jtaynl/deer-flow (local-fixes)

This document covers the production-deployment shape used on the `local-fixes`
branch of this fork: Docker Compose (`make up`), managed Postgres, hosted
LLM APIs only, and Caddy as the public HTTPS reverse proxy with basic_auth.

For instance-specific values (domain, DB hostname, credentials), see the
private operator notes — this file is intentionally generic.

## Branch model

```
main          tracks upstream/main one-to-one (clean mirror, never patched)
local-fixes   main + local-only commits (UI branding, prompt tweaks, hotfixes)
              ← this is what the server checks out and builds from
```

See `git log --oneline upstream/main..local-fixes` for the exact set of local
patches at any time.

## Required prereqs on the host

Installed once via `scripts/install-prereqs.sh`-equivalent (see commit history
or the operator's runbook):

- Docker Engine + Compose plugin (from `download.docker.com`)
- Caddy v2 (from `cli.cloudsmith.io`)
- `git`, `make`, `build-essential`, `ufw`
- (Optional) `gh` CLI for `git push` over HTTPS without prompts
- UFW: allow 22, 80, 443; everything else denied. The DeerFlow gateway is
  pinned to `127.0.0.1:2026` (see `.env`/`PORT` below), so 2026 is not
  publicly reachable even if UFW were down.

Add the deploy user to the `docker` group (`usermod -aG docker <user>`); a
fresh login is required to pick up the group, otherwise wrap commands with
`sg docker -c '...'`.

## Repository layout on the host

```
~/deer-flow/                    repo, branch = local-fixes
├── .env                        secrets + env vars (chmod 600)
├── docker/.env → ../.env       SYMLINK — compose substitution reads this
├── config.yaml                 active config (bind-mounted into gateway, RO)
├── extensions_config.json      MCP + skills enable map (bind-mounted RO)
└── backend/.deer-flow/         persistent data (BETTER_AUTH_SECRET, per-user dirs)
```

Public 80/443 → Caddy → `127.0.0.1:2026` (compose `nginx`) → `frontend` /
`gateway:8001`. The internal compose `nginx` does same-origin routing.

## Daily commands

All from `~/deer-flow`. Drop `sg docker -c '...'` once your shell has the
`docker` group.

```bash
make up                          # build (cached) + start, idempotent
make down                        # stop + remove containers (volumes preserved)
make down && make up             # full restart

docker compose -p deer-flow -f docker/docker-compose.yaml logs -f
docker logs --tail 200 -f deer-flow-gateway      # backend only
docker logs --tail 200 -f deer-flow-frontend     # Next.js
docker logs --tail 50  -f deer-flow-nginx        # internal nginx (compose)

sudo systemctl reload caddy      # after /etc/caddy/Caddyfile edits
sudo journalctl -u caddy -f
```

## When to recreate vs. just restart

| Change                                     | Action                          |
| ------------------------------------------ | ------------------------------- |
| Any var in `.env`                          | `make down && make up`          |
| `config.yaml`                              | `make down && make up`          |
| `extensions_config.json`                   | `make down && make up`          |
| Anything under `backend/` or `frontend/`   | `make down && make up` (rebuild) |
| `/etc/caddy/Caddyfile`                     | `sudo systemctl reload caddy`   |

`docker compose restart <svc>` is **not** sufficient for `config.yaml` or
`extensions_config.json` changes — both are single-file bind mounts, which
pin to the source file's inode at container creation. Most editors
(including `Edit` in this fork's tooling) use write-and-rename, which
creates a new inode. The container keeps seeing the old content until it's
recreated.

## Upstream sync workflow

```bash
cd ~/deer-flow

# 1. Fast-forward main to upstream
git fetch upstream main
git checkout main
git merge --ff-only upstream/main
git push origin main                              # keep your fork's main aligned

# 2. Replay local patches on top of new main
git checkout local-fixes
git rebase main                                   # obsolete patches auto-skip
git push --force-with-lease origin local-fixes    # required: rebase rewrites SHAs

# 3. Rebuild + relaunch
make down && make up
```

**Never** use `--force` on `main`. Only `local-fixes` and topic branches.

## Non-obvious gotchas

1. **`docker/.env → ../.env` symlink is required.** Without it, compose
   variable substitution (`${PORT:-…}`, `${UV_EXTRAS:-}`) never sees your
   `.env`. The `env_file: ../.env` directive only passes vars into the
   container at runtime — it doesn't feed compose's own parser.

2. **`PORT=127.0.0.1:2026` in `.env`** pins the compose `nginx` to loopback.
   The compose port spec is `${PORT:-2026}:2026`; setting `PORT` to
   `127.0.0.1:2026` expands to `127.0.0.1:2026:2026` (loopback bind).

3. **`PGSSLMODE=require` belongs in `.env`, not in `DATABASE_URL`.** DeerFlow
   uses two Postgres clients against the same URL: SQLAlchemy + asyncpg
   (engine) and langgraph + psycopg (checkpointer). asyncpg rejects
   `?sslmode=…` (it uses `ssl=`), psycopg rejects `?ssl=…` (it uses
   `sslmode=`). They are mutually incompatible URL syntaxes. Stripping the
   SSL param from the URL and setting the libpq env var `PGSSLMODE=require`
   works for both.

4. **`GATEWAY_CORS_ORIGINS=https://your-domain` is required behind any
   external reverse proxy.** The compose `nginx` rewrites `X-Forwarded-Proto`
   to `http` (since the Caddy → compose-nginx hop is plain HTTP on loopback),
   which makes the gateway's derived request_origin `http://…` while the
   browser's `Origin` header is `https://…`. The CSRF middleware rejects the
   mismatch with `403 Cross-site auth request denied`. Setting
   `GATEWAY_CORS_ORIGINS` to the public origin bypasses the equality check.

5. **`agents_api.enabled: true` in `config.yaml`** is required to use the
   Custom Agents page. Off by default upstream.

6. **`supports_thinking: true` + `supports_reasoning_effort: true` on every
   model entry** are required for the UI mode picker to offer
   Reasoning/Pro/Ultra. Without them, every model is forced to Flash.

7. **`UV_EXTRAS=postgres` in `.env`** is required when `database.backend:
   postgres`. It's passed through to the gateway image build as a Docker
   build arg (`backend/Dockerfile` line 17), which adds `asyncpg` to the
   `uv sync`. Without it the gateway crashes at startup with
   `ImportError: asyncpg is not installed`.

8. **`make doctor` is host-only.** It validates `make dev` (local-Python)
   prereqs: pnpm, node, nginx on the host, plus `.env`/`config.yaml` at
   host-relative paths. None of those checks are useful for Docker
   production. To verify runtime health, look at gateway logs for
   `Application startup complete` and check
   `curl -u <user>:<pw> https://<your-domain>/setup` returns 200.

9. **AIO sandbox image** lives at
   `enterprise-public-cn-beijing.cr.volces.com/vefaas-public/all-in-one-sandbox:latest`.
   First pull from non-CN regions can be slow. Run `make docker-init` if you
   ever lose the local image (e.g., after `docker system prune`).

10. **Gateway image rebuilds on every `make up`.** Backend code is **not**
    bind-mounted; it's baked into the image. So any patch to
    `backend/packages/harness/deerflow/...` needs `make up` (which calls
    `docker compose build` + `up`). The only exceptions are `config.yaml`,
    `extensions_config.json`, and `skills/` — those *are* mounted from the
    host.

11. **Caddy `encode` must skip `/api/*` and `reverse_proxy` needs
    `flush_interval -1`.** Caddy's `encode gzip zstd` buffers compressed
    output until each chunk boundary; the gateway's SSE responses
    (`text/event-stream`) appear to hang for tens of seconds and then dump
    the entire result at once — visible to users as "moving dots, then a
    sudden big response." The Caddyfile must use a request matcher that
    excludes the `/api/*` surface from `encode`, and the `reverse_proxy`
    block must set `flush_interval -1` to forward each chunk immediately
    (matching the internal compose-nginx, which already does this with
    `proxy_buffering off` + `X-Accel-Buffering: no` on streaming routes).

12. **`run_events.backend: db` is required for the chat-history UI.** The
    upstream default is `memory`, which keeps events only in-process. The
    frontend's `GET /api/threads/{id}/runs/{rid}/messages` endpoint reads
    from the `run_events` table, so with `memory` it always returns an
    empty list — the user's own prompt vanishes from the chat the moment
    the UI re-syncs from the server during a streaming run. Set
    `run_events.backend: db` to persist messages and traces into Postgres.

13. **The legacy `checkpointer:` section is required *in addition to*
    `database:`** for the LangGraph Store. Without it, the store falls
    back to `InMemoryStore` (the gateway logs `WARNING - No 'checkpointer'
    section ... Thread list will be lost on server restart`). The
    `database:` section drives application data only (runs, threads_meta,
    feedback, run_events). Both should point at the same Postgres:

    ```yaml
    checkpointer:
      type: postgres
      connection_string: $DATABASE_URL
    database:
      backend: postgres
      postgres_url: $DATABASE_URL
    ```

    psycopg (which the checkpointer + store use) reads `PGSSLMODE=require`
    from the env, so the URL still has no SSL query param.

## Postgres tables managed by DeerFlow

On a fresh deploy with both `database:` and `checkpointer:` configured plus
`run_events.backend: db`, the gateway's startup creates 11 tables in your
Postgres database:

| Source | Tables |
|---|---|
| DeerFlow (`database:` via SQLAlchemy) | `feedback`, `run_events`, `runs`, `threads_meta`, `users` |
| langgraph checkpointer (`database:` or `checkpointer:`) | `checkpoint_blobs`, `checkpoint_migrations`, `checkpoint_writes`, `checkpoints` |
| langgraph store (`checkpointer:` section) | `store`, `store_migrations` |

The langgraph migrations (both checkpointer and store) are **not idempotent**
across multiple uvicorn workers on a fresh DB — `GATEWAY_WORKERS=4` means
4 workers race to create the implicit row types, and 3 of them will crash
with `UniqueViolation` on `pg_type_typname_nsp_index`. The winning worker
creates the tables, the losers are auto-replaced by uvicorn, and steady-state
is reached. This is a known transient warning on first-ever start (you'll
see it for `checkpoint_migrations`, and again for `store_migrations` when
the `checkpointer:` section is first added); it is **not** a problem on
restarts where the tables already exist.

If you need a clean slate (e.g., schema drift after major upstream changes):

```bash
docker run --rm -e PGSSLMODE=require postgres:16-alpine \
  psql "$DATABASE_URL" \
  -c "DROP SCHEMA public CASCADE; CREATE SCHEMA public; \
      GRANT ALL ON SCHEMA public TO public;"
```

## Local patches currently carried on `local-fixes`

Run `git log --oneline upstream/main..local-fixes` for the current list.
The hotfix worth knowing about:

**`fix(task_tool): handle AsyncCallbackManager in _find_usage_recorder`**

Recent langgraph passes `config["callbacks"]` as an `AsyncCallbackManager`
(not a list). The upstream iteration `for cb in callbacks:` crashes with
`TypeError` on every subagent dispatch in Ultra mode. The fix normalizes
via `.handlers` when present:

```python
handlers = getattr(callbacks, "handlers", callbacks)
for cb in handlers:
    if hasattr(cb, "record_external_llm_usage_records"):
        return cb
```

Upstream has not (as of 2026-05-20) accepted a fix for this. If/when they
do, our patch becomes a no-op during rebase and git will auto-drop it.

## Quick health check

```bash
curl -sS -u <user>:<password> -o /dev/null -w "HTTPS / -> %%{http_code}\n" \
  https://<your-domain>/                                          # expect 200

sg docker -c 'docker ps --filter name=deer-flow \
  --format "{{.Names}}: {{.Status}}"'                             # expect 3x Up

sg docker -c 'docker logs --since 5m deer-flow-gateway 2>&1 \
  | grep -iE "error|traceback" | grep -v PendingDeprecation | head'
# expect no output (occasional Jina ReadTimeout warnings are non-fatal)
```
