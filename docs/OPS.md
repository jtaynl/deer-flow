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

`local-fixes` is **merge-maintained** (50+ merge commits — it merges `upstream/main`
directly). **Do NOT rebase it**: a rebase would rewrite 200+ commits and force-push a
destructive history. Merge instead (this is what every prior sync did).

```bash
cd ~/deer-flow

# 1. Fetch upstream + merge into local-fixes (the deploy branch).
git fetch upstream main
git checkout local-fixes
git merge-tree --write-tree local-fixes upstream/main >/dev/null && echo "clean"  # optional conflict preview
git merge --no-edit upstream/main                 # resolve conflicts if any (history shows clean)

# 2. Record the sync in this file: prepend a "Most recent upstream sync" entry
#    (demote the prior one to "Earlier <date>"), then commit.
git add docs/OPS.md
git commit -m "docs(ops): record YYYY-MM-DD sync (N commits, <upstream-tip>) — clean merge"

# 3. Rebuild + verify BEFORE pushing (don't publish a sync you haven't deployed).
make down && make up
# verify: gateway logs "Application startup complete", app :2026 → 200, deps + models intact.

# 3b. RECONCILE THE DB SCHEMA. SINCE 2026-06-25 (#3706) alembic is wired: `make up` runs a hybrid bootstrap
#     in the FastAPI lifespan (empty→create_all+stamp head; legacy→create_all backfill + stamp 0001_baseline
#     + upgrade head; versioned→upgrade head; pg_advisory_lock'd). Upstream column adds now arrive as
#     migrations applied AUTOMATICALLY on startup — alembic OWNS DDL. schema_sync.py is now a read-only
#     SENTINEL (confirms no MISSING columns; it does NOT track server_default/type drift — alembic does):
sg docker -c 'docker exec deer-flow-gateway sh -lc "cd /app/backend && PYTHONPATH=/app/backend .venv/bin/python scripts/schema_sync.py"'
#     (exits 1 on a missing column; --apply still backfills missing columns but with alembic that is a no-op.
#      If 0002's safe_add_column WARNs about server_default/type drift on a PRE-EXISTING column — as on the
#      2026-06-25 sync for runs.token_usage_by_model — apply the one canonical ALTER by hand; see that entry.)

# 3c. SMOKE A REAL CHAT RUN — HTTP 200 + "startup complete" do NOT exercise the run/persist path (that's
#     how the 2026-06-21 token_usage_by_model 500 slipped through). Open https://<domain>/workspace/chats/new
#     and send one message; confirm it streams a reply (no 500). Then check the gateway log is clean:
sg docker -c 'docker logs --since 3m deer-flow-gateway 2>&1 | grep -iE "error|traceback|UndefinedColumn|500" | grep -v PendingDeprecation'   # expect no output

# 4. Push (NO force — a merge appends; it never rewrites history).
git push origin local-fixes

# 5. Keep the fork's main aligned (safe fast-forward only).
git checkout main && git merge --ff-only upstream/main && git push origin main && git checkout local-fixes
```

**Never force-push.** With the merge workflow no `--force` is needed on any branch;
`main` only ever fast-forwards.

**Gotcha — a stale ROOT-owned working-tree dir can block the `git checkout` between
branches** (e.g. `frontend/public/wri/`, a container-written artifact). Without sudo,
`brandon` can't move or delete a root-owned dir (a cross-parent rename needs write *on the
dir* to update `..`). Clear it via a throwaway root container, then re-checkout — git
restores the tracked, brandon-owned copy:

```bash
docker run --rm --entrypoint sh -v ~/deer-flow/frontend/public:/pub <any-present-image> -c 'rm -rf /pub/wri'
git checkout local-fixes
```

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
   Also bump `max_tokens` to at least `32768` (16384 minimum) on these
   entries. With thinking enabled, reasoning tokens count against
   `max_tokens` — the upstream example value of `8192` regularly burns the
   whole budget on the reasoning chain and truncates the visible answer
   to a few tokens. Modern reasoning models (DeepSeek V4, Kimi K2.6,
   Claude Opus 4.x) all support 128K+ completion tokens; `32768` is a
   safe headroom for Ultra-mode answers without any meaningful cost
   increase (you only pay for tokens actually generated).

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

    Working `/etc/caddy/Caddyfile` for this setup (replace placeholders):

    ```caddyfile
    your-domain.example {
        @encodeable {
            not {
                path /api/*
            }
        }
        encode @encodeable gzip zstd

        basic_auth {
            <user> <bcrypt-hash from `caddy hash-password`>
        }

        reverse_proxy 127.0.0.1:2026 {
            flush_interval -1
            header_up Host {host}
            header_up X-Real-IP {remote_host}
            header_up X-Forwarded-Proto {scheme}
        }
    }
    ```

    Generate the bcrypt hash once with `caddy hash-password` (interactive
    prompt — never pass `--plaintext` from your shell history). After
    editing the Caddyfile, reload with `sudo systemctl reload caddy`;
    Caddy's reload is graceful and never drops in-flight connections.

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

14. **AIO sandbox provider stale-handle reuse — PARTIALLY FIXED as of
    `f401e7ba` (2026-06-11 sync).** The gateway keeps an in-process map of
    `thread_id → sandbox_id`. If the underlying sandbox container dies
    (idle eviction, OOM, `--rm` after exit, a host-level `docker rm`, or
    any DooD-side cleanup), a stale ID could be reused. Symptom when it
    bites: every subsequent tool call in that thread hangs ~120 s and then
    fails with
    `Failed to execute command in sandbox: [Errno 110] Connection timed out`,
    and the agent's reasoning loop keeps retrying, burning tokens.

    **Two timing windows — only one is now auto-healed:**

    - **Window A — container dies, then a *new* run/turn starts for that
      thread: FIXED.** `f401e7ba` added an `is_alive` health check
      (`docker inspect`, works for us via the docker.sock DooD mount, 5 s
      timeout, offloaded via `asyncio.to_thread`) to the two acquire-time
      reuse paths (`_reuse_in_process_sandbox` active cache,
      `_reclaim_warm_pool_sandbox` warm pool). A definitively-dead
      container is now evicted from the maps, destroyed, and recreated, so
      the next run no longer inherits a dead `sandbox_id`. **No manual
      `docker restart deer-flow-gateway` needed for between-run staleness
      anymore.** Our `GATEWAY_WORKERS=1` (single in-process map) is exactly
      the targeted scenario; the LGI batch (sequential thread-reusing runs)
      self-heals between runs.
    - **Window B — container dies *mid-run* (tool calls within an
      already-acquired run): NOT FIXED.** The hot tool path
      (`sandbox/tools.py` `_get_sandbox` / `ensure_sandbox_initialized` →
      `provider.get()`) is deliberately a lock-protected in-memory lookup
      with **no** health check (kept event-loop-safe; enforced by
      `test_get_uses_in_memory_registry_only`). With `lazy_init=True` a
      sandbox is acquired once at first tool use and reused via `get()` for
      the rest of the run, so a mid-run death still hangs that run ~120 s.
      Only the *following* run self-heals. **Manual gateway restart is now
      only needed to unblock a long-lived run mid-flight.**

    Diagnose:

    ```bash
    sg docker -c 'docker ps --filter name=deer-flow-sandbox --format "{{.Names}}: {{.Status}}"'
    # if empty AND the gateway log shows "Reusing in-process sandbox <id>"
    # for a thread that's stuck, you're in this state.
    ```

    Fix — restart **just the gateway**, not the full stack. That clears
    the in-process map without bouncing the frontend/nginx:

    ```bash
    sg docker -c 'docker restart deer-flow-gateway'
    ```

    `docker compose restart gateway` does NOT work outside of `make up` —
    it silently fails to substitute the volume specs (the env vars
    `deploy.sh` exports aren't present), prints `invalid spec:
    :/app/backend/config.yaml:ro: empty section between colons`, and
    leaves the gateway unchanged. Use plain `docker restart`.

    After the gateway restart, the next tool call from any thread spins
    up a fresh sandbox container automatically — no manual cleanup
    needed.

15. **AIO sandbox `/v1/shell/exec` runs as `uid=1000 (gem)`, NOT as root.**
    The container itself starts as root (visible from
    `docker exec <sandbox> id` → `uid=0`), but the shell-exec HTTP
    endpoint the agent actually uses runs commands as a non-root user
    `gem`. This trips up any operator who tests sandbox file access with
    `docker exec` and concludes "works fine" — the agent will still hit
    `Permission denied`. **Use the HTTP endpoint for any sandbox file/
    permission verification:**

    ```bash
    curl -sS -X POST -H 'Content-Type: application/json' \
      -d '{"command":"id; head -c 80 /mnt/user-data/uploads/<file>"}' \
      "http://127.0.0.1:<sandbox-host-port>/v1/shell/exec"
    ```

    The gateway by default writes uploaded files as root with mode
    `0600` (its own umask), so the sandbox user immediately gets
    `Permission denied` on `/mnt/user-data/uploads/*`. The agent then
    detours through `sudo cp /mnt/user-data/uploads/X /mnt/user-data/workspace/X
    && sudo chown $(whoami):$(whoami) /mnt/user-data/workspace/X` —
    which works but burns turns and triggers the audit middleware's
    medium-risk warning on every upload.

    **Upstream fix landed 2026-05-25 in `f9b70713`** — adds a
    `_make_file_sandbox_readable()` helper (sets `S_IRGRP | S_IROTH`
    on every uploaded file) and a `SandboxProvider.needs_upload_permission_adjustment`
    attribute so the local-filesystem provider can opt out cleanly.
    Any deployment current with upstream `main` (or any merge after
    `f9b70713`) gets this automatically — no local patch required.
    The previous `fix(uploads): chmod uploaded files to 0644` patch
    on this fork is now redundant; commit `f83611f1` removed our
    inline duplicate after the merge.

    For older deployments still on a pre-`f9b70713` snapshot, the
    one-off remediation that fixes already-uploaded files is:

    ```bash
    sg docker -c 'docker exec deer-flow-gateway sh -c \
      "find /app/backend/.deer-flow/users -path \"*/user-data/uploads/*\" \
       -type f -exec chmod 644 {} +"'
    ```

16. **`readabilipy`'s bundled Node deps aren't installed by `uv sync`.**
    The Python `readabilipy` package (used by `deerflow.community.jina_ai`
    as the local fallback when Jina's reader returns sparse content)
    ships an `ExtractArticle.js` that does `require('@mozilla/readability')`,
    `require('jsdom')`, and `require('minimist')`. None of those are pip
    dependencies — they're Node modules declared in a `package.json`
    *inside the venv* at `site-packages/readabilipy/javascript/`, and the
    package's documented install flow is `pip install readabilipy && cd
    .../javascript && npm install`. `uv sync` only runs the pip half,
    leaving an empty `node_modules/@mozilla/` directory. The visible
    failure is repeated `Cannot find module '@mozilla/readability'`
    tracebacks at WARNING level whenever the Readability.js fallback
    fires (typically alongside Jina `ReadTimeout` warnings on slow sites).

    This fork's `backend/Dockerfile` (commit `910d6b6d`) adds an explicit
    `npm install --omit=dev` step in the builder stage so the populated
    `node_modules` ships inside the venv copied to the runtime image. If
    you're deploying on a different fork or branch, either cherry-pick
    that change or hot-patch the running container (ephemeral — wiped on
    next rebuild):

    ```bash
    sg docker -c 'docker exec deer-flow-gateway sh -c \
      "cd /app/backend/.venv/lib/python3.12/site-packages/readabilipy/javascript \
       && rm -rf node_modules package-lock.json \
       && npm install --omit=dev --no-audit --no-fund"'
    ```

    To verify resolution works after either path:

    ```bash
    sg docker -c 'docker exec deer-flow-gateway node -e \
      "console.log(require.resolve(\"@mozilla/readability\"))"' 2>/dev/null
    # expect: /app/backend/.venv/.../node_modules/@mozilla/readability/index.js
    ```

17. **~~Managed Postgres resize / failover invalidates the gateway's psycopg
    connection pool.~~ RETIRED 2026-06-01** — fixed upstream by `031d6fbc`
    (PR #3226). The gateway now uses `AsyncConnectionPool` with
    `check_connection` + TCP keepalive, so dead idle connections are
    detected and replaced on checkout. No more manual `docker restart
    deer-flow-gateway` after DO Postgres maintenance / resize / failover.

    Historical detail preserved below for reference if the symptom ever
    recurs from a different cause:

    > When the DB host was resized (e.g. DO managed Postgres tier
    > change), maintenance-restarted, or failed over, existing TCP
    > connections from the gateway were dropped server-side. The
    > gateway's psycopg async pool did **not** auto-recycle dropped
    > connections — it handed stale handles to the next request, which
    > then raised `psycopg.OperationalError: the connection is closed`.
    > Symptoms were: HTTP 500 on `/api/threads/{id}/history` and other
    > DB-backed endpoints right after a Postgres maintenance event;
    > repeated `connection is closed` tracebacks in gateway logs;
    > `/api/health` still 401'd fine (no DB call) so naive uptime checks
    > didn't catch it. Manual fix was `docker restart deer-flow-gateway`
    > (no rebuild — pool reinit only). The Langchain checkpointer
    > auto-recovered on the next write; the app-level pool didn't —
    > hence the asymmetric failure mode.

18. **`DEER_FLOW_INTERNAL_AUTH_TOKEN` must be shared across gateway
    workers.** As of upstream #3184 (merged 2026-05-27), the gateway
    requires a stable internal auth token so channel workers handled by
    one Uvicorn worker can call internal APIs served by another. The
    canonical workflow is `scripts/deploy.sh`, which auto-generates the
    token and persists it to `$DEER_FLOW_HOME/.internal-auth-token` (or
    reuses an existing one). Symptoms of a missing or non-shared token:
    `403`/`401` on internal `/api/...` calls when channel workers fan
    out across multiple Uvicorn workers.

    If running `docker compose` directly without `deploy.sh`, you must
    set `DEER_FLOW_INTERNAL_AUTH_TOKEN` yourself (e.g.,
    `export DEER_FLOW_INTERNAL_AUTH_TOKEN=$(openssl rand -hex 32)`) —
    otherwise each worker process generates its own and inter-worker
    requests fail authentication.

19. **MCP HTTP/SSE transports cannot be session-pooled.** As of upstream
    #3203 (merged 2026-05-27), only `transport: stdio` MCP servers are
    wrapped with persistent-session logic. HTTP/SSE servers are returned
    unwrapped because their internal anyio TaskGroups can't be closed
    from a different async task — pooling them previously caused
    `anyioRuntimeError` on cleanup. If you add an HTTP/SSE MCP entry to
    `extensions_config.json` and see those errors, the fix is now
    automatic; no config change needed.

20. **`GATEWAY_WORKERS` must stay at 1 — do NOT raise it.** The gateway holds
    run state **in-process and per-worker**: `RunManager._runs` (each run's
    `asyncio.Task` + abort event) and the `MemoryStreamBridge` (per-run SSE
    event log) live in one worker's memory, and there is **no shared
    cross-worker stream bridge** (our config uses the memory backend; the
    redis path is `NotImplementedError`). nginx round-robins with **no sticky
    sessions**, so with >1 worker a cancel/reconnect/SSE request has a high
    chance of landing on a worker that never saw the run → `cancel`/`join`
    return **HTTP 409 "not active on this worker"**, and an SSE reconnect
    subscribes to an empty stream and **emits only a 15s heartbeat, never an
    END (reconnect hangs)**. We ran the `:-4` default (4 workers) until
    **2026-06-10**, when upstream `05ae4467` (#3475) changed the compose
    default to `${GATEWAY_WORKERS:-1}`; merging it dropped us to 1 worker and
    eliminated the race (gateway memory fell ~662 MB → ~175 MB as a side
    effect). **`GATEWAY_WORKERS` is intentionally unset in `.env`** so we take
    that `:-1` default; a regression test (`test_compose_default_workers.py`)
    pins it. Scale a single worker with **more CPU/RAM**, not more workers,
    until upstream ships the shared stream bridge (tracked in #3191). The LGI
    Stage-1 batch is unaffected by worker count (it uses `docker exec` →
    embedded `DeerFlowClient`, bypassing the HTTP/RunManager/StreamBridge path).

21. **The IM-channels subsystem is always wired in, even with zero channels
    configured — and it auto-creates 4 Postgres tables on boot.** As of the
    **2026-06-12** sync (upstream `aa015462`, #3487, user-owned IM channel
    connections), the gateway lifespan unconditionally calls
    `start_channel_service()` every boot and a `channel_connections` router is
    mounted at `/api/channels`. With **no IM channels in `config.yaml`** (our
    case — Playwright MCP only) this is **inert**: the boot log shows
    `Channel service started: {... all 7 providers enabled:false, running:false}`,
    no channel workers run, and the mutating `runtime-config` endpoints are
    **admin-gated** (`/api/channels/providers` returns 401 unauthenticated).
    The one real side effect: because we have a live DB engine
    (`run_events.backend: db`), `Base.metadata.create_all` **auto-creates 4 new
    empty tables** — `channel_connections`, `channel_credentials`,
    `channel_oauth_states`, `channel_conversations` — idempotently, with no
    alembic migration and no data. This is expected and benign; do **not**
    "clean them up." The same sync also **rescoped internal-token authz**:
    internal-token callers (which we don't use — they're for IM channel
    workers) are no longer exempt from the stateless-run thread-ownership
    guard, scoped instead to an `X-DeerFlow-Owner-User-Id` owner header that is
    ignored for all `system_role` `user`/`admin` (i.e. all our) traffic. Net
    security hardening; zero behavior change for the operator or the LGI batch.
    Keep the `channels`/`channel_connections` config blocks **absent** to keep
    the feature disabled.

## Tuning recommendations

These aren't required for a working deployment but are improvements
discovered during production hardening. They're separate from the gotchas
above because the upstream defaults work — these are just better.

### Temperature for research/analysis workloads (0.7 → 0.5)

The upstream `config.example.yaml` sets `temperature: 0.7` on every model
— a generic "creative but coherent" default tuned for chatbot breadth,
not for factual synthesis. For workloads dominated by deep research,
analysis, or forecasting, lower temperatures (0.3-0.5) reduce
hallucination by keeping the sampler on high-confidence completions.

The effect is muted on reasoning-enabled models — most of the sampling
diversity happens inside the thinking chain, and the final answer is
closer to deterministic regardless. (Anthropic deprecated `temperature`
entirely on Opus 4.7 for exactly this reason — see the Claude tuning
section above.) But the marginal accuracy gain is still real, and the
cost is nil.

This deployment runs `temperature: 0.5` across all five active models
(`deepseek-v4-pro`, `kimi-k2.6`, `qwen3.6-plus`, `qwen3.7-max`,
`mimo-v2.5-pro`) since 2026-05-22 (MiMo added 2026-05-28). If outputs start feeling flat or repetitive, bump back to
0.6-0.7 — there's no harm in iterating. The commented example blocks
in `config.yaml` for disabled models (Gemini, Claude, vLLM templates)
remain at the upstream 0.7 default as reference.

### Summarization trigger (15564 → 32000)

Upstream raised the default `summarization.trigger.tokens` from 15564 to
32000 in commit `a64a39db` (before v2.0-m1). For research, analysis, and
forecasting workloads on models with ≥32K context windows, the higher
threshold is unambiguously better:

- Summarization is a **lossy** operation — it compresses verbatim content
  into a summary, which means downstream tool calls can no longer quote
  exact passages from the source material.
- 15564 tokens (~12k words) fires summarization aggressively early.
  Research threads that pull a few full articles via `web_fetch` cross
  that line quickly, then operate on compressed memory for the rest of
  the run.
- 32000 tokens (~24k words) roughly doubles the verbatim retention
  window before compression. Models with 32K+ contexts (all four active
  on this fork) handle that easily.

Bump both fields together so the summarization input budget stays sized
to the trigger threshold:

```yaml
summarization:
  trigger:
    - type: tokens
      value: 32000          # was 15564
  trim_tokens_to_summarize: 32000  # was 15564
```

`summarization.*` is hot-reloadable per `CLAUDE.md`, but the
single-file bind-mount inode pin (gotcha #14) means a config edit via
write-and-rename effectively requires `make down && make up` to land.
Trade-off: slightly higher per-run token spend; meaningfully better
citation fidelity in long research threads.

### Jina `web_fetch` timeout (10 → 30)

The upstream `config.example.yaml` sets `timeout: 10` for the Jina reader
tool. JavaScript-heavy pages routinely take longer than 10s to extract,
causing roughly a 20% `ReadTimeout` rate in observed workloads. Bumping
to 30 drops the timeout rate to near-zero without measurable downside:

```yaml
- name: web_fetch
  group: web
  use: deerflow.community.jina_ai.tools:web_fetch_tool
  timeout: 30
```

### DeepSeek via native API instead of OpenRouter

OpenRouter's OpenAI-compatible passthrough strips the `reasoning_content`
field from DeepSeek's responses, which breaks multi-turn thinking — on
the second turn the DeepSeek API rejects the request because earlier
assistant messages are missing their required `reasoning_content`. The
fork ships `PatchedChatDeepSeek` (in `deerflow.models.patched_deepseek`)
which talks to DeepSeek's native API and preserves the field across
turns. To use it (substitute the DeepSeek model slug you want from
`https://api.deepseek.com/v1/models`):

```yaml
- name: deepseek-v4-pro
  use: deerflow.models.patched_deepseek:PatchedChatDeepSeek
  model: deepseek-v4-pro
  api_key: $DEEPSEEK_API_KEY
  timeout: 600.0
  max_retries: 2
  max_tokens: 32768
  supports_thinking: true
  supports_reasoning_effort: true
  when_thinking_enabled:
    extra_body:
      thinking:
        type: enabled
  when_thinking_disabled:
    extra_body:
      thinking:
        type: disabled
```

Add `DEEPSEEK_API_KEY=<key>` to `.env`. Kimi K2.6 and Claude Opus 4.7
don't have this issue with OpenRouter — they can stay on the
`langchain_openai:ChatOpenAI + base_url=https://openrouter.ai/api/v1`
pattern.

### Gemini 3.x via native Google API instead of OpenRouter

Same family of problem: Gemini 3.x thinking returns a `thought_signature`
on tool-call objects that the native API requires back on every
multi-turn request. OpenRouter's OpenAI-compatible passthrough drops
that field, breaking tool-using runs in Reasoning/Ultra mode. (Upstream
ships `deerflow.models.patched_openai:PatchedChatOpenAI` as a workaround
for OpenRouter-proxied Gemini — but using the native Google API is
simpler, since `langchain_google_genai:ChatGoogleGenerativeAI` handles
`thought_signature` correctly out of the box.)

```yaml
- name: gemini-3.1-pro
  display_name: Gemini 3.1 Pro Preview
  use: langchain_google_genai:ChatGoogleGenerativeAI
  model: gemini-3.1-pro-preview         # native slug, no provider prefix
  timeout: 600.0
  max_retries: 2
  max_output_tokens: 32768              # not max_tokens — Google SDK uses this name
  temperature: 0.7
  supports_thinking: true
  supports_reasoning_effort: true
```

Add `GOOGLE_API_KEY=<key>` to `.env`. `langchain_google_genai` (a hard
dep in `pyproject.toml`, already in the gateway image; no UV_EXTRAS
needed) auto-reads the key from the env var, so no `api_key:` field in
the config. Verify the key has access to the model with:

```bash
curl -sS "https://generativelanguage.googleapis.com/v1beta/models?key=$GOOGLE_API_KEY" \
  | python3 -c "import json,sys; d=json.load(sys.stdin); print('\n'.join(m['name'] for m in d['models'] if 'gemini-3' in m['name']))"
```

### Claude via native Anthropic API + DeerFlow's ClaudeChatModel

DeerFlow ships a custom Claude provider
(`deerflow.models.claude_provider:ClaudeChatModel`) which subclasses
`langchain_anthropic.ChatAnthropic` and adds two material wins over the
OpenRouter passthrough:

1. **Prompt caching on by default** — the provider stamps
   `cache_control: { type: ephemeral }` on the system prompt, the 3 most
   recent messages, and the last tool definition. Long-running threads
   with stable system prompts then hit Anthropic's cached-input pricing
   (~90% discount on the cached prefix). Visible in `usage_metadata` as
   rising `cache_read` / `ephemeral_5m_input_tokens` over a conversation.
2. **Auto thinking budget** — when `thinking_enabled=true` (Reasoning,
   Pro, Ultra modes), 80% of `max_tokens` is automatically reserved for
   the reasoning chain. Avoids the manual `thinking.budget_tokens`
   tuning needed with raw `ChatAnthropic`.

It also transparently handles Claude Code OAuth tokens (`sk-ant-oat-…`)
if you ever paste one — auto-swaps `x-api-key` for `Authorization:
Bearer` and disables prompt caching (OAuth token's 4-cache-control limit).
For standard `sk-ant-api03-…` keys, normal `x-api-key` auth is used and
caching stays on.

```yaml
- name: claude-opus-4.7
  display_name: Claude Opus 4.7
  use: deerflow.models.claude_provider:ClaudeChatModel
  model: claude-opus-4-7                # native slug — dashes, not dots
  api_key: $ANTHROPIC_API_KEY
  default_request_timeout: 600.0        # NB: 'default_request_timeout', not 'timeout'
  max_retries: 2
  max_tokens: 32768
  supports_thinking: true               # toggles thinking_enabled, not budget
  supports_reasoning_effort: false      # see note below — Anthropic rejects it
  supports_vision: true
  enable_prompt_caching: true           # explicit (default true)
  auto_thinking_budget: false           # see note below — Opus 4.7 schema mismatch
```

Add `ANTHROPIC_API_KEY=<key>` to `.env`.

**Three Opus 4.7 quirks that older Anthropic configs got wrong** (each
returns HTTP 400 on the first call if you don't preempt it):

1. **No `temperature`** — Anthropic deprecated this on newer Opus/Sonnet/Haiku
   4.x. Requests with `temperature` return:
   `"temperature" is deprecated for this model.` Sampling diversity is
   driven by the thinking process now. Omit the field; the factory will
   not add a default.

2. **No `reasoning_effort` kwarg** — the DeerFlow UI sends an OpenAI-style
   `reasoning_effort: low|medium|high` based on the picked mode. The
   Anthropic SDK doesn't accept that param and raises
   `AsyncMessages.create() got an unexpected keyword argument 'reasoning_effort'`.
   Setting `supports_reasoning_effort: false` makes the factory strip the
   kwarg before invocation (see `backend/.../models/factory.py:113`).

3. **No legacy `thinking.type: enabled`** — Opus 4.7 changed the thinking
   API from `thinking: { type: enabled, budget_tokens: N }` to
   `thinking: { type: adaptive }` + `output_config: { effort: ... }`.
   Sending the old shape returns:
   `"thinking.type.enabled" is not supported for this model. Use "thinking.type.adaptive" and "output_config.effort" to control thinking behavior.`
   The DeerFlow `ClaudeChatModel.auto_thinking_budget` was written for
   the old schema; **set `auto_thinking_budget: false` and omit the
   `when_thinking_enabled`/`when_thinking_disabled` blocks entirely**.
   Opus 4.7 will pick its own thinking budget adaptively. The trade-off:
   you lose UI-driven reasoning intensity control on Claude, but the
   model decides its own depth per prompt (which it's typically good at).

If a future DeerFlow update teaches `ClaudeChatModel` to emit the new
`thinking.type: adaptive` + `output_config.effort` shape, you can flip
`auto_thinking_budget` and `supports_reasoning_effort` back to `true`
and Claude will steer reasoning depth from the UI again.

### Qwen via Alibaba DashScope (OpenAI-compatible)

DashScope's `compatible-mode/v1` endpoint accepts standard OpenAI-style
requests, so plain `langchain_openai:ChatOpenAI` with a custom `base_url`
works without any patched provider. The only quirk is the thinking
toggle: Qwen uses a top-level `enable_thinking: true|false` field
(passed via `extra_body`), **not** the OpenAI-style `reasoning_effort`
kwarg.

```yaml
- name: qwen3.6-plus
  display_name: Qwen3.6 Plus (DashScope)
  use: langchain_openai:ChatOpenAI
  model: qwen3.6-plus                        # stable production tag
  api_key: $DASHSCOPE_API_KEY
  base_url: https://dashscope-intl.aliyuncs.com/compatible-mode/v1
  request_timeout: 600.0
  max_retries: 2
  max_tokens: 32768
  temperature: 0.7
  supports_thinking: true
  supports_reasoning_effort: false           # Qwen uses enable_thinking, not reasoning_effort
  when_thinking_enabled:
    extra_body:
      enable_thinking: true
  when_thinking_disabled:
    extra_body:
      enable_thinking: false
```

Add `DASHSCOPE_API_KEY=<key>` to `.env`.

Quick verification recipe — list the slugs your key has access to:

```bash
curl -sS -H "Authorization: Bearer $DASHSCOPE_API_KEY" \
  https://dashscope-intl.aliyuncs.com/compatible-mode/v1/models \
  | python3 -c "import json,sys; d=json.load(sys.stdin); print('\n'.join(m['id'] for m in d['data'] if 'qwen3' in m['id'].lower()))" \
  | head
```

**Multi-turn thinking works** through plain `ChatOpenAI` — verified in
production with 24 consecutive successful calls and `reasoning_tokens`
counts (102/145/171/270/125) across the conversation. Unlike DeepSeek,
DashScope does not require `reasoning_content` echoed back on
subsequent turns, so no `PatchedChatQwen` wrapper is needed.

**Tier / generation choice:**

- `qwen3.6-plus` (used above) — 3.6-generation mid-tier, stable
  production slug, faster and cheaper. Recommended default.
- `qwen3.6-max-preview` — 3.6 top-tier model but a `-preview` slug:
  Alibaba may rename or remove it without warning. Pin a dated suffix
  (e.g. `qwen3-max-2026-01-23`) if you want the heaviest 3.6 model with
  long-term slug stability.
- `qwen3.7-max` — newest top-tier (announced 2026-05-20). 1M-token
  context, AAII intel index 56.6 (+4.8 vs 3.6-max-preview), tuned for
  long-horizon agent tasks. The DashScope intl slug is `qwen3.7-max`
  **without** any `-preview` suffix (the `-preview` suffix is rejected
  by the API as of 2026-05-22) and no dated snapshot is published yet.
  Treat it as preview anyway — the slug can be renamed/removed
  underneath you. Once a dated variant ships (e.g.
  `qwen3.7-max-YYYY-MM-DD`), pin that instead. The block is identical
  in shape to `qwen3.6-plus` above — same `use:`, same `base_url`,
  same `enable_thinking` toggle. Verified end-to-end on this fork
  2026-05-22: model responds, returns `reasoning_content` and
  `completion_tokens_details.reasoning_tokens`.

Either way, the international DashScope endpoint
(`dashscope-intl.aliyuncs.com`) is what's used here; the mainland-China
endpoint (`dashscope.aliyuncs.com`) needs a different key. The two are
not interchangeable.

### MiMo (Xiaomi) via `PatchedChatMiMo` adapter

Xiaomi's MiMo reasoning model family (mimo-v2.5-pro, mimo-v2.5, mimo-v2-pro,
mimo-v2-omni, mimo-v2-flash) returns `reasoning_content` in thinking mode and
**requires that field to be replayed on historical assistant messages** in
multi-turn agent/tool-call conversations. Standard `langchain_openai.ChatOpenAI`
drops the provider-specific field, causing HTTP 400 errors once tool calls
enter the conversation history. Upstream provides a dedicated adapter:

```yaml
- name: mimo-v2.5-pro
  display_name: MiMo V2.5 Pro
  use: deerflow.models.patched_mimo:PatchedChatMiMo
  model: mimo-v2.5-pro
  api_key: $MIMO_API_KEY
  base_url: https://api.xiaomimimo.com/v1
  request_timeout: 600.0
  max_retries: 2
  max_tokens: 8192
  temperature: 0.5
  supports_thinking: true
  supports_vision: false
  when_thinking_enabled:
    extra_body:
      thinking:
        type: enabled
  when_thinking_disabled:
    extra_body:
      thinking:
        type: disabled
```

Endpoint selection by key prefix:
- `sk-...` keys (pay-as-you-go) → `https://api.xiaomimimo.com/v1`
- `tp-...` keys (Token Plan, regional) → `https://token-plan-cn.xiaomimimo.com/v1` (or other regional Token Plan URL)

Verified end-to-end 2026-05-28: API call returned both `content` and
`reasoning_content` fields with `completion_tokens_details.reasoning_tokens`
set, confirming thinking mode and the adapter wire-up.

`PatchedChatMiMo` is model-id agnostic — use the same `use:` line for every
MiMo thinking model entry, including subagent model overrides. The adapter
upstream PR was #3298 (merged 2026-05-28); requires this fork to be
current with that commit or later.

### Persistence config block (full reference)

Three sections must be present together for a fully-persistent deployment.
Cross-reference gotchas #3, #12, #13. Both `checkpointer:` and `database:`
point at the same Postgres URL — psycopg (used by checkpointer + store)
and asyncpg (used by SQLAlchemy engine) both read `PGSSLMODE=require`
from the env, so the URL stays SSL-param-free:

```yaml
checkpointer:                    # LangGraph state + Store (via psycopg)
  type: postgres
  connection_string: $DATABASE_URL

database:                        # DeerFlow app data (via SQLAlchemy + asyncpg)
  backend: postgres
  postgres_url: $DATABASE_URL

run_events:                      # chat-history surface — MUST be 'db'
  backend: db
  max_trace_content: 10240
  track_token_usage: true
```

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

## Safety termination handling

Upstream commit `be0eae98` (merged 2026-05-22) added
`SafetyFinishReasonMiddleware` to intercept AIMessages where the provider
stopped generation for safety reasons but still returned `tool_calls`.
Without it, LangChain's tool router executes those half-truncated tool
calls anyway (issue #3028) — the model says "I won't do this" while the
agent silently runs the partial `write_file` it emitted before refusing.

Built-in detectors cover:

- **OpenAI-compatible** `finish_reason='content_filter'` — DeepSeek, Kimi,
  Qwen (DashScope), OpenRouter, etc.
- **Anthropic** `stop_reason='refusal'`
- **Gemini** `finish_reason` in
  `SAFETY` / `BLOCKLIST` / `PROHIBITED_CONTENT` / `SPII` / `RECITATION` /
  `IMAGE_SAFETY`

**Default config — middleware is ON, no action needed:**

```yaml
safety_finish_reason:
  enabled: true        # default true
  # detectors: null    # default null = use the built-in set
```

When safety termination fires, the middleware:

- Clears structured `tool_calls` AND raw `additional_kwargs.tool_calls`/`function_call`
- Preserves `response_metadata.finish_reason` for downstream observers
- Stamps `additional_kwargs.safety_termination` for trace observability
- Appends a user-facing explanation to the message content
- Emits a `safety_termination` custom SSE event (live UIs reconcile any "tool starting…" indicator)
- Records a `middleware:safety_termination` row in `run_events` (offline audit)

**Audit query — which runs got safety-suppressed:**

```sql
SELECT run_id, created_at, content::json->>'changes' AS details
FROM run_events
WHERE event_type = 'middleware:safety_termination'
ORDER BY created_at DESC
LIMIT 20;
```

Tool *arguments* are deliberately excluded from the journal (the very text
the provider filtered would defeat the audit's purpose); the row records
the detector, the reason value, suppressed tool names/ids, and the
message_id.

**Customisation — extending the OpenAI detector for non-standard tokens:**

```yaml
safety_finish_reason:
  enabled: true
  detectors:
    - use: deerflow.agents.middlewares.safety_termination_detectors:OpenAICompatibleContentFilterDetector
      config:
        finish_reasons: ["content_filter", "sensitive", "risk_control"]
    - use: deerflow.agents.middlewares.safety_termination_detectors:AnthropicRefusalDetector
    - use: deerflow.agents.middlewares.safety_termination_detectors:GeminiSafetyDetector
```

Providing a `detectors:` list fully **overrides** the built-in set — to
disable the middleware entirely, use `enabled: false` instead of an empty
list.

## MCP servers

The runtime gateway image bakes Microsoft's [`@playwright/mcp`](https://github.com/microsoft/playwright-mcp)
plus Chromium so deployments can enable browser automation without
running a separate container. The package version and the browser are
pinned at image-build time in `backend/Dockerfile`:

- npm package: `@playwright/mcp@0.0.75` (override via
  `--build-arg PLAYWRIGHT_MCP_VERSION=<ver>`)
- Browser cache: `/ms-playwright` (`PLAYWRIGHT_BROWSERS_PATH`)

To enable, add the entry to `extensions_config.json` (gitignored —
per-deployment state):

```json
{
  "mcpServers": {
    "playwright": {
      "enabled": true,
      "type": "stdio",
      "command": "npx",
      "args": ["@playwright/mcp@0.0.75", "--headless", "--isolated",
               "--no-sandbox", "--browser", "chromium"]
    }
  }
}
```

Flag rationale for the Docker runtime:

- `--headless` — no display server in the container
- `--isolated` — clean profile per session; sidesteps the persistent-profile
  lockfile conflict the upstream README warns about
- `--no-sandbox` — chromium's internal sandbox needs `CAP_SYS_ADMIN`
  which isn't granted by default; the gateway container is the security
  boundary, not the browser

DeerFlow loads MCP tools lazily on first agent use and watches
`extensions_config.json` via mtime polling, so the config entry itself
needs no restart. The chromium binary is baked at build time, though, so
**adding the dependency** (or bumping `PLAYWRIGHT_MCP_VERSION`) requires
`make down && make up`.

**Deferred-MCP-tool loading / `tool_search` is DISABLED here.** `config.yaml`
sets `tool_search.enabled: false`, so the whole deferred-tool subsystem
(the `tool_search` builtin, `DeferredToolFilterMiddleware`, `mcp_metadata`
tagging, `ThreadState.promoted`) is a runtime no-op for this deployment —
with a single Playwright MCP server, its tools load directly rather than
being deferred behind a search tool. Practical consequence for syncs:
upstream commits scoped to that subsystem (e.g. `d9f47249` #3342,
`2bbc7879` #3370, and the earlier deferred-tool work) are **inert for us**
and can be fast-tracked as low-priority once confirmed they don't touch
the eager tool-load or MCP-tag-on-load path. Re-evaluate only if we ever
flip `tool_search.enabled: true` (would be relevant only with many MCP
servers/tools where context-window pressure justifies deferral).

Quick sanity check that chromium launches and the MCP responds
(independent of the gateway agent path):

```bash
# 1. Write the JSON-RPC handshake payload (initialize + initialized + tools/list)
cat > /tmp/mcp-probe.jsonl <<'EOF'
{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2024-11-05","capabilities":{},"clientInfo":{"name":"t","version":"1"}}}
{"jsonrpc":"2.0","method":"notifications/initialized"}
{"jsonrpc":"2.0","id":2,"method":"tools/list"}
EOF

# 2. Pipe it into the MCP inside the gateway container
sg docker -c 'docker exec -i deer-flow-gateway \
  npx -y @playwright/mcp@0.0.75 --headless --isolated --no-sandbox \
      --browser chromium' < /tmp/mcp-probe.jsonl | head -5
```

Expect two JSON-RPC response lines — an `initialize` ack with
`serverInfo.name == "Playwright"`, then a `tools/list` reply carrying
23 tools (`browser_navigate`, `browser_click`, `browser_snapshot`,
`browser_take_screenshot`, `browser_evaluate`, etc.).

## Local patches currently carried on `local-fixes`

Run `git log --oneline upstream/main..local-fixes` for the current list.
**Zero runtime hotfixes are currently carrying** — all previous bug
patches have been absorbed upstream:

- `fix(task_tool): handle AsyncCallbackManager in _find_usage_recorder`
  was absorbed on 2026-05-21 (merge into `e93f6584` introduced an
  equivalent `isinstance(BaseCallbackManager)` check).
- `fix(uploads): chmod uploaded files to 0644 so sandbox user can read`
  was absorbed on 2026-05-25 (upstream `f9b70713` added
  `_make_file_sandbox_readable()` plus a clean `SandboxProvider`
  opt-out attribute; our follow-up `f83611f1` removed the now-redundant
  inline chmod).

Most recent upstream sync: **2026-07-17** — **13 commits (`b3a0dac8`→`9a4c72db`), CLEAN merge (0 conflicts), MIGRATION-FREE (head stays `0005`), NO CVE.** Merge `dc29032a`; `main` ff'd → `9a4c72db`. Scope: 76 files, +6075/−284; NO landing/login/setup/i18n/Dockerfile/redis/consolidation touched → no re-skin conflict (merge-tree 0). ⚠ **Dating note:** the system clock read **2026-07-17** for this sync although the entry below is labeled 2026-07-19; the sequence is authoritative **by code** — this continues exactly from the prior tip `b3a0dac8` — not by the calendar labels. **⚠ HEADLINE — authz Phase-1A scaffolding (`#4203` `10890e10`):** propagates a trusted authorization Principal context (new `deerflow/authz/principal.py` + `test_authorization_principal.py` + 3 planning docs under `docs/plans/`); **ADDITIVE/INERT** — Phase 1A provides only the trusted identity chain; automatic Layer-1 filtering + Layer-2 enforcement middleware remain separate future work, and no guardrail provider is configured → zero runtime gating. **Memory subsystem: consolidation STILL OFF** — NO memory-consolidation commit in range; the one `<memory>`-tagged commit (`#4209` `e7d7da9e`) is a FRONTEND streamdown fix (strips `<memory>` tags to avoid a React console error), unrelated to the consolidation subsystem. `DeerMemConfig.consolidation_enabled` re-confirmed default `False` in the merged code + our `backend_config: {}` + runtime-resolved `False`. **Security:** `#4253` `ae223199` escape MindIE tool-response content against a `</tool_response>` breakout (prompt-injection hardening — matters for the LGI-outreach untrusted-content path). **New middlewares (additive):** `agents/middlewares/skill_tool_policy_middleware.py` (`#4098` apply allowed-tools only to ACTIVE skills — passive/enabled skills no longer clamp the toolset) + `agents/middlewares/tool_output_synopsis.py` (`#3377` structured typed synopsis for oversized tool-output previews). **Other:** `#4245` preserve single tildes in markdown, `#4219` refuse empty SOUL.md updates in update_agent, `#4235` TUI interrupt an active run before /quit, `#4247` mcp passive-skill-tool-visibility test. **Inert (DeerFlowClient research path uses no IM channels / no provisioner):** `#4229`/`#4251`/`#4231` Feishu/connect-binding/WeChat channel fixes, `#3929`/`#4190` helm sandbox Services default ClusterIP. **`config.example.yaml`** = COMMENT-ONLY (tool_output synopsis doc + an optional `preview_head/tail` sampling note) → NO consolidation/memory-default change; our gitignored `config.yaml` governs. **Carried patches intact:** backend/Dockerfile readabilipy+Playwright ✓, `.dockerignore` `.deer-flow` ✓, `UV_EXTRAS=postgres` ✓, WRI re-skin ✓ (all UNTOUCHED — merge-tree 0). **Verified (hardened, all green):** `make down && make up` (gateway+frontend rebuilt) → bootstrap (NO new migration, head stays `0005`) → "Application startup complete" <3s, 0 boot errors, NO redis/provisioner, `:2026` 200 → **3b sentinel "schema in sync — no missing columns"** (head `0005`) + **6 models** (deepseek-v4-pro/kimi-k2.6/qwen3.6-plus/qwen3.7-plus/qwen3.7-max/mimo-v2.5-pro) → **consolidation VERIFIED OFF** (`DeerMemConfig.consolidation_enabled=False` — default in merged code + `backend_config: {}` + runtime-resolved; mode middleware; injection false) → **3c Aio bash smoke** `get_sandbox_provider().acquire_async(user_id=sync-smoke-20260717).get(sid).execute_command(...)` → sandbox `2ba39c90` / `SYNC20260717_SANDBOX_OK / Linux / python3.10 / uv0.8.9 / gem` (gem canary back). Rollback tag `pre-sync-20260717` → `14157fb4` (drop post-push).

Prior 2026-07-19 — **7 commits (`45865e9f`→`b3a0dac8`), CLEAN merge (0 conflicts), + NEW MIGRATION `0005_run_stop_reason` (head 0004→0005), NO CVE.** Merge `b1c933a0`; `main` ff'd → `b3a0dac8`. Scope: 43 files, +2091/−375; NO landing/login/setup/i18n/Dockerfile/redis touched → no re-skin conflict (merge-tree 0). **⚠ HEADLINE — first new migration since 0004: `0005_run_stop_reason` (#4188)** = additive/safe (a single NULLABLE `runs.stop_reason` VARCHAR(50) via `safe_add_column`, no backfill/default); applied cleanly on `make up` bootstrap (`branch=versioned → upgrade head 0005`, log "Running upgrade 0004_run_ownership -> 0005_run_stop_reason"), sentinel confirms it. **Memory subsystem touched again (3 commits, consolidation STILL off):** `#4143` LLM-assigned per-fact expiry (new `backend_config` fields `staleness_max_lifetime_multiplier: 20.0` / `staleness_max_extension_days: 3650`; staleness review gains an EXTEND branch — our `backend_config: {}` → all DEFAULT, moot with injection off); `#4217` treat explicit null `backend_config` values as omitted in DeerMemConfig (robustness for a null `model:` key — WE DON'T HIT IT, ours is `{}` with no null model); `#3556` record effective memory identity per run. **Config re-verify (rigorous):** our RESTRUCTURED `config.yaml` (from 2026-07-15: `backend_config: {}` + explicit mode/manager_class/injection_enabled) went LIVE this recreate and resolves clean — `mode=middleware`, `injection_enabled=False`, `manager_class=deermem`, **`DeerMemConfig.consolidation_enabled=False` RESOLVED** (rigorously instantiated under the new #4217/#4143 code; new staleness fields default 20.0/3650). Other: `#4215` subagent checkpoint namespace, `1769b2de` run stop_reason from context. **Inert:** `#4222`/`#4218` channel/GitHub @mention+case fixes (no IM/GitHub channels). **Carried patches intact** (backend/Dockerfile readabilipy+Playwright, `.dockerignore` `.deer-flow`, `UV_EXTRAS=postgres`, WRI re-skin untouched; redis defn present but NO redis container post-`up`). **Verified (hardened, all green):** `make up` (gateway+frontend rebuilt) → bootstrap `upgrade head 0005` (NEW migration applied) → "Application startup complete", 0 boot errors, NO redis/provisioner, `:2026` 200 → **3b sentinel "schema in sync — no missing columns"** (head `0005`, incl. new `stop_reason` col) + **6 models** (deepseek-v4-pro/kimi-k2.6/qwen3.6-plus/qwen3.7-plus/qwen3.7-max/mimo-v2.5-pro) → **consolidation VERIFIED OFF** (`DeerMemConfig.consolidation_enabled=False`; mode middleware; injection false; backend_config {}) → **3c Aio bash smoke** `get_sandbox_provider().acquire_async(user_id=e778f372…).get(sid).execute_command(...)` → sandbox `3c4103ce` / `SYNC0716_SANDBOX_OK / Linux / python3.10 / uv0.8.9`. Rollback tag `pre-sync-20260719` → `cb7325ee` (drop post-push).

Earlier 2026-07-15: **17 commits (`656f6b36`→`45865e9f`), CLEAN merge (0 conflicts), migration-free (head stays `0004`), NO CVE.** Merge `ea1ff7d6`; `main` ff'd → `45865e9f`. Scope: 97 files, +8088/−3109; **NO landing/login/setup/i18n/Dockerfile/redis touched → no re-skin conflict** (merge-tree 0). **⚠ HEADLINE — memory-subsystem refactor `#4122` (pluggable memory abstraction + self-contained DeerMem backend):** reorganizes the `memory:` config — `consolidation_*`/`storage_path`/`model_name`/`staleness_*` MOVED under `backend_config:` (DeerMem-private); a new top-level `manager_class` backend selector (default `deermem`); `enabled`/`mode`/`injection_enabled` stay host-shared. **`MemoryConfig` has a back-compat auto-migration shim** — legacy top-level keys auto-migrate into `backend_config` with a warning, so our old-structure `config.yaml` `memory:` block (`storage_path: memory.json`, `model_name: null`, `injection_enabled: false`) loads unchanged. **Load-bearing re-verify — consolidation still OFF:** `DeerMemConfig.consolidation_enabled` RESOLVES to `False` (rigorously instantiated from the effective `backend_config`, not just unset), `mode=middleware`, our carried **`injection_enabled=False`** (LGI-batch anti-pollution, 2026-05-26) preserved, `manager_class=deermem`. **Notable (relevant):** `#4162` html-escape the conversation block in MEMORY_UPDATE_PROMPT (prompt-injection hardening — LGI-outreach untrusted-content path); `#3800` keep `create_thread` idempotent under an insert race (gateway robustness — LGI pipeline + outreach discovery both hit the gateway); `#4181` flush memory queue on graceful shutdown; `#4193` sanitize invalid tool-call arguments; `#4199`/`#4201` goal-continuation + tui-delta fixes; `#4160`/`#4166` skills/agent config guards. **Opt-in/inert:** `#4127`/`#4063` pluggable authz scaffolding (Phase 0 — no provider configured → inert), `#4104` GitHub-webhook dedupe (no GitHub channel), `#4175` helm CI, `#4211` h3 frontend dep bump, docs. **`config.example.yaml`** memory section restructured (consolidation under `backend_config`, still defaults `false`) — we carry no local delta on it. **Carried patches intact:** backend/Dockerfile readabilipy+Playwright, `.dockerignore` `.deer-flow`, `UV_EXTRAS=postgres`, WRI re-skin (hero+login+i18n UNTOUCHED). **Verified (hardened, all green):** `make up` (gateway+frontend rebuilt) → bootstrap `branch=versioned → upgrade head (0004_run_ownership)` (no new migration) → gateway clean start, 0 boot errors, **NO redis / NO provisioner**, `:2026` 200 → **3b sentinel "schema in sync — no missing columns"** (head `0004`) + **6 models** (deepseek-v4-pro/kimi-k2.6/qwen3.6-plus/qwen3.7-plus/qwen3.7-max/mimo-v2.5-pro) → **`memory.mode=middleware`, `injection_enabled=False`, `manager_class=deermem`, `DeerMemConfig.consolidation_enabled=False` VERIFIED OFF (under the new schema)** → **3c Aio bash smoke** `get_sandbox_provider().acquire_async(user_id=e778f372…).get(sid).execute_command(...)` → sandbox `b743e4c3` / `SYNC0715_SANDBOX_OK / Linux / Ubuntu 22.04 / python3.10 / uv0.8.9`. ⚠ the old `gem` canary is now absent (`no-gem`) — a benign `all-in-one-sandbox:latest` image drift (config.yaml line 804), NOT the code sync; bash+python+uv all functional. Rollback tag `pre-sync-20260715` → `1302250f` (drop post-push).

Earlier 2026-07-14 (later): **15 commits (`41b137c4`→`656f6b36`), CLEAN merge (0 conflicts), migration-free (head stays `0004`), NO CVE.** Merge `3e08c3de`; `main` ff'd → `656f6b36`. Scope: 40 files, +3303/−289; **NO landing/login/setup/i18n touched → no re-skin conflict** (merge-tree exit 0). **Notable (relevant):** `#4182` html-escape the summary input blocks in the summarization prompt (prompt-injection hardening — matters for the LGI-outreach untrusted-content path); `#4072` **window the LoopDetection tool-frequency counter so long runs don't false-trip** (helps our long qwen research runs — outreach discovery + LGI pipeline have hit "tool frequency hard limit"); `#4080` drop orphan ToolMessages so strict OpenAI-compat providers (**qwen**) don't 400; `#4140` stop persisting base64 image data in checkpoint state (checkpoint-bloat); `#4118` persist run duration in checkpoints; `#4124` invalidate MCP tools cache on config content+path (not just mtime); `#4090` legacy-backfill creates missing Index objects (INERT — already versioned at `0004`); `#4161` prohibit task tool in general-purpose subagent prompt. **Inert:** firecrawl align `#4151` + its revert `#4165` (no firecrawl), `#4141` Volcengine Coding Plan (COMMENT-ONLY config.example example), `#4131` GitHub review-comment fan-out, `#4169` skills-review CI, `#4147` frontend branch-action. **Carried patches intact:** backend/Dockerfile readabilipy+Playwright, `.dockerignore` `.deer-flow`, WRI re-skin (hero + i18n UNTOUCHED — no conflict). **`config.example.yaml` change = COMMENT-ONLY Volcengine example → NO consolidation/memory-default change** (memory_config.py defaults reconfirmed on the new image: `mode` default `middleware`, `consolidation_enabled` default `False`; our `config.yaml` sets neither). **Verified (hardened, all green):** `make down && make up` (gateway+frontend rebuilt) → bootstrap `branch=versioned → upgrade head (0004_run_ownership)` (no new migration) → "Application startup complete", 0 boot errors, **NO redis / NO provisioner**, `:2026` 200 → **3b sentinel "schema in sync — no missing columns"** (head `0004`) + **6 models** (deepseek-v4-pro/kimi-k2.6/qwen3.6-plus/qwen3.7-plus/qwen3.7-max/mimo-v2.5-pro) → **`memory.mode=middleware` + `consolidation_enabled=False` (image defaults, unset in config.yaml) VERIFIED OFF** → **3c Aio bash smoke** `get_sandbox_provider().acquire_async(user_id=e778f372…).get(sid).execute_command("echo SYNC0714B_SANDBOX_OK && uname -s && whoami")` → `AioSandboxProvider`/sandbox `7f30d1c2`/`SYNC0714B_SANDBOX_OK / Linux / gem`, released ok; log window clean (zero 5xx/traceback/consolidation). Rollback tag `pre-sync-20260714b` → `c119731a` (drop post-push).

Earlier 2026-07-14: **23 commits (`2bd0f56a`→`41b137c4`), 1 conflict (re-skin hero, kept OURS), migration-free (head stays `0004`), NO CVE — SECURITY-significant (4 prompt-injection/escape hardening fixes).** Merge `bc76175a`; `main` ff'd → `41b137c4`. Scope: 120 files, +7295/−1078. **Conflict:** `frontend/src/components/landing/hero.tsx` — upstream `#4092` tweaks the Galaxy/FlickeringGrid landing-hero background *positioning*; our WRI re-skin hero is a full custom maroon-gradient `<section>` using neither component, so **kept OURS** — nothing to graft. **The 23 — security (matter for the LGI-outreach untrusted-content path):** `#4155` block forged framework tags in the input guardrail, `#4157` html-escape subagent descriptions before `<subagent_system>`, `#4137` html-escape SOUL.md before `<soul>`, `#4128` escape untrusted skill metadata before the model prompt. **Sandbox/skill hardening:** `#4116` provisioner `/api/*` now requires `PROVISIONER_API_KEY` (opt-in; empty default → provisioner stays off), `#4153/#4130` skillscan recognizes more outbound network sinks, `#4154` validate MCP tool names at load, `#4108` output-mask regex single owner, `#4129` scope slash-skill whitelist to the run owner, `#4103` activate slash skill once/run, `#4146` scope OpenAI-compat rules to BaseChatOpenAI. **Robustness:** `#4065` context-compress bug, `#4064` cancel→lease takeover multi-worker (inert single-worker), `#4102` stream_chunk_timeout default on all BaseChatOpenAI subclasses, `#4136` load SOUL.md w/o config.yaml, `#4086` tool_search MAX_RESULTS cap off for `select:`. **Features (opt-in, OFF):** `#4024` Monocle observability (env-gated, process-global OTel — inert unless `MONOCLE_TRACING`), `#4115` subagent total-delegation cap (`subagents.max_total_per_run` default 6). **`#4034` memory = TEST-ONLY** (staleness/updater test additions; no consolidation/mode change). **`config.example.yaml` 22→24** (additive docs: Monocle/provisioner_api_key/max_total_per_run — our gitignored `config.yaml` governs → consolidation stays OFF, mode default middleware); **`docker-compose-dev.yaml`** +`PROVISIONER_API_KEY=${…:-}` passthrough (additive, empty default; redis service defn byte-unchanged pre=post, still not started — verified NO redis container post-`up`). **Carried patches intact:** backend/Dockerfile readabilipy+Playwright, `.dockerignore` `.deer-flow`, WRI re-skin (hero kept OURS); no i18n/auth re-skin files touched → no new re-skin strings. **Verified (hardened, all green):** `make down && make up` (gateway+frontend rebuilt) → bootstrap `branch=versioned → upgrade head (0004_run_ownership)` (no new migration) → "Application startup complete", 0 boot errors, **NO redis / NO provisioner** container, `:2026` 200 → **3b sentinel "schema in sync — no missing columns"** (head `0004`) + **6 models** (deepseek-v4-pro/kimi-k2.6/qwen3.6-plus/qwen3.7-plus/qwen3.7-max/mimo-v2.5-pro) → **effective `memory.mode=middleware` (image default, unset in config.yaml), `consolidation_enabled=False` (image default), `memory.enabled=True`** → **3c Aio bash smoke** `get_sandbox_provider().acquire_async(user_id=e778f372…).get(sid).execute_command("echo SYNC0714_SANDBOX_OK && uname -s && whoami")` → `AioSandboxProvider`/sandbox `bcff13e3`/`SYNC0714_SANDBOX_OK / Linux / gem`, released ok; log window clean (zero 5xx/traceback/consolidation). Rollback tag `pre-sync-20260714` → `10d1ba8f` (drop post-push).

Earlier 2026-07-13: **4 commits (`897be7e0`→`2bd0f56a`), CLEAN merge (0 conflicts), migration-free (head `0004`), NO CVE — all minor sandbox/subagent fixes.** Merge `e4379880`; `main` ff'd → `2bd0f56a`. Scope: 8 files (local_sandbox.py, sandbox/tools.py, subagents/executor.py + 5 tests), +257/−31; NO frontend → no re-skin conflict. **The 4:** `#4051` allow bash after cwd setup failure + `#4110` guard the sandbox path-translation regex with a segment boundary (**both were the 2 DEFERRED from the 07-12-later sync — now cleared**), `#4058` use `os.sep` in the reverse-resolve containment check **on Windows** (INERT for us — Linux), `#4056` classify recursion-capped subagent LLM error fallbacks as failed (subagent run path). **Carried patches intact:** backend/Dockerfile readabilipy+Playwright, `.dockerignore` `.deer-flow`, WRI re-skin all present (185 local-only commits vs upstream). **Verified (hardened, all green):** `make up` (gateway+frontend rebuilt) → bootstrap `upgrade head (0004_run_ownership)` (no new migration) → "Application startup complete", 0 boot errors, NO redis/provisioner, `:2026` 200 → **3b sentinel "schema in sync"** (head `0004`) + **6 models** → **`memory.mode=middleware`, `consolidation_enabled=False`, `enabled=True`** → **3c Aio bash smoke** `get_sandbox_provider().acquire_async(user_id=e778f372…).get(sid).execute_command("echo SYNC0713_SANDBOX_OK && uname -s && whoami")` → `AioSandboxProvider`/sandbox `6bcad75c`/`SYNC0713_SANDBOX_OK / Linux / gem`, released ok; log clean. Rollback tag `pre-sync-20260713` → `e8bed30d` (dropped post-push). Deferred backlog now ZERO.

Earlier 2026-07-12 (later): **25 commits (`c143c041`→`897be7e0`), 1 conflict (re-skin, kept OURS), migration-free (head stays `0004`), SECURITY-significant (3 CVEs + 3 prompt-injection fixes).** Merge `18539c80`; `main` ff'd → `897be7e0`. Scope: 53 files, +2735/−188. **Conflict:** `frontend/src/components/workspace/settings/about-content.ts` — upstream `#4126` (show real project version on the About page) collided with our WRI re-skin title; **kept OURS** (`# About WRI AI`, no version, no `bytedance/deer-flow` title link — DeerFlow attribution stays in the "Built On" section); no upstream logic to graft. **Carried patches intact:** on the conflict surface upstream touched only `frontend/Dockerfile` (auto-merged clean, NOT a conflict); backend/Dockerfile readabilipy+Playwright, `.dockerignore` `.deer-flow`, WRI login/setup/landing re-skin all verified present (182 local-only commits vs upstream). **Security:** `#4125` CVE-2026-33128, `#4106` CVE-2026-35209, `#4093` CVE-2026-49477 + prompt-injection hardening `#4119` (html-escape memory context summaries rendered into the injection prompt), `#4097` (html-escape memory facts), `#4099` (neutralize prompt-injection tags in `web_capture` results) — the last three matter directly for the LGI-outreach plan (untrusted web-researched content → deer-flow). **Other:** memory null-confidence/parse-gating fixes (`#4074/#4075/#4076/#4073`), runtime/sandbox robustness (`#4077` serialize SQLite event-store writes, `#4085` drop silent streaming delta-discard, `#4082` re-buffer subagent batch on flush failure, `#4079` str_replace empty-file, `#4096` stop glob/grep surfacing disabled skills), `#4114` show assistant text during tool steps, `#4055` require_mention gating, `#4084` MCP per-server OAuth priming, `#4088` continuation-count race. **Verified (hardened, all green):** `make up` (gateway+frontend rebuilt) → bootstrap `upgrade head (0004_run_ownership)` (no new migration) → "Application startup complete", 0 boot errors, NO redis/provisioner, `:2026` 200 → **3b sentinel "schema in sync"** (head `0004`) + **6 models** → **`memory.mode=middleware`, `consolidation_enabled=False`, `enabled=True`** → **3c Aio bash smoke** `get_sandbox_provider().acquire_async(user_id=e778f372…).get(sid).execute_command("echo SYNC0712C_SANDBOX_OK && uname -s && whoami")` → `AioSandboxProvider`/sandbox `1639b4e6`/`SYNC0712C_SANDBOX_OK / Linux / gem`, released ok; log clean (NO orphan-recovery this time — DB already at `0004`, no inflight orphans); frontend Ready 227ms. Rollback tag `pre-sync-20260712b` → `3c6467a5` (dropped post-push). **⚠ Upstream advanced +2 DURING this sync (`897be7e0`→`c82fba41`): `#4110` guard the sandbox command path-translation regex with a segment boundary, `#4051` allow bash after cwd setup failure — both minor sandbox fixes, no CVE/migration. The `main` mirror was ff'd to `c82fba41`; local-fixes stays at the verified `897be7e0` merge → the +2 are DEFERRED to the next sync (didn't chase a moving tip).**

Earlier 2026-07-12: **2 commits (`1ebf59fe`→`c143c041`), CLEAN merge (0 conflicts), migration-free (head stays `0004`).** Merge `5a1ce4c5`; `main` ff'd → `c143c041`. **The 2:** `#3994` fix(config) checkpointer honors the unified `database:` config (backend sqlite|postgres) instead of needing a legacy `checkpointer` section — legacy section stays authoritative when present, so backward-compatible; in `checkpointer/provider.py`, our postgres run/persist path → warranted the smoke; `#4069` fix(wecom) null-quote guard in the WeChat-Work ws handler — **INERT for us (WeCom is commented out in config.yaml)**. 6 files, 4 tests. **Verified (hardened, all green):** `make up` → bootstrap `upgrade head (0004_run_ownership)` (no new migration) → "Application startup complete", 0 boot errors, NO redis/provisioner, `:2026` 200 → **3b sentinel "schema in sync"** (head `0004`) + **6 models** → **3c Aio bash smoke** `execute_command("echo SYNC0712_SANDBOX_OK && uname -s")` → `AioSandboxProvider`/`dce3d225`/`SYNC0712_SANDBOX_OK / Linux`; `memory.mode=middleware`, consolidation `False`; log clean. Rollback tag `pre-sync-20260712` → `b062dec4` (drop post-push).

Earlier 2026-07-11 (later): **17 commits (`bbb3deb2`→`1ebf59fe`), CLEAN merge (0 conflicts), + FIRST NEW MIGRATION since 0003 (alembic head 0003→0004) + a CVE fix.** Merge `5a89a681` (no re-skin commit — 0 new "DeerFlow" i18n strings); `main` ff'd → `1ebf59fe`. Scope: 140 files, +9794 (test/skill/plans-heavy). **⚠ MIGRATION `0004_run_ownership` — VERIFIED SAFE + APPLIED:** additive only — `safe_add_column` two NULLABLE cols on `runs` (`owner_worker_id` varchar(128), `lease_expires_at` timestamptz) + idempotent indexes `ix_runs_lease` and the UNIQUE `uq_runs_thread_active` (one active run per thread); `down_revision=0003_scheduled_tasks`. Bootstrap ran `upgrade 0003→0004` on `make up` (advisory-locked), head now `0004_run_ownership`, cols+indexes confirmed present, schema sentinel "schema in sync". **The unique index did NOT trip on existing data (single-worker, no dup active runs).** **Benign boot WARNING: "Recovered 7 orphaned inflight run(s) as error"** — the NEW run-ownership reconciliation (`#3948`/`#4003`) cleaning up old inflight runs on startup and setting their STATUS to error; expected, not a failure. **The 17 commits — key ones:** `#4089` CVE-2026-49476 (3-line `uv.lock` transitive-dep bump, inert to code); `#4023` **memory tool sets** — adds `memory.mode: middleware|tool`, the new `tool` mode (model calls memory_search/add/update/delete) is EXPERIMENTAL OPT-IN, **`config.example.yaml` ships `mode: middleware` = our current passive extraction → VERIFIED effective `memory.mode=middleware` on the new image (our gitignored config doesn't set it), consolidation still False**; `#4067` guardrails empty-allowlist now DENIES all (was fail-open); `#4044` html-escape memory state in MEMORY_UPDATE_PROMPT (prompt-injection hardening, like #4028); `#4053` sandbox output-mask regex boundary; `#4078` read_file one-sided ranges; `#4071` SSE reconnect hang fix; `#3948/#4003` multi-worker run atomicity (the code side of migration 0004 — inert single-worker); `#4033/#6389` ensure visible response after tool runs (our run path); `#4037` new `skill-reviewer` public skill + quality gate (inert unless used); `#4050` CI nightly (inert). `config_version: 21→22` (informational). **Verified (hardened, all green):** `make up` (gateway+frontend rebuilt) → bootstrap `upgrade head (0004_run_ownership)` → "Application startup complete", NO redis/provisioner, `:2026` 200 → **3b sentinel "schema in sync"** + **alembic head `0004`** + new `runs` cols/indexes present + **6 models intact** → **effective config: `memory.mode=middleware`, `consolidation_enabled=False`, `memory.enabled=True`** → **3c Aio bash smoke** `get_sandbox_provider().acquire_async(user_id=e778f372…).execute_command("echo SYNC0711B_SANDBOX_OK && uname -s")` → `AioSandboxProvider`/sandbox `47e05ed4`/`SYNC0711B_SANDBOX_OK / Linux`. Log clean (only the benign orphan-recovery warning; zero 5xx/traceback/consolidation). Rollback tag `pre-sync-20260711b` → `5e5cbe39` (drop post-push).

Earlier 2026-07-11: **6 commits (`be637163`→`bbb3deb2`), CLEAN merge (0 conflicts), migration-free, backend-only.** Merge `a2c24c97`; `main` ff'd → `bbb3deb2`. Scope: 15 files, **8 of them tests** (807 insns, ~half tests); **0 migrations (head stays `0003`), 0 compose, 0 Dockerfile, 0 frontend, 0 i18n, 0 `docker/`** → zero carried-patch overlap (redis-strip, `.dockerignore` `.deer-flow`, `UV_EXTRAS=postgres`, Dockerfile readabilipy+Playwright, WRI re-skin, `read_before_write.enabled: true` all verified intact post-merge). **⚠ Recon note: `git diff --name-only local-fixes..upstream/main` is MISLEADING — it lists OUR divergence (landing re-skin, redis-strip, etc.) as "touched", which looks like a huge carried-patch collision. Use `main..upstream/main` (main = clean upstream tip) to see what the NEW commits actually change.** **The 6 commits:** `#4028` html-escape fact content in the memory prompt's staleness/consolidation sections (security / prompt-injection hardening — `memory/updater.py`, NOT the consolidation toggle); `#4042` classify LLM error fallbacks as failed (`subagents/executor.py` + `llm_error_handling_middleware.py`); `#4040` inject durable context before compaction (`subagents/executor.py`); `#3991` fix circuit-breaker wedging after a non-retriable half-open probe; `#4046` `llm_text` unit tests (inert); `#4045` ja/fr/ru README Langfuse docs (inert). Live-code sits in the subagent-executor + LLM/tool error-handling middlewares (the run path) → warranted the smoke. **Verified (hardened, all green):** `make up` (gateway rebuilt + recreated; frontend cached — no FE change) → "Application startup complete", `bootstrap: branch=versioned -> upgrade head (0003_scheduled_tasks)` → `bootstrap: complete`, 0 boot errors, **NO redis / NO provisioner**, app `:2026` 200 → **3b `schema_sync.py` sentinel "schema in sync — no missing columns"** → **6 models intact** (deepseek-v4-pro/kimi-k2.6/qwen3.6-plus/qwen3.7-plus/qwen3.7-max/mimo-v2.5-pro; config.yaml byte-identical) → **3c Aio bash smoke** via `get_sandbox_provider().acquire_async(user_id=e778f372…).get(sid).execute_command("echo SYNC0711_SANDBOX_OK && uname -s")` → `AioSandboxProvider` → sandbox `32f520e2` → `SYNC0711_SANDBOX_OK / Linux` (no TypeError). Consolidation reconfirmed **OFF** (`MemoryConfig().consolidation_enabled=False`). Log window clean: zero 5xx / traceback / consolidation activity. Rollback tag `pre-sync-20260711` → `9119078b` (drop post-push).

Earlier 2026-07-10 (later): **8 commits (`90976426`→`be637163`), CLEAN merge (0 conflicts), migration-free, NOT inert (both images rebuilt).** Merge `1bb6d9e1`; local-fixes tip `c4fa271c` (+1 local re-skin commit, below); `main` ff'd → `be637163`. Scope: 21 files (7 tests, 2 new frontend voice modules); **0 migrations (head stays `0003`), 0 compose, 0 Dockerfile, 0 `docker/`** → every carried patch verified intact post-merge (redis-strip, `.dockerignore` `.deer-flow` ×2, `UV_EXTRAS=postgres`, Dockerfile readabilipy+Playwright ×5, WRI auth/landing re-skin; `read_before_write.enabled: true`). **The recurring i18n re-skin collision did NOT bite this time:** `#4036` (voice dictation) edits the same `en-US.ts`/`zh-CN.ts` as our WRI re-skin and `git merge-tree` flagged them "changed in both", but a real dry-run merge (hard-aborted before touching `local-fixes`) exited 0 — our re-skin hunks (lines 64/521/634/815) and upstream's voice strings (line 122) auto-3-way-merge. **NEW local re-skin commit `c4fa271c`:** the added `voiceInputStart` strings named "DeerFlow" → re-skinned to "WRI AI" in both locales (consistency with the standing re-skin). **The 8 commits:** `#4026` scrub abbreviated `*_PASS` + Postgres `PGPASSFILE` from skill env (security win, extends the #4018 pattern we carry); `#3992` UnboundLocalError in memory injection when facts empty; `#3993` KeyError in staleness review when a fact has no id; `#3989` AttributeError in ThreadDataMiddleware when `runtime.context` is None; `#4035` guard the sandbox reverse-path-translation regex with a segment boundary; `#4036` voice dictation input (Web Speech API mic button — inert unless clicked); `#3995`/`#4022` docs. **`config.example.yaml` change is COMMENT-ONLY** (documents `storage_path` behaviour — no new key/default, so the gitignored-config silent-default risk does NOT apply). Memory fixes are to injection/staleness, NOT consolidation — **consolidation stays OFF (default `False` on the new image, our `config.yaml` doesn't set it — reconfirmed).** **Verified (hardened, all green):** `make up` (gateway+frontend rebuilt + recreated) → "Application startup complete", 0 boot errors, **NO redis / NO provisioner** container, app `:2026` 200 → **3b `schema_sync.py` sentinel "schema in sync — no missing columns"** + alembic `upgrade head (0003_scheduled_tasks)` + **6 models intact** (deepseek-v4-pro/kimi-k2.6/qwen3.6-plus/qwen3.7-plus/qwen3.7-max/mimo-v2.5-pro) → **3c bash smoke**: full HTTP path exercised (register 201 → `/api/models` 200 → `POST /api/langgraph/threads` 200 [CSRF: `X-CSRF-Token` header echoing the `csrf_token` cookie, Origin `https://app.worldresearch.org` — the `GATEWAY_CORS_ORIGINS` value] → `/runs/wait` → run created + dispatched to worker) **AND the Aio sandbox executed bash end-to-end** via `get_sandbox_provider().acquire_async(...).get(sid).execute_command("echo SYNC0710B_SANDBOX_OK && uname -s && pwd")` → `AioSandboxProvider` → sandbox `3fcd9893` → `SYNC0710B_SANDBOX_OK / Linux / /home/gem` (exercises the #4035 path-translation subsystem; no TypeError). **⚠ 3c NOTE: the canonical `ts_team@sipmm.edu.sg` chat-run wasn't used — that password isn't in the gist (by design).** Instead a throwaway user (`smoke_sync_0710b@sipmm.edu.sg`, uid `4adf5034…`) drove the HTTP path; its run errored `Agent directory not found: …/agents/deerflow` because a fresh user has no provisioned agent (unrelated to the merge — it's upstream of sandbox execution), so the sandbox leg was proven directly via the provider against the real provisioned user `e778f372…`. **The throwaway user + its thread/run/checkpoint were REMOVED post-sync** (transactional delete via the gateway engine — `get_app_config()`→`init_engine_from_config(cfg.database)`→scoped `DELETE`s in FK-safe order [checkpoints→runs→threads_meta→users], guarded so exactly one `users` row disappeared and the target's email matched; 2→1 users. No better-auth session/account tables exist in this DB, and no on-disk `.deer-flow/users/<uid>` dir was ever created — the run failed before provisioning). Log window clean: the only traceback is that benign agent-dir error; zero 5xx, zero consolidation activity. Rollback tag `pre-sync-20260710b` → `4113a2a9` (dropped post-push).

Earlier 2026-07-10: **8 commits (`488ec178`→`90976426`), CLEAN merge, migration-free, but NOT inert (gateway image rebuilt).** Merge `3becab82`; `main` ff'd → `90976426`. Scope: 44 files, 16 of them tests; **0 migrations (head stays `0003`), 0 compose, 0 Dockerfile, 0 frontend, 0 `docker/`** → every carried patch untouched (redis-strip, `.dockerignore` `.deer-flow`, `UV_EXTRAS=postgres`, Dockerfile readabilipy+Playwright, WRI auth/landing re-skin; `read_before_write.enabled: true` preserved). Zero conflicts — pre-verified with a throwaway `git worktree add --detach` dry-run before touching `local-fixes`. **⚠ The commit to know is `#3996 feat(memory): memory consolidation`** — it lets the LLM merge fragmented facts into one synthesized fact, and upstream is explicit that this is **LOSSY: source fact content is permanently replaced, only the source IDs survive in `consolidatedFrom`.** It ships `consolidation_enabled: false` by default. **We do NOT set the key in our (gitignored) `config.yaml`, so we inherit `False` — verified against the NEW image**, and the code gates it twice (prompt-build time AND apply time, deliberately, so a config change racing a debounced update cannot silently merge facts). **Keep it off** unless a memory backup/audit story exists; `memory.enabled` is `true` here. Other commits inert or safe for our config: `#4019` MCP auto-promote is gated on `tool_search.enabled` (=`false` here, so `auto_promote_top_k: 3` never fires); `#4012` fixes malformed redis reconnect ids in the **redis** stream bridge (we run the in-memory bridge, single worker, no redis container); `#4018` scrubs `MYSQL_PWD`/`REDISCLI_AUTH`/`REDIS_AUTH` from the inherited skill env (exact-match, wildcards deliberately avoided so `PWD`/`OLDPWD` survive) — a straight security win; `#3990` full-res `image_search` URLs. **Live-code changes that warranted the full smoke:** `#4009` (subagents inherit summarization middleware + hardened step capture), `#4017` (tool-output tail truncation), plus `lead_agent/agent.py`, `utils/messages.py`, and the tool-output-budget / tool-error-handling middlewares — i.e. the path long `web_fetch` outputs take, which the LGI pipeline leans on. **Verified (hardened, all green):** `make up` (gateway+frontend recreated, new gateway image) → "Application startup complete", 0 errors, **NO redis / NO provisioner** container, app `:2026` 200 → **3b `schema_sync.py` sentinel "schema in sync — no missing columns"** + alembic `upgrade head (0003_scheduled_tasks)` → **effective-config probe on the new image: `consolidation_enabled=False`, `tool_search.enabled=False`** → **3c real-chat bash smoke** (`ts_team@sipmm.edu.sg` → `/runs/wait` `qwen3.7-plus` → `echo SYNC0710_OK` through Aio; log: Created sandbox `17ceb451` → Released to warm pool → **Run → success**; zero 5xx / zero TypeError; **zero consolidation activity in the logs**). Rollback tag `pre-sync-20260710` dropped post-push.

Earlier 2026-07-09 (later): **1 commit (`52418d60`→`488ec178`), CLEAN merge, fully INERT.** Merge `efe44f16`; `main` ff'd → `488ec178`. **`#4016 feat(provisioner): ClusterIP services + scoped skills PVC mounts`** touches ONLY `docker/provisioner/app.py` + its README + 2 backend tests — **the provisioner is the OPTIONAL Kubernetes-mode sandbox; we run `AioSandboxProvider` and no provisioner container ever starts**, so zero functional impact. **All 4 changed files are EXCLUDED from our Docker build context** (`.dockerignore` carries `tests/` + `docker/`) → gateway/frontend images unchanged. No migration → head stays `0003`. Zero conflicts, zero carried-patch overlap (redis-strip holds, `.deer-flow` dockerignore, `UV_EXTRAS=postgres`, Dockerfile readabilipy+Playwright, WRI landing/i18n all intact; `read_before_write.enabled: true` preserved). **Verified (hardened, all green):** `make up` (gateway+frontend recreated) → "Application startup complete" (0 errors) + **NO redis / NO provisioner container** + app `:2026` 200 + **3b `schema_sync.py` sentinel "schema in sync — no missing columns"** + alembic `upgrade head (0003_scheduled_tasks)` + gate on + **3c real-chat bash smoke** (`ts_team@sipmm.edu.sg` → `/runs/wait` `qwen3.7-plus` → `echo SYNC0709B_OK` through Aio; log: Created sandbox → Released to warm pool → **Run → success**; zero 5xx / zero TypeError). Rollback tag `pre-sync-20260709b` dropped post-push.

Earlier 2026-07-09: absorbed **39 commits** (`f6a910de`→`52418d60`) — **migration-free (alembic head STAYS `0003`)**; the batch held since 07-05 grew from 2 → 39 commits. Merge `local-fixes@fd3559f7` + `.dockerignore` fix `c360245f`. **Conflicts (4), all the WRI landing re-skin — resolved keep-OURS-skin + `git rm`:** `frontend/src/app/page.tsx` + `landing/header.tsx` + `landing/hero.tsx` (kept OURS = WRI light-theme / maroon `#7b1e2b` / stats-hero / "Insights ↗", vs upstream DeerFlow dark-theme / animated-hero / MobileNav+GitHub-star from `#3740` + others — pure branding, no logic to graft), and `landing/sections/case-study-section.tsx` (modify/delete → kept our deletion via `git rm`). i18n (`en-US.ts`/`zh-CN.ts`) auto-merged clean (WRI strings survived + upstream keys added → 836 lines). **⚠️ NEW build gotcha (handled): the Docker build-context sender hit EPERM on `backend/.deer-flow/.../.mcp/tmp` — a root-owned (mode 700) sandbox dir the July LGI runs created through the Aio sandbox.** `.deer-flow` (gateway runtime home; 481 thread dirs / 31M) was NEVER in `.dockerignore` — earlier builds only worked because no root-owned dir existed yet. **Fixed: added `backend/.deer-flow/` + `**/.deer-flow/` to `.dockerignore`** (runtime data is volume-mounted, never a build input; also −31M context) — **NEW carried patch**. **Carried patches ALL preserved (verified post-merge):** redis-strip holds (compose has only a header comment, deploy.sh 0 refs), `UV_EXTRAS=postgres`, Dockerfile readabilipy+Playwright (8 refs), WRI i18n. **POST-LGI: `#3911` read-before-write gate RE-ENABLED** (`config.yaml read_before_write.enabled: true` — was false for the LGI batch); `injection_enabled` left false (standing pipeline setting). **The 2 previously-held commits are now in: `#3945` per-run workspace-change review + `#3428` wizard-thinking.** Notable batch: `#3960` refuse multi-worker on non-Postgres (we're single-worker+PG ✓), `#4015` bake postgres extra into image, `#3860` memory staleness-review, `#4002` prompt-injection neutralization, `#3969` manual compaction, skills chips/SkillScan, dep bumps, Helm chart (k8s, N/A). **Verified (hardened, all green):** `make up --build` — the FIRST attempt failed on the `.deer-flow` EPERM while the OLD stack kept serving (no downtime); retry after the `.dockerignore` fix succeeded → gateway "Application startup complete" (no tracebacks) + **NO redis container** + app `:2026` 200 + **3b `schema_sync.py` sentinel "schema in sync — no missing columns"** + alembic head `0003` + config **6 models** intact + **3c real-chat bash-smoke** (`ts_team@sipmm.edu.sg` → `POST /api/threads` → `/runs/wait` `model_name=qwen3.7-plus` → `echo SYNC0709_OK` **executed through the Aio sandbox + echoed back**; gateway log: Created sandbox → Released to warm pool → **Run → success**; **zero 5xx / zero TypeError** — aio_sandbox healthy post-merge). Pre-sync `3015742b` (rollback tag `pre-sync-20260709`, delete post-push). `main` ff to `52418d60` separately.

Earlier 2026-07-05: absorbed 18 commits — **CLEAN merge (0 conflicts) + FIRST new migration since 0002 (alembic head 0002→0003)**
(merge `131914b5` → upstream tip `f6a910de`, OPS `next`). **⚠️ ALEMBIC HEAD IS NOW `0003_scheduled_tasks` (was 0002)** — `#3898` scheduled-tasks MVP added migration `0003` (down_revision `0002_runs_token_usage`; STRICTLY ADDITIVE — CREATE TABLE `scheduled_tasks`+`scheduled_task_runs`+indexes only, idempotent `has_table` guard, NO ALTER/DROP on any existing table, zero data-loss). On `make up` the versioned bootstrap ran `upgrade 0002→0003` cleanly; verified `alembic_version=0003_scheduled_tasks` + `scheduled_tasks` table exists + 3b sentinel "schema in sync". The scheduler poller stays OFF (`scheduler.enabled` default False, absent from our config) → tables exist but nothing runs. **Verified via a 3-agent analysis workflow that this batch is LGI-SAFE (run 07-06/07):** `#3790` model `api_base→base_url` normalize is a NO-OP for us (all 6 models use `base_url`/native providers; `api_base` only in commented examples) + the "warn on unknown keys" is warn-only/non-fatal (won't fire for our clean config) → same endpoints; `#3939` DeepSeek-v4 is wizard/docs-only (our config untouched); `#3942` web_fetch SSRF guard is wired ONLY into browserless/crawl4ai/fastcrw → our **Jina web_fetch + Tavily web_search UNTOUCHED** (and it blocks only private IPs anyway) → public research fetching unaffected; the AioSandbox bash path (`env=None`) is behaviourally identical (only a no-op `_validate_extra_env(None)` [#3754] + a `None` `identity_prefix` [#3926] added); `#3920` cost-accounting adds NO migration (sparse `cache_read_tokens` in the existing JSON col). New config sections all additive/safe-default (scheduler off, `skills.deferred_discovery` off, `agents.github` none). **Carried local state PRESERVED (verified post-merge):** `read_before_write.enabled=false` (our #3911 gate — still disabled pending the LGI run) + **redis NOT re-added** (our #3191 strip holds; compose+deploy.sh have 0 redis). Overlaps prompt.py `<language>` + i18n WRI-AI auto-merged. Other 13 inert (frontend/channels[Feishu/Wechat/GitHub]/langfuse/k8s-provisioner/BoxLite-scaffold/skills-secrets/deferred-discovery/upload-IO-offload). **Verified (hardened):** `make down && make up` → gateway "startup complete" (1×, `branch=versioned -> upgrade head 0003_scheduled_tasks`, no tracebacks) + **NO redis container** + app `:2026` 200 + **3b "schema in sync"** + `alembic head=0003_scheduled_tasks` + `/api/models` **6 intact** + `read_before_write.enabled==False` + **3c bash smoke** (`echo SYNC0705_OK` via Aio, **NO TypeError**), run persisted `status=success`, zero 5xx. Pre-sync `22922312` (rollback `rollback-presync-20260705`, delete post-verify). `main` ff to `f6a910de` separately.

Earlier 2026-07-04 sync absorbed 12 commits — **CLEAN merge (0 conflicts) + a LOCAL redis-strip customization**
(merge `61bf3ab4` → upstream tip `80e031dc`, redis-strip commit `08a1a242`, OPS `next`). **Verified via a 3-agent analysis workflow that ALL auto-merged build-critical overlaps stayed coherent:** backend/Dockerfile (our readabilipy+Playwright blocks + UV_EXTRAS asyncpg loop intact; upstream added a harmless `--extra redis` — the extra is defined so the build is fine, asyncpg still installs), `lead_agent/prompt.py` (our `<language>` block present exactly once + upstream's #3889/#3906 skill-cache/user_id changes integrated), i18n (WRI-AI rebrand survives + new `pleaseWaitStreaming` key), no new alembic migration → head `0002`. **LGI-SAFE (run 07-06/07):** `#3889`'s new file gate is scoped STRICTLY to `/mnt/skills` (`_is_disabled_skill_path`) → `/mnt/user-data/…` workspace I/O + bash exec (the LGI Stage-1 path) UNTOUCHED; `aio_sandbox.py` not touched; the #3911 read-before-write gate STAYS disabled (batch has zero read_before_write changes; our gitignored config.yaml never overwritten). **⚠️ THE ONE CATCH — handled: `#3191` (redis stream bridge) auto-merged an UNCONDITIONAL `redis:7-alpine` service + a HARD `gateway depends_on: redis(service_healthy)` into `docker-compose.yaml` + `services="redis …"` in `deploy.sh`.** We run a SINGLE worker with the default in-memory stream bridge (`stream_bridge` unset → `None` → memory), so redis is unused, and setting `type=memory` does NOT remove the COMPOSE dep. So I **STRIPPED redis** from our (locally-patched) compose + deploy.sh (removed the service, the gateway `depends_on`+`DEER_FLOW_STREAM_BRIDGE_REDIS_URL` env, the `redis-data` volume, and `redis` from deploy.sh's `services` list) — commit `08a1a242`, keeping our validated single-worker topology. ⚠️ **GOTCHA for next sync: any future compose/deploy.sh redis re-add will look like a conflict/overlap — keep stripping it unless we move to multi-worker (then set `stream_bridge.type=redis` + restore the service).** Other 10 inert/safe: `#3875` subagent loop-detection (ultra only), `#3927` Discord IO (INERT), `#3933` redis retention (INERT — no redis), `#3878`/`#3908` frontend fixes, `#3906` structured runtime metadata (emit-only), `#3924` skill-install security scan, `#3928` provisioner port (INERT), `#3812` MCP route-by-server (inert unless MCP), `#3917` limit upload manifest. **Verified (hardened):** `make down && make up` → gateway "startup complete" (1×, no redis/ImportError/tracebacks) + **NO redis container** (gateway/nginx/frontend only) + app `:2026` 200 + **3b "schema in sync"** + `/api/models` **6 intact** + `read_before_write.enabled==False` + `stream_bridge==None` (in-memory) + **3c bash smoke** (`echo SYNC0704_OK` via Aio, **NO TypeError**), run persisted `status=success`, zero 5xx. (Hiccup: a foreground `make up` hit the 2-min tool timeout mid-build → re-ran in background; harmless.) Pre-sync `f7429ddf` (rollback `rollback-presync-20260704`, delete post-verify). `main` ff to `80e031dc` separately.

Earlier 2026-07-03 (latest) sync absorbed 5 commits — **CLEAN merge + 1 INSTANCE-CONFIG toggle (disabled #3911 gate pending the LGI run)**
(merge `ccdde5fc` → upstream tip `48477d86`, merge commit `ccdde5fc`, OPS `next`). **The 5:** `#3911` feat(middlewares) **deterministic read-before-write version gate for file tools** — new `ReadBeforeWriteMiddleware` (default `enabled: True`) that BLOCKS a `write_file`/`str_replace` OVERWRITE of an EXISTING file unless the agent read it first (new-file creates always allowed). **We DISABLED it in the gitignored instance `config.yaml`** (`read_before_write: {enabled: false}`, added near `tool_search`) so the **2026-07-06/07 LGI June production run** uses the exact validated agent file-tool behaviour — the LGI Stage-1 research fallback writes JSON to `/mnt/user-data/outputs/…` (new-file creates, wouldn't be gated anyway, but zero-risk this way). ⚠️ **RE-ENABLE (remove the block / set true + `make down && make up`) AFTER the LGI run.** Other 4 all inert/safe: `#3922` fix(sandbox) AIO fail-fast when the image lacks `bash.exec` (**env-bearing/skill-secret path ONLY** — our normal bash is `env=None` → legacy `shell.exec_command`, untouched; auto-merged since aio_sandbox.py is now vanilla), `#3862`/`#3863` skills preserve `read_file` for lead skill loading (bug fix), `#3907` frontend citation-sources evidence panel (additive UI), `#3837` guardrails persist interventions as run_events (INERT — `guardrails.enabled` off; existing table, no migration). Merge-tree exit 0, zero conflicts, i18n auto-merged (WRI branding intact), **no new alembic migration** → head `0002`. **Verified (hardened):** `make down && make up` → gateway "startup complete" (1×, no tracebacks) + all 3 containers up + app `:2026` 200 + **3b "schema in sync"** + `/api/models` **6 intact** + **`get_app_config().read_before_write.enabled == False`** (gate confirmed OFF in the running container) + **3c bash smoke** (`echo SYNC0703D_OK` via Aio, **NO TypeError**), run persisted `status=success`, zero 5xx. Pre-sync `0addb22a` (rollback `rollback-presync-20260703c`, delete post-verify). `main` ff to `48477d86` separately.

Earlier 2026-07-03 (later) sync absorbed 5 commits — **CLEAN merge** (merge-tree exit 0, zero conflicts — cleanest in a while now the Aio shim is gone)
(merge `c34857db` → upstream tip `ddca8641`, merge commit `aa536bc5`, OPS `next`). **Small, mostly-docs batch, all INERT/additive for us:** `#3830`/`#3913`/`#3833` docs-only (production-DB guidance, stale-doc/typo fixes, docker mount-path clarification — the AGENTS.md scripts-list update + new `scripts/nginx.sh` helper rode in with these), `#3843` feat(mcp) **per-server `tool_call_timeout`** for MCP tool calls (additive `extensions_config` field + `mcp/tools.py`; INERT unless an MCP server sets it — we don't), `#3869` fix(sandbox) **normalize Windows backslash→forward-slash in RESOLVED virtual paths** (`sandbox/tools.py`+`local_sandbox.py`: `.replace("\\","/")` applied ONLY to the resolved host path of a matched `/mnt/...` virtual path, NOT to arbitrary command backslashes → **no-op on our Linux/Aio deployment**, forward-slash paths already). No conflict (aio_sandbox.py is now vanilla-upstream post-shim-removal so nothing to collide); i18n en-US/zh-CN auto-merged (WRI branding intact); no new alembic migration → head `0002`; only `extensions_config.example.json` touched (we don't consume it). **Verified (hardened):** `make down && make up` → gateway "startup complete" (1×, no tracebacks) + all 3 containers up + app `:2026` 200 + **3b "schema in sync"** + login 200 + `/api/models` **6 intact** + **3c bash smoke** (`echo SYNC0703C_OK` → marker returned via Aio, **NO TypeError**), run persisted `status=success`, zero 5xx. Pre-sync `c34857db` (rollback `rollback-presync-20260703b`, delete post-verify). `main` ff to `ddca8641` separately.

Earlier 2026-07-03 sync absorbed 13 commits — **1 conflict resolved TAKE-UPSTREAM (dropped our Aio shim — now redundant)**
(merge `c31a3a44` → upstream tip `25ea6970`, merge commit `9016eea8`, OPS `next`). **HEADLINE: our 06-30 Aio `timeout=` shim is REMOVED** — upstream `#3871` (request-scoped skill secrets) reworked `AioSandbox.execute_command` to `(self, command, env=None, timeout=None)`, which SUPERSEDES our shim (it now has `timeout` natively, plus a new `env=` for per-command secret injection). The ONLY merge conflict was `community/aio_sandbox/aio_sandbox.py` (our shim vs #3871) → resolved `git checkout --theirs` (take-upstream, drop shim); our fork divergence shrinks by one patch. This is exactly the "TAKE-UPSTREAM once #3864 is fixed" note we'd carried. Normal bash path is byte-identical (`env=None` → `del timeout; if env:` false → legacy `shell.exec_command`); `sandbox.py` base + `sandbox/tools.py` also gained `env=` (tools.py now calls `execute_command(command, env=injected_env, timeout=command_timeout)`). **The other 12 auto-merged clean** (i18n en-US/zh-CN auto-merged, WRI branding preserved; login/setup frontend pages untouched). **Notable, all verified SAFE via a 3-agent analysis workflow:** `#3904` fix(store) unified DB config = **no-op for us** (our `checkpointer.type=postgres` section takes precedence in the new `_resolve_store_config`, so the LangGraph Store stays on postgres via `$DATABASE_URL` — the fallback-to-`database`-section only fires when `checkpointer` is ABSENT); `#3903` recursion clamp ceiling defaults to **1000 = our value** → no-op; `#3872` fix(auth) csrf cookie now lives as long as the access_token (positive; we're HTTPS); `#4fcb4bc3` subagent step-history persists to the **EXISTING `run_events` table** (no new schema, best-effort/errors-swallowed → won't break ultra runs); `#3902` observability trace-id **OFF by default** (`logging.enhance.enabled` absent → inert, plain logs); `#3858` goal continuations opt-in via `/goal` (cheap post-run goal read returns None when unset); `#3757` agents-UI gate is a no-op (our `agents_api.enabled=true`); `#3821` Crawl4AI + `#3866` Brave image = INERT community providers. **⚠️ ONE OPERATIONAL NOTE:** `#3855` now requires **admin** for ALL skill-management endpoints (install/toggle/rollback) — if the operator login isn't admin, UI skill-toggling 403s (NOT on the chat path). **No new alembic migration** → head stays `0002`; config-schema changes (`app_config.py` logging+max_recursion_limit) are additive with safe defaults → gateway starts on our config.yaml unchanged (only a non-fatal config_version 9<17 warning). **Verified (hardened, all green):** `make down && make up` → `+ asyncpg==0.31.0` → gateway "startup complete" (1×, **no ImportError/tracebacks, no InMemoryStore fallback → `$DATABASE_URL` Store resolution fine**) + all 3 containers up + app `:2026` 200 + **3b "schema in sync"** + login 200 w/ csrf+access_token cookies + `/api/models` **6 intact** + **3c BASH-TRIGGERING smoke** (the take-upstream Aio `env=None` path): *"echo SYNC0703_OK"* → `[tool]` returned **`SYNC0703_OK`** (bash through Aio, **NO TypeError**), persisted `status=success` 25600 tokens, **zero 5xx**, logs plain (no trace_id). Pre-sync `c31a3a44` (rollback `rollback-presync-20260703`, delete post-verify). `main` ff to `25ea6970` separately.

Earlier 2026-07-01 (later) sync absorbed 6 commits — **CLEAN merge** (merge-tree exit 0; shim + our Dockerfile patch both survived)
(merge `b64ecc31` → upstream tip `2e15e3fe`, merge commit `2da621ba`, OPS `next`). **The 6:** `#3897` fix(docker) **production Postgres UV-extras detection** (⚠️ Dockerfile OVERLAP with our local patch — but our patch is readabilipy+Playwright, NOT the asyncpg area; text-merge CLEAN; #3897 reworks `UV_EXTRAS` with strict `[A-Za-z][A-Za-z0-9_-]*` validation + adds `.env`/config auto-detection to `deploy.sh` + new `detect_uv_extras.py`. **NET IMPROVEMENT for us** — our repo-root `.env` has `UV_EXTRAS=postgres` [valid token] AND `config.yaml` has `database.backend: postgres` + `checkpointer.type: postgres` [auto-detect triggers], so asyncpg is doubly-ensured; **build log confirmed `+ asyncpg==0.31.0` installed** via the new loop), `#3887` feat preserve **durable context across summarization** (LIVE — new `durable_context_middleware.py` + summarization/thread_state/input_sanitization changes + `summarization_config` fields; in-state, NO migration), `#3874` fix fallback title for interrupted first-turn runs (LIVE — extends the #3885 title path we run), `#3870` fix(frontend) stop rendering reasoning twice (LIVE, minor), `#3881` feat(community) **Browserless web_capture** screenshot tool (INERT — we use Tavily/Jina), `#3886` feat(scripts) redacted support-bundle generator (INERT — dev/support script). **CRITICAL pre-checks:** none touch `aio_sandbox.py`/`tools.py`/base `sandbox.py` → **Aio shim unaffected** (verified present post-merge); the only local-patch overlap is `backend/Dockerfile` and it merged CLEAN (our Playwright block + #3897's UV_EXTRAS loop coexist); **no new alembic revision** → bootstrap no-op (`branch=versioned -> upgrade head (0002)`); config.example.yaml + summarization_config add new fields (defaults apply; our config.yaml untouched). **Verified (hardened, all green):** `make down && make up` → build log shows #3897 UV_EXTRAS loop ran + **`+ asyncpg==0.31.0`** (the exact asyncpg-crash surface — now installed via the new mechanism) → gateway "Application startup complete" (1×, **no asyncpg ImportError**/tracebacks) + all 3 containers up + app `:2026` 200 + **3b sentinel "schema in sync"** + `/api/models` authed → **6 models intact** + **3c BASH-TRIGGERING smoke** (confirms the merged summarization/title/upload/durable-context chain + Aio shim): `ts_team@sipmm.edu.sg` → `/runs/wait` 200 with *"echo SYNC0701B_OK"* → `[tool]` returned **`SYNC0701B_OK`** (bash through Aio, NO TypeError), persisted `status=success` 25604 tokens, zero 5xx. Pre-sync `b64ecc31` (rollback `rollback-presync-20260701b`, delete post-verify). `main` ff to `2e15e3fe` separately.

Earlier 2026-07-01 (earlier) sync absorbed 4 commits — **CLEAN merge** (merge-tree exit 0, zero conflicts; our 06-30 Aio shim survived untouched)
(merge `8739e17b` → upstream tip `8fa6ed2b`, merge commit `2218e039`, OPS record next). **The 4:** `#3822` fix(gateway) oversized upload replacements deleting existing files (**LIVE + beneficial** — real data-loss bug in the upload path we use; `uploads/manager.py`+`uploads_middleware.py`+`routers/uploads.py`), `#3877` feat(subagents) system-maintained **delegation ledger** to stop redundant re-delegation (**LIVE in ultra/subagent mode**; new `delegation_ledger_middleware.py` + `thread_state.py` `merge_delegations` reducer — stored IN LangGraph thread-state, **NO DB table/migration**), `#3880` fix(frontend) keep orphan tool messages visible (**LIVE**, minor chat-render; `core/messages/utils.ts`), `#3883` feat(sandbox) **add E2B sandbox provider** (**INERT for us** — we run Aio, not E2B; new `community/e2b_sandbox/*` + makes `e2b-code-interpreter>=2.8.1` a **core dep** now installed in the image but never instantiated unless configured). **CRITICAL pre-checks:** none of the 4 touch `aio_sandbox.py` / `sandbox/tools.py` / the base `sandbox.py` interface → **our 06-30 Aio `timeout`-kwarg shim is unaffected** (verified `execute_command(self, command, timeout=None)` still present post-merge); no local-patch overlap; **no new alembic revision** → bootstrap no-op (`branch=versioned -> upgrade head (0002)`). **Verified (hardened, all green):** `make down && make up` (e2b dep installed: 218 pkgs in 9.5s; frontend+gateway rebuilt) → gateway "Application startup complete" (1×, no tracebacks/ImportError) + all 3 containers up + app `:2026` 200 + **3b sentinel "schema in sync"** + `/api/models` (authed) → **6 models intact** (deepseek-v4-pro, kimi-k2.6, qwen3.6-plus, qwen3.7-plus, qwen3.7-max, mimo-v2.5-pro) + **3c BASH-TRIGGERING smoke** (confirms the merged subagent-ledger/upload middleware chain + Aio shim still execute): `ts_team@sipmm.edu.sg` → `/runs/wait` 200 with *"echo SYNC0701_OK"* → `[tool]` returned **`SYNC0701_OK`** (bash through the Aio sandbox, NO TypeError), persisted `status=success` 25600 tokens, **zero 5xx/TypeError** in logs. `config.yaml` untouched. Pre-sync `8739e17b` (rollback branch `rollback-presync-20260701`, delete post-verify). `main` ff to `8fa6ed2b` separately.

Earlier 2026-06-30 sync absorbed 2 commits — **CLEAN merge + 1 WRI local shim** (for a stacked held commit)
(merge `a1b1f9aa` → upstream tip `2453718a`, merge commit `d26620ec`, shim commit `bebd0956`). **Context:** `fe825520` (#3864 sandbox bash-timeout) was DELIBERATELY HELD on 2026-06-30's earlier check because its shared `tools.py` calls `sandbox.execute_command(command, timeout=…)` for EVERY provider but only updates `LocalSandbox` — our community **`AioSandboxProvider`** (`config.yaml:764`) has no `timeout` kwarg → would `TypeError` on every agent bash call. The next day `2453718a` (#3885 fix(title): avoid default LLM call before stream end) stacked on top of it — a genuinely beneficial fix for us (our `title.model_name: null` previously fired an extra default-model LLM call per new chat; now the `null` path builds the title LOCALLY, no LLM call). To get `2453718a` we synced to the tip (which also brings `fe825520`) + added a **1-line local shim** to make the Aio provider safe. **Shim (`bebd0956`):** `community/aio_sandbox/aio_sandbox.py::execute_command` now accepts `timeout: float | None = None` (interface-compat; the AIO sandbox already bounds commands via its own `no_change_timeout`, so the param is accepted but not separately applied). Text-merge was CLEAN (merge-tree exit 0, no local-patch overlap), **no schema/migration** (alembic no-op at head `0002`), only `config.example.yaml` + new `sandbox_config.bash_command_timeout` field touched (our gitignored `config.yaml` untouched). **Verified (hardened workflow, all green):** `make down && make up` (frontend+gateway rebuilt) → gateway "Application startup complete" (1×, no tracebacks) + app `:2026` 200 + **3b `schema_sync.py` sentinel "schema in sync — no missing columns"** + **3c BASH-TRIGGERING smoke** (the shim's exact regression surface): `ts_team@sipmm.edu.sg` → `POST /api/langgraph/threads` 200 → `/runs/wait` 200 with *"use the bash tool to run `echo SHIM_OK_8842`"* → the `[tool]` result returned **`SHIM_OK_8842`** (bash executed through `AioSandbox.execute_command(command, timeout=…)` with NO `TypeError`), AI echoed it back; persisted run `status=success total_tokens=25618 token_usage_by_model={deepseek-v4-pro}`, **zero 5xx / zero TypeError** in gateway logs. `config.yaml` untouched (qwen/Bailian intact). ⚠️ **The shim is a local patch on a file upstream owns** — if upstream later fixes #3864 properly (updates the Aio provider signature), `aio_sandbox.py` WILL conflict on a future sync; resolve by taking upstream's version. Pre-sync `a1b1f9aa` (rollback branch `rollback-presync-20260630`, delete post-verify). `main` ff to `2453718a` separately.

Earlier 2026-06-28 (later) sync absorbed 3 commits — **2 conflicts resolved** (non-clean)
(merge `1e573400` → upstream tip `b3c312b7`, merge commit `0c4d27c0`). **Conflicts (2), both from `#3844` (fix auth setup redirects) colliding with our WRI auth re-skin — resolved keep-OURS-skin + adopt-upstream-logic:** (1) `frontend/src/app/(auth)/login/page.tsx` — kept our WRI re-skin (maroon `#7b1e2b`/cream gradient, "WRI AI / World Research Institute" logo header, white card, uppercase labels) and GRAFTED upstream's two real behaviour changes: the `{systemNeedsAdminSetup}` admin-setup notice (re-skinned to WRI maroon, not `border-blue-500`) and the `{regularSignupAllowed && (…)}` gate on the sign-up/sign-in toggle + the `handleSubmit` early-return guard — login no longer hard-redirects to `/setup`. The new `@/core/auth/setup` helper import + the 3 new i18n keys auto-merged clean. (2) `frontend/src/app/(auth)/setup/page.tsx` — kept our WRI skin; took upstream's 3 redirect bug-fixes (`fetchSetupStatus()` no-store/credentials, `router.push`→`router.replace` ×3, `isSystemAlreadyInitializedError`→`/login` guard). Also re-skinned the new `adminSetupRequiredDescription` i18n string "DeerFlow"→"WRI AI" (en-US + zh-CN). **The other 2 commits auto-merged clean:** `#3809` refactor(middlewares) declarative layered builder + chain-order tests (backend; IN our live lead path but a behavioural **NO-OP** for us — we run `lazy_init=True`, the fixed bug needs `lazy_init=False`), `#3854` fix(frontend) retain presented artifacts in header dropdown (continues the #3198 artifact lineage; live UX correctness). **No backend-schema/config/persistence change; no new migration** → alembic no-op (`branch=versioned -> upgrade head (0002)`). **Verified (hardened workflow, all green):** adversarial conflict-resolution review **PASS** (JSX valid, imports resolve, upstream behaviour adopted, WRI skin preserved, i18n keys present, no dup) → `make down && make up` (frontend + gateway rebuilt; frontend compiled clean = JSX valid) → gateway "Application startup complete" (no tracebacks) + app `:2026` `/`+`/login` 200 + **3b `schema_sync.py` (read-only sentinel) "schema in sync — no missing columns"** + **3c real-chat smoke** (`ts_team@sipmm.edu.sg` → `POST /api/langgraph/threads` 200 → `/runs/wait` 200 → AI reply **"pong"**; persisted run `status=success total_tokens=12673 token_usage_by_model={deepseek-v4-pro}`, **zero 5xx**) + `/api/models` 200 (6 models intact: deepseek-v4-pro, kimi-k2.6, qwen3.6-plus, qwen3.7-plus, qwen3.7-max, mimo-v2.5-pro) + rendered `/login` shows WRI branding ("World Research Institute"/"WRI AI"/`#7b1e2b`) with ZERO DeerFlow leftover (no `font-serif`/`border-blue-500`/`DeerFlow</h1>`). `config.yaml` untouched (gitignored; qwen/Bailian token-plan intact). Pre-sync `1e573400` (rollback branch `rollback-presync-20260628b`, delete post-verify). `main` ff to `b3c312b7` separately.

Earlier 2026-06-28 (earlier) sync absorbed 4 commits — **CLEAN merge** (merge-tree exit 0)
(merge `41cb1fbd` → upstream tip `22290c16`, merge commit `ce2ac0d1`). The 4 are all bug fixes: `#3825`/`#3826` fix(frontend) **preserve messages across context summarization** (`frontend/src/core/threads/hooks.ts` + a message-merge unit test — relevant to chat UX), `#3780` fix: **ignore middleware prompts in run-journal input** (`harness/deerflow/runtime/journal.py` + test), `#3810` fix(feishu) stop creating thread topics + throttle card updates (`app/channels/feishu.py`/`manager.py` + test — **INERT for us**, no Feishu configured), `#3828` fix: reap macOS nginx processes on stop (`scripts/serve.sh` + test — **INERT**, we're Linux/Docker). **No backend-schema/config/persistence change; ZERO overlap with our local patches** (prompt.py/login/Dockerfile/compose/nginx untouched — merge-tree exit 0, `ort` strategy, no conflicts). No new migration → alembic no-op (`branch=versioned -> upgrade head (0002)`). Rebuilt **`make down && make up`** (gateway + frontend images rebuilt): gateway "Application startup complete" (clean, no tracebacks), app `:2026` 200, **3b `schema_sync.py` (read-only sentinel) → "schema in sync — no missing columns"**, **3c HTTP smoke** (`ts_team@sipmm.edu.sg` → `POST /api/threads` → `/runs/wait` with `model_name=qwen3.7-plus` → reply **"pong"**; gateway log zero 5xx/tracebacks). `config.yaml` untouched (qwen/Bailian token-plan intact). Pre-sync `41cb1fbd` (rollback branch `presync-20260628`, deleted post-verify). `main` ff to `22290c16` separately.

Earlier 2026-06-27 sync absorbed 3 commits — **CLEAN merge** (merge-tree exit 0)
(merge `d85722c6` → upstream tip `3e2f1bbe`, merge commit `c24933af`). All 3 are FRONTEND artifact-preview fixes — `#3198` show filename for presented artifacts missing from `thread.values.artifacts`, `#3791` preserve artifacts during streaming updates, `#3805` markdown heading-anchors in artifact previews. **Frontend-only** (9 files: artifact-panel components + e2e/unit tests + **one new frontend dep** in `package.json`/`pnpm-lock.yaml`); NO backend/schema/config or local-patch overlap (prompt.py/login/Dockerfile/compose/nginx untouched). No new migration → alembic no-op (`branch=versioned -> upgrade head (0002)`). Rebuilt **`make down && make up`** (frontend rebuild + `pnpm install` pulls the new dep): gateway "Application startup complete" (clean), app `:2026` 200, **3c HTTP smoke** (`ts_team@sipmm.edu.sg` → `POST /api/threads` → `/runs/wait` → "pong"; run persisted, zero 5xx). `config.yaml` untouched (qwen/Bailian intact). Pre-sync `d85722c6` (rollback branch `presync-20260627`, deleted post-verify). `main` ff to `3e2f1bbe` separately.

Earlier 2026-06-26 (later) sync absorbed 1 commit — **CLEAN merge** (merge-tree exit 0)
(merge `630e71a3` → upstream tip `7a6c4a99`, merge commit `d29123b1`). Single commit `#3799` fix(channels): serialize per-chat thread creation to avoid duplicate threads — touches ONLY `backend/app/channels/manager.py` (+ its test); **INERT for us** (IM channels disabled, none configured). No config/.env/schema change, no new migration → alembic no-op at head `0002`. Rebuilt `make down && make up`: gateway "Application startup complete" (clean), app `:2026` 200, 3c HTTP smoke (`ts_team@sipmm.edu.sg` → `/runs/wait` → "pong", run persisted, zero 5xx). Pre-sync `630e71a3` (rollback branch `presync-20260626b`, deleted post-verify). `main` ff to `7a6c4a99`.

Earlier 2026-06-26 sync absorbed 9 commits — **CLEAN merge** (merge-tree exit 0, zero conflicts)
(merge `eb27e354` → upstream tip `71c5c4a0`, merge commit `d3c46752`). **Headline: `#3793` nginx `X-Forwarded-Proto` fix — directly relevant to our Caddy→nginx→gateway chain.** Our compose nginx (`docker/nginx/nginx.conf`, run upstream-verbatim) previously forced `proxy_set_header X-Forwarded-Proto $scheme` on all 10 location blocks → since Caddy→nginx is plain HTTP, the gateway saw `http` even for real HTTPS clients (drops the session-cookie `Secure` flag + risks a CSRF Origin-scheme 403). #3793 adds a `map $http_x_forwarded_proto $forwarded_proto { default $scheme; ... }` so nginx PRESERVES Caddy's `X-Forwarded-Proto: https`, falling back to `$scheme` only when no upstream proxy set it (the direct-:2026 path is unchanged). **Verified post-deploy:** a login to `:2026` with `X-Forwarded-Proto: https` now returns an `access_token` cookie WITH `Secure`; with `http` it correctly does NOT (control) — the gateway scheme now tracks the forwarded proto. **No alembic migration in this batch** → the bootstrap took the clean `branch=versioned -> upgrade head (0002)` no-op path (DB stays at head `0002`; first restart on the steady-state versioned path since the 06-25 legacy transition). **Verified (hardened workflow):** `make down && make up` → gateway "Application startup complete" (clean) + app `:2026` 200 + nginx reloaded with the `$forwarded_proto` map + `schema_sync.py` (read-only sentinel) "schema in sync — no missing columns" + **3c HTTP chat smoke** (login `ts_team@sipmm.edu.sg` → `POST /api/threads` → `/runs/wait` → reply "pong"; persisted run `status=success model=qwen3.7-plus token_usage_by_model={qwen3.7-plus, deepseek-v4-pro}`, zero 5xx). `config.yaml` untouched (qwen/Bailian token-plan intact). **Other commits (inert/low-impact):** `#3760` a new **Hermes-like `deerflow` terminal-workbench TUI** backed by DeerFlowClient (+ tests/docs/svgs — ~half the diff; inert, we don't use the TUI), `#3768` frontend notification-permission refresh, `#3764` block unresolved suggestion placeholders, `#3778` skills storage singleton lifecycle (empty skills map → inert), `#3772` isolate MCP tool-discovery failures (mild Playwright-MCP resilience), `#3786` allow valid heredocs in sandbox audit, `#3770` docs adopt `AGENTS.md` as source of truth (CLAUDE.md imports it; doc-only). Pre-sync state `eb27e354` (rollback branch `presync-20260626`, delete post-verify). `main` ff to `71c5c4a0` separately.

Earlier 2026-06-25 sync absorbed 12 commits — **CLEAN merge** (merge-tree exit 0, zero conflicts)
(merge `a9a96fe9` → upstream tip `30841c3b`, merge commit `5bdcaf13`). **Headline: alembic migrations wired (#3706 `debb0fd1`)** — replaces the bare `create_all` bootstrap with a hybrid lifespan bootstrap (empty→create_all+stamp head; **legacy→create_all backfill + stamp `0001_baseline` + upgrade head**; versioned→upgrade head; `pg_advisory_lock`'d). On our legacy DB the first `make up` logged exactly `branch=legacy -> create_all + stamp 0001_baseline + upgrade head (0002_runs_token_usage)` → DB is now **alembic-versioned at head `0002`**; the legacy→versioned transition is **one-time** (next restart logged `branch=versioned -> upgrade head`, no re-run, no re-warn). **This SUPERSEDES `schema_sync.py` as the schema manager** — alembic now OWNS DDL; `schema_sync.py` kept as a read-only drift sentinel (see step 3b). **One residual drift handled:** 0002's `safe_add_column` found `runs.token_usage_by_model` already present (added by the 2026-06-21 schema_sync fix) but **missing the model's `server_default '{}'`** (the type diff `JSON(astext_type=Text())` vs `JSON()` is a benign reflection artifact) → it WARNed and left as-is. Applied the one canonical manual ALTER `ALTER TABLE runs ALTER COLUMN token_usage_by_model SET DEFAULT '{}'::json` so the column now exactly matches the model (`nullable=NO` + `server_default '{}'`); `schema_sync.py` (read-only) → "schema in sync — no missing columns". **Verified (hardened workflow):** `make down && make up` → gateway "Application startup complete" (alembic bootstrap clean, no tracebacks) + app `:2026` 200 + **Bailian model live** (embedded `DeerFlowClient.chat(model_name=qwen3.7-plus)` → "pong", 8.7s) + **runs ORM round-trip** (INSERT/SELECT/UPDATE/DELETE on the live schema — the exact 2026-06-21 failure surface — all clean, count restored) + **3c canonical HTTP smoke** — logged in `ts_team@sipmm.edu.sg` (CSRF: `csrf_token` cookie set on the *login* response, `X-CSRF-Token` header on state-changing POSTs) → `POST /api/threads` (create) → `POST /api/threads/{id}/runs/wait` → reply "pong"; persisted run `c6df1718…` `status=success model=qwen3.7-plus total_tokens=12919 token_usage_by_model={qwen3.7-plus:…, deepseek-v4-pro:…}`, **zero 5xx** in logs — run/persist + token-usage verified end-to-end. `config.yaml` untouched (gitignored; qwen/Bailian token-plan intact). **Other notable commits:** `#3674` channel-config precedence (UI runtime channel config wins over `config.yaml` — *messaging* channels, NOT LLM model config, so our Bailian models unaffected), `#3711` coalesce SystemMessages before LLM request, `#3686`/`#3562` perf indexing (MemoryRunEventStore by run_id; MemoryRunStore by thread_id), `#3730` sandbox provider singleton lifecycle, `#3746` middleware ID-swap recursion fix, `#3551` artifact filesystem-IO offload, `#3733`/`#3557` frontend clickable chat rows + math rendering, `#3747`/`#3749` shared message→text helper + dead-code removal. Pre-sync state `a9a96fe9` (rollback branch `presync-20260625`, delete post-verify). `main` ff to `30841c3b` separately (default-branch push needs auth).

Earlier 2026-06-23 sync absorbed 35 commits — **2 conflicts resolved** (first non-clean sync)
(merge `cd34f7a0` → upstream tip `cefc53c7`, merge commit `e562f8b3`). **Conflicts:** (1) `agents/lead_agent/prompt.py` — ADDITIVE, kept BOTH our local `<language>` block AND upstream's new "System-Context Confidentiality (CRITICAL)" + user-input-boundary security block (#3630/#3661/#3662 prompt-injection / role-isolation hardening). (2) `frontend/src/app/(auth)/login/page.tsx` — kept OURS at merge, then **ADOPTED upstream's i18n + OIDC-SSO login in a follow-up (re-skinned with WRI branding)**: started from `upstream/main`'s login (keeps `useI18n` `t.login.*` copy, `/api/v1/auth/providers` SSO-provider buttons, the post-fail SSO hint), dropped the deer FlickeringGrid/useTheme, and re-applied the WRI identity (logo `/wri/android-chrome-192x192.png`, "WRI AI / World Research Institute" lockup, maroon `#7b1e2b` button, cream gradient bg, white card). Rebuilt with **`make up`** alone (frontend-only code change → build-while-serving, recreate changed). Verified: `/login` renders WRI branding + i18n title ("Sign in to your account") + maroon button; **email login still works + chat smoke 200/no-5xx**. **SSO buttons render only when an OIDC provider is configured** — none is yet, so the capability is present but dormant (configure a provider to surface the "Continue with …" buttons). **Verified:** `make down && make up` full recreate → gateway "Application startup complete" (clean, no tracebacks) + app `:2026` 200 + **direct Bailian `GET /models` 200** + 6 models intact (`deepseek-v4-pro, kimi-k2.6, qwen3.6-plus, qwen3.7-plus, qwen3.7-max, mimo-v2.5-pro`) + **3b `schema_sync.py` (read-only) → "schema in sync — no missing columns"** (none of the 35 added a missing column; `--apply` not run — agent blind-apply is permission-gated) + `config.yaml` byte-identical (gitignored; qwen/Bailian token-plan intact). **3c real-chat smoke DONE** — logged in (`ts_team@sipmm.edu.sg`) + sent a chat → model (DeepSeek V4 Pro) replied "ok"; `runs/stream` + `messages` + **`token-usage`** (the 2026-06-21 incident endpoint) all **200, zero 5xx** — run/persist path verified end-to-end. Rollback branch `rollback-pre-sync-20260623` deleted post-verify (pre-sync `cd34f7a0` still recoverable as `e562f8b3^`/reflog). **Notable upstream content:** security middleware (input sanitization #3662, system-context role isolation #3661), OIDC SSO #3506, GroundRoute web search/fetch #3675, TokenBudgetMiddleware #3412, perf (#3688 config index O(1), #3700 SSE resume O(1), #3687 subagent dedup), + many fixes. Pre-sync state `cd34f7a0` (rollback branch `rollback-pre-sync-20260623`, delete post-verify). `main` ff to `cefc53c7` separately (default-branch push needs auth).

Earlier 2026-06-21 sync absorbed 4 commits cleanly
(merge-tree clean, exit 0, zero conflicts; merge `f3621bc8` → `5ddf6988`, merge commit `0c268a7d`). **First sync run under the hardened workflow** (steps 3b/3c added after the 2026-06-21 `runs.token_usage_by_model` incident): rebuilt `make up`, then **`schema_sync.py --apply` → "schema in sync — no missing columns"** (confirms none of these 4 added a column) + run-persist round-trip smoke OK + gateway "Application startup complete" + app 401 + `config.yaml` byte-identical (qwen/Bailian intact) + version `2.1.0`. Commits: `#3673` frontend new-chat-reset-on-nav (minor), `#3663` persist AI turn duration (backend — **derives it from existing `runs.created_at/updated_at`, NO new column**), `#3585` fastCRW provider (inert — we run qwen/Bailian), `#3304` docs cleanup. No config/.env/schema change. Pre-sync state `5e75178e` (rollback branch `presync-backup-0621`, delete post-verify). `main` ff to `5ddf6988` separately (default-branch push needs auth).

Earlier 2026-06-20 sync absorbed 23 commits cleanly
(merge-tree + throwaway-worktree dry-run verified, exit 0, zero conflicts; merge `f3621bc8` → merge commit `2ee96e7b`). **This is the upstream 2.0.0 release** — but for us it's a routine sync, NOT a scary major upgrade: `local-fixes` already runs the 2.0 codebase (identical layout: `backend/packages/harness/`, `frontend/`, `contracts/`), so the `0.1.0 → 2.1.0` jump is just the release version-string flip (`#3603` is only a CHANGELOG + version bump). The **single** documented 2.0.0 ⚠ breaking change — `#2932` (runs hydrate from `RunStore`; cross-worker cancels now return 409) — is commit `88759015`, **already an ancestor of `local-fixes`** (predates this sync), so this sync introduces **zero new breaking changes**. **No config.yaml/.env/schema/Makefile change** (only a local-dev `nginx.local.conf` tweak); the gateway rebuild ships the dep bumps. Content is frontend-dominated (chat/workspace UI, threads hooks, +new e2e/unit tests) + 2 features + safe backend fixes/perf.

- **3 dependency bumps** (`backend/uv.lock`): **cryptography →≥48.0.1** (#3666, constraint match to lockfile), **pydantic-settings 2.14.0→2.14.2** (#3670), **langsmith 0.8.0→0.8.18** (#3669). Prebuilt wheels; shipped on the gateway rebuild.
- **Features:** `#3637` regenerate-latest-answer; `#3627` frontend "(thought for Xs)" thinking-duration indicator; `#3599`/`#3591` make AI follow-up suggestions optional (frontend stops fetching when disabled).
- **Backend fixes/perf (beneficial, inert-risk):** `#3658` attribute token usage to the actual model; `#3597` make stdio-MCP-produced files resolvable via virtual sandbox paths; `#3654` cache `Base.to_dict` column reflection; `#3647/#3648` cache LocalSandbox path-rewrite regexes; `#3657` faster `should_ignore_name` in glob/grep; `#3590` `make dev` works on non-root/NFS hosts; `#3631` strip base64 image data from streamed values events.
- **Verified after `make up` (compose up --build -d → builds new images while old containers serve, then recreates the two changed ones — minimal downtime):** gateway clean startup ("Application startup complete", Uvicorn on `:8001`, AsyncPostgresStore connected, no errors/tracebacks); **`config.yaml` byte-identical** (54026 bytes, mtime unchanged → qwen/Bailian model config intact); deployed **version = `2.1.0`** live in the gateway image; front door `https://app.worldresearch.org/` → **401** (auth challenge = stack serving, not 502/503); all 3 containers Up (gateway+frontend recreated, nginx unchanged). Rollback marker `presync-backup` @ `13d7cbc0` (deleted post-verify).

Earlier 2026-06-18 sync absorbed 25 commits cleanly
(merge-tree verified, exit 0, zero conflicts; merge `6044e5c5` → merge commit `8f8f0dcc`). **No config.yaml/.env/schema change; the gateway rebuild ships the backend dep bumps.** Mostly IM-channel hardening (inert here) + dependency maintenance + sandbox fixes.

- **5 dependency bumps** (`backend/uv.lock`): **cryptography 46.0.7→48.0.1**, **aiohttp 3.14.0→3.14.1**, **starlette 1.0.1→1.3.1**, **pyjwt 2.12.1→2.13.0**, **python-multipart 0.0.27→0.0.31**. Security/maintenance; prebuilt cp312 wheels (no C build). Ship on the gateway rebuild. ⚠️ starlette 1.0→1.3 is a minor-series jump — boot-verify after redeploy.
- **Sandbox fixes (we use aio):** `97dd9ecf` stop flagging string-literal path fragments as unsafe absolute paths; `6a4a30fa` actionable hint when `read_file` hits a binary file; `f212da9f` create shell session before retrying on a fresh id; `5851f825` make `setup-sandbox.sh` executable (now `100755`). Backend-only, beneficial.
- `05be7ea6` **fix(subagents): raise general-purpose max_turns to 150 + default timeout 30min (#3610)** — subagent behavior change; our LGI Stage-1 runs with subagents OFF (`--subagents` default off) so inert there, but applies to any subagent use.
- `c81ab268` stop stripping `__interrupt__` from channel values; `a72af8ea` Langfuse subagent-span attribution; `1896722e` MCP tools-cache-reset endpoint — backend resilience/observability, beneficial or inert.
- **Inert / no-op for us:** the IM-channel hardening batch (`525af0da` `2b301e82` `68ba4198` `8c0830ae` `e732a741` `926406e0` `43dba448` `0966131b` `ec16b665` — IM channels unconfigured); `0bbbbc06` Serper Google-Images `image_search` provider (we use Tavily/Jina); `65fab1d4` maintainer-orchestrator skill + `6044e5c5` bug-report.yml (docs/CI only).

Earlier 2026-06-15 sync absorbed 15 commits cleanly
(15-agent parallel behavioral triage + merge-tree sim; merge `d2cc991d`, exit 0, zero conflicts). **One deploy action — redeploy the gateway to apply the CLI-auth-mount security fix (`474c89ba`); no config.yaml/.env/schema change.** Upstream also cut a **`2.0.0-release` branch + tag `v2.0.0-rc0`** — a 2.0 release candidate is incoming; review before the next deep sync.

- `474c89ba` **fix(security): do not bind-mount host CLI auth dirs by default (#3521)** — drops the default `~/.claude` + `~/.codex` gateway bind-mounts (moved to an opt-in `docker/docker-compose.cli-auth.yaml` overlay). **ACTION: redeploy** (`scripts/deploy.sh`) — the live gateway (created 2026-06-13) still mounts our real `~/.claude`/`~/.codex` (credentials + shell history) into the sandbox container; we authenticate by API key (`ANTHROPIC_API_KEY`; no CLI-login provider, no `acp_agents`) so the mounts are dead weight → dropping them is a pure security win. `deploy.sh` will NOT add the cli-auth overlay (we don't use it). No `.env`/config change.
- `5d61718c` **fix(security): mount host Docker socket only in aio (DooD) sandbox mode (#3517)** — the `/var/run/docker.sock` mount is now gated on aio mode instead of mounted unconditionally. **Behavior-preserving for us:** config uses `AioSandboxProvider` (no `provisioner_url`) → `detect_sandbox_mode()=aio` → `deploy.sh`/`docker.sh` auto-append the new `docker/docker-compose.dood.yaml` overlay that re-adds the socket. Redeploy keeps the socket exactly as today. No action.
- `47e9570d` fix(subagent): isolate subagent from parent run checkpointer (#3559) — **real fix for us** (`checkpointer.type=postgres`): a subagent was inheriting the parent run's *sync* Postgres checkpointer via `copy_context()`, crashing the async path (`NotImplementedError` from `aget_tuple()`). 1-line `checkpointer=False` + test; subagents are one-shot (never resume) so no persistence lost. Backend-only.
- `8955b322` fix(sandbox): merge idempotent sandbox state updates (#3518) — new LangGraph `merge_sandbox` reducer so concurrent sandbox tools emitting the same `sandbox_id` in one step don't trip `INVALID_CONCURRENT_GRAPH_UPDATE` (fails closed on genuinely conflicting ids). Backend-only, beneficial.
- `25fbd25b` fix(frontend): cap deeply nested list indentation (#3570) — clamps >200-col leading whitespace before `marked` lexes it, preventing a chat-route render crash on pathological nested lists. **Applies to us** (every user hits the Streamdown renderer). Ships on the frontend rebuild.
- `34e126ee` fix(frontend): reset active chat after deletion (#3519) — deleting the open thread now resets to a blank `/chats/new` (and 404/403 thread URLs redirect to blank) instead of a stale/broken view. Frontend-only, no new endpoint. Applies.
- `f43aa781` fix(agents): sync `agent_name` across context/configurable + reject empty SOUL (#3553) — runtime fix for the custom-agent/`setup_agent` (per-agent SOUL.md) path under LangGraph ≥1.1.9; rejects empty/whitespace SOUL before any FS write. Backend-only, beneficial if we use custom agents.
- `d2cc991d` feat: make AI follow-up suggestion chips optional (#3591) — new `SuggestionsConfig` (defaults **on** via `default_factory`), `config_version 12→13`, backward-compatible; our v9 config keeps chips on. Inert — optionally set `suggestions.enabled: false` to hide them.
- **Inert / no-op for us:** `6e839342` Brave Search web-search tool (we use Tavily); `1783da42` Discord fd-leak fix + `c91dacc8` WeCom WS-failure logging (IM channels unconfigured); `ec520e64` makefile pre-commit-hook ergonomics (local dev only); `0fb2a75b` docker-config doc relocation + `554017a8` custom-AIO-image guide + `d23eac22` maintainer-SOP skill (docs only).

**Aside (pre-existing, surfaced by triage):** the live gateway also mounts `/var/run/docker.sock` (host-root-equivalent) — required for aio/DooD sandboxing and acceptable under single-operator basic-auth, but revisit if the instance ever ingests untrusted input. The `474c89ba` redeploy drops the credential mounts; the socket stays (via the dood overlay).

Earlier 2026-06-13 sync absorbed 7 commits cleanly
(4-agent parallel behavioral triage + merge-tree sim; merge `66e4b144`, exit 0, zero conflicts). **No config.yaml/.env action.**

- `09429644` fix(history): strip base64 image data from REST endpoint responses (#3535) — **beneficial behavioral fix.** New `runtime/serialization.py` helpers strip `data:` base64 `image_url` blocks **only from `hide_from_ui` messages** (ViewImageMiddleware internal model-context) in 6 REST endpoints incl. `get_thread_history`. **No rendering regression** — the frontend already drops `hide_from_ui` messages before render; user-uploaded/visible images are untouched. **Persisted checkpoints are NOT modified** (operates on a serialized copy), so existing image threads stay replayable. Fixes a UI freeze on image-bearing threads. LGI/embedded path unaffected. No config change.
- `3475f7cd` / `83bc2fb1` chore(deps): bump **starlette 1.0.0→1.0.1** (pure-python patch) + **aiohttp 3.13.5→3.14.0** (transitive) in `backend/uv.lock`. aiohttp is pulled in only by IM-channel SDKs + firecrawl (we use neither — our fetch path is httpx); **zero direct imports in the repo**. Ships a prebuilt cp312 wheel → no C build on `python:3.12-slim`. Verified live: running gateway reports `aiohttp 3.14.0 | starlette 1.0.1`, clean boot. No `pyproject.toml` change.
- `a17d2ff8` fix(mcp): surface admin-required state on the settings Tools page (#3533) — frontend resilience fix; the MCP-config admin gate (403 for non-admin) is **pre-existing backend behavior**, this just renders it instead of failing silently. We're single-operator-admin → page renders normally. Touches `tool-settings-page.tsx`, `core/mcp/api.ts/hooks.ts` + i18n (en/zh/types). No schema change.
- `839fa992` feat(telegram): stream replies by editing the placeholder message; `420a886e` fix(channels): offload inbound file-IO — **both channels-only → inert** (no IM channels). Touch `channels/manager.py`/`telegram.py` only.
- `cad6e89a` fix(scripts): `make stop` can't stop next-server — `scripts/serve.sh`, **dev-tooling only** (our prod path is `docker compose`; serve.sh never runs in-container). New `DEERFLOW_DAEMON_ROOT` is an internal serve.sh var, never set in our deploy.

Earlier 2026-06-12 sync absorbed 9 commits
(5-agent parallel behavioral triage + merge-tree sim; merge `d1a58aad`, **one real conflict resolved**) —
the big **user-owned IM channels** feature (inert for us) + an **authz hardening** + runtime perf. **No config.yaml/.env action.**

- `aa015462` feat(im): user-owned IM channel connections (#3487) — **96 files (+8588), but inert for us (NO IM channels configured) — and a net SECURITY hardening.** (1) **Internal-token authz rescoping:** internal-token callers were previously *fully exempt* from the stateless-run thread-ownership guard; now they're scoped to the owner carried in a new `X-DeerFlow-Owner-User-Id` header (`get_trusted_internal_owner_user_id`, honoured ONLY when `system_role == "internal"`). Every real better-auth request is `user`/`admin` (never `internal`), so the header is **ignored for all our traffic** and the prior `check_access` cross-user 404 guard is preserved verbatim — strictly tighter on the internal path, unchanged for us. **LGI batch unaffected** (embedded client bypasses HTTP/internal-token entirely). (2) `POST/DELETE /api/channels/{provider}/runtime-config` are now **admin-gated** (mirrors the MCP-config model); read-only GETs stay open. (3) `auth_disabled.py` `e2e-user`→`default` rename — dead-path (we're `DEER_FLOW_ENV=production`). **Startup side effects (benign, verified):** `start_channel_service()` runs every boot and logs all 7 providers `enabled:false/running:false` (inert idle service); **4 new ORM tables auto-create empty** in Postgres (`channel_connections`, `channel_credentials`, `channel_oauth_states`, `channel_conversations`) via idempotent `create_all` — no migration, no data. Adds an optional `channel_connections` config block (we leave it absent → disabled). **See new gotcha #21.**
- `76136d22` fix(channels): reload config.yaml on channel restart (#3514) — channels-only; `restart_channel()` re-reads the channel's config entry, `to_thread`-offloaded. Inert with no channels.
- `0d3bfe0a` perf(runtime): index runs by thread_id in RunManager (#3499) — **our per-worker run-manager path**; pure secondary-index optimisation (lockstep with the source dict), behavior-preserving (run lifecycle/ordering unchanged). Benign speedup.
- `579e4164` perf(runtime): index messages in `MemoryRunEventStore` (#3531) — **not our path** (we run `run_events.backend: db` → `DbRunEventStore`); the indexed store isn't even instantiated. Inert.
- `503eeac7` fix(frontend): render user messages as **plain text** + cap blockquote nesting (#3502) — **operator-facing UI change:** human messages now render verbatim (whitespace-pre-wrap, no markdown) instead of through the markdown renderer. Self-contained render-layer change; no rebrand collision.
- `b8f5ed36` fix(skills): keep skill archive installation off the event loop (#3505) — `to_thread` the skill-install IO; our skills map is empty → benign.
- `c002596a` chore(todo): remove an unused completion-reminder counter — no behavior change. `a838546a` blocking-io detector CLI shim — dev/CI-only. `bbce6c0a` docs(config): SearXNG/Browserless config examples — `config.example.yaml` only (bumps **example** version 12→13; live config is v9, warning-only/non-fatal — already fires; do NOT enable the example blocks).
- **The conflict — `settings-dialog.tsx`:** aa015462 adds a Channels settings tab (`CableIcon` + `ChannelsSettingsPage` import, `channels` section + render), which collided with our hide-About-tab patch (`72a02078`, which had removed the `InfoIcon` import + About sidebar entry). **Resolved:** keep all upstream Channels additions, keep our About removal (drop `InfoIcon`, no About sidebar entry; the unreachable `about` render + `AboutSettingsPage` import stay harmless). i18n `t.settings.sections.channels` shipped with the commit (en/zh/types). Frontend rebuilt + typechecked clean on deploy; verified live (auth 403, frontend 200, channels all `enabled:false`).

Earlier 2026-06-11 (later) sync absorbed 4 commits
(5-agent parallel behavioral triage + merge-tree sim; tree `201254b1`, **one real conflict resolved**) —
a **partial FIX for gotcha #14** plus the WRI-rebrand offline-banner merge:

- `f401e7ba` [codex] Fix stale AIO sandbox cache reuse (#3494) — **partially fixes gotcha #14** (see the updated gotcha #14 above). Adds an `is_alive` `docker inspect` health-check to the sandbox **acquire** paths (`_reuse_in_process_sandbox` + `_reclaim_warm_pool_sandbox`): a dead container is now evicted+destroyed+recreated, so **Window A (cross-run staleness) self-heals — no more manual gateway restart between runs**. **Window B (mid-run death) is NOT fixed** — the hot tool path (`provider.get()`) stays health-check-free for event-loop safety, so a container dying mid-run still hangs that run ~120 s. Works for us via the docker.sock DooD mount; `to_thread`-offloaded; `remote_backend.py` change inert (we run `LocalContainerBackend`). Beneficial for the LGI batch (sequential thread-reusing runs self-heal between runs). No config/.env change.
- `919d8bc2` fix(sandbox): persist lazily-acquired sandbox state via Command (#3464) — additive `wrap_tool_call`/`awrap_tool_call` on `SandboxMiddleware` that commit a lazily-acquired `sandbox_id` to LangGraph graph state (was an in-invocation `runtime.state` mutation invisible to the channel reducer). **Does NOT change acquire timing** (still lazy on first tool call) and **does NOT fix gotcha #14** (provider-level stale-handle issue). Mildly beneficial: sub-agents (`task_tool`) reliably see the parent's sandbox id. Applies to both gateway + embedded LGI path. No config change.
- `c733d3c9` fix(frontend): isolate new chat thread messages (#3508) — frontend-only; gates message rendering by `currentViewThreadId` so a new chat no longer shows the prior thread's messages after client-side `replaceState`. Disjoint from WRI rebrand files; clean merge. No batch interaction.
- `b6fbf0d1` fix(frontend): keep workspace interactive when SSR auth probe can't reach gateway (#3493) — **the conflict.** Replaces the dead-static-HTML `gateway_unavailable` fallback with `<GatewayOfflineFallback renderBanner>` (real AuthProvider + a client banner that silently re-probes `/api/v1/auth/me` every 10 s, applies the recovered user directly, reentrancy-guarded, 3-consecutive-401 → `/login`). **Conflicted with our WRI rebrand of that block in `(auth)/layout.tsx`.** Resolved by adopting upstream's `GatewayOfflineFallback` wrapper + import, grafting our WRI styling (gradient/maroon/heading) onto the inner div, and **dropping the now-redundant manual Retry `<Link>`** (banner auto-re-probes). The auto-added i18n keys (`gatewayUnavailable*`) merged in cleanly across en/zh/types. **Nice side benefit: a `/workspace` tab now stays interactive and the banner auto-clears within ~10 s during a gateway bounce** — exactly the gotcha-14 / redeploy restart window. Frontend rebuilt + typechecked clean on deploy; verified live (auth 403, frontend 200, sandbox-fix code present in running gateway).

Earlier 2026-06-11 sync absorbed 5 commits cleanly
(5-agent parallel behavioral triage + merge-tree sim; tree `11498269`, exit 0, zero conflicts;
`prompt.py` auto-merged — our `<language>` block (L369) + upstream's `use_tiktoken` kwarg (L596)
both preserved). **NONE required any config.yaml/.env action.**

- `ba9cc5e9` fix(gateway): enforce thread ownership on stateless run endpoints (#3473) — **the one to watch**, since the LGI batch drives stateless runs. Adds a `check_access(thread_id, user.id)` guard in `start_run()` (services.py, +15 lines) that 404s a run on **another user's** thread (cross-user IDOR fix). **Safe for us:** `check_access` returns True for own/NULL-owner/missing-row threads; the batch creates threads AND starts runs under the **same operator identity**, so it always passes. The only reject case — a thread owned by a *different* user — cannot occur in a single-user instance. **Invariant to preserve:** never reconfigure the batch to run unauthenticated or under a different user id than the one that created the threads, or foreign-owner 404s appear. Verified live: auth still 403 unauthenticated, guard present in running `services.py`. The next LGI Stage-1 batch exercises this guard naturally (in-process embedded client, operator identity).
- `167ef451` feat(memory): add `memory.token_counting` config (`tiktoken`|`char`) to avoid tiktoken's network BPE download; hardens default tiktoken path (LOADING sentinel + cached-failure cooldown); bumps `config_version` 11→12 — **dead-path for us** (`injection_enabled: false` short-circuits `_get_memory_context` before token counting runs). `token_counting` defaults to `"tiktoken"` and `AppConfig` is `extra="allow"`, so omitting it from our `config.yaml` is valid (no loading break). `config_version` check is **warning-only** (we already trip it at v9; v12 changes nothing). Optional: set `memory.token_counting: char` only if injection is ever enabled under network restriction.
- `b3c2cc42` fix(agents): require `config.yaml` in `resolve_agent_dir` to skip memory-only dirs (#3390) — **inert + protective.** `resolve_agent_dir` is only reached via a non-None `agent_name`; the LGI batch and embedded `DeerFlowClient` never set `agent_name` (default lead_agent → `load_agent_config(None)` short-circuits). Zero custom-agent dirs on disk. No-op today, exactly the fix we'd want if a custom agent + memory were ever introduced.
- `5819bd8a` fix(frontend): paginate workspace chat list beyond 50 threads (#3482) — **frontend-only, cosmetic.** Swaps `useThreads()` → `useInfiniteThreads()` (TanStack + IntersectionObserver). Backend `/threads/search` already owner-scoped via contextvar; only observable effect is the operator scrolling past 50 chats. 3 net-new generic i18n keys (no WRI-rebrand collision). No batch interaction.
- `2d5f0787` Update lint-check.yml — CI lint-workflow tweak, no runtime impact.

Earlier 2026-06-10 (latest) sync absorbed 1 commit cleanly
(2-agent triage; tree `da238bd6`, zero conflicts) — **a latent-bug FIX for us, not just an absorb**:

- `05ae4467` fix(docker): default Gateway to a single worker (#3475) — changes the compose default `${GATEWAY_WORKERS:-4}` → `${GATEWAY_WORKERS:-1}`. **We rode the `:-4` default = 4 workers, which silently broke run-cancellation + SSE-reconnect** (run state is per-worker in-process, nginx has no sticky sessions, no shared cross-worker bridge → ~75% of cancel/reconnect requests hit the wrong worker → HTTP 409 + heartbeat-only SSE hangs). Merging dropped us to **1 worker**, eliminating the race — verified live: `--workers 1`, gateway memory ~662 MB → ~175 MB, auth still 401, clean boot. Our 3 compose edits (`stop_grace_period`, `NO_PROXY`, the new `:-1`) all coexist. **See new gotcha #20** — keep `GATEWAY_WORKERS` unset/1 until upstream's shared stream bridge (#3191) lands. The LGI batch is unaffected (`docker exec` path). README + a regression test (`test_compose_default_workers.py`) came with it.

Earlier 2026-06-10 (later) sync absorbed 1 commit (auth-sensitive — full 3-agent
security triage before merge; clean, tree `23c74bea`) **+ 1 local security hardening**:

- `2b795265` fix: align auth-disabled mode and mock history loading (#3471) — adds an **auth-disabled dev/e2e mode** (anonymous requests run as a synthetic admin `id=e2e-user`, `system_role=admin`) gated behind env `DEER_FLOW_AUTH_DISABLED=1` AND a production kill-switch. **We are auth-ENABLED and do NOT set that flag**, so all new branches are dead code for us — verified at runtime: `is_auth_disabled()` returns `False` and unauthenticated `/api/v1/auth/me` still returns 401. Adversarial review confirmed `auth_middleware.py` is a pure branch-order refactor preserving the enabled-path contract byte-for-byte (junk/expired cookie→401, no-cookie→401, internal-token preserved); `csrf`/`langgraph_auth`/`routers/auth` changes are gated no-ops when auth is enabled. One benign enabled-path delta: `deps.py` now short-circuits to `request.state.user` within a single request (dedups a redundant JWT-decode+DB-lookup; result-equivalent for sessions, no spoofing vector — only AuthMiddleware writes that state). Frontend `hooks.ts` (+55) is gated by `?mock=true`; real authenticated thread-history loading unchanged. Clean merge, no local-patch overlap.
- **LOCAL HARDENING (`.env`):** set **`DEER_FLOW_ENV=production`**. This arms the upstream production kill-switch (`is_explicit_production_environment()`), so even if `DEER_FLOW_AUTH_DISABLED=1` were ever set on this box (accidentally or maliciously), auth-disabled mode + the synthetic-admin backdoor **cannot activate**. Verified safe before applying: `DEER_FLOW_ENV` is read in exactly 3 non-test places — the auth-disabled guard (intended) and two `inject_langfuse_metadata(environment=...)` trace-label calls (benign; just tags Langfuse traces `environment=production`, no behavior/feature/logging switch). Defense-in-depth only — auth-disabled was already off (flag absent). **Keep `DEER_FLOW_AUTH_DISABLED` absent and `DEER_FLOW_ENV=production` present.**

Earlier 2026-06-10 sync absorbed 7 commits cleanly
(4-dimension triage + merge-tree sim; tree `657641d8`, zero conflicts; `prompt.py` auto-merged —
our `<language>` block + upstream's slash-skill section both preserved):

- `16391e35` fix(skills): harden slash skill activation across chat channels (#3466) — **one all-entrypoints behavior change worth knowing.** Adds `SkillActivationMiddleware` to EVERY lead-agent turn (incl. our web UI). **Inert unless a user message starts with `/skill-name`** (strict regex; reserved words `help`/`memory`/`models`/`new`/`status`/`bootstrap` excluded). When triggered it injects the matching `SKILL.md` before the model call; since our `extensions_config.json` skills map is empty, a `/foo`-prefixed message returns "skill not found/disabled" instead of normal chat. **Normal non-slash chat is unaffected.** The channel-handler changes (slack/telegram/discord/feishu/etc) are inert (no channels configured).
- `a57d05fe` fix(runtime): journal run lifecycle events (#3470) — fixes spurious per-node `run.end` events; only the root run now journals one `run.end` (adds the symmetric `parent_run_id` guard to `on_chain_end`). Cleans up our db-backed event stream. Authoritative run status (`worker.py set_status`) + the `268fdd69` drain unaffected.
- `b62c5a7b` fix(agents): offload blocking FS IO in custom-agent router off the event loop (#3457) — wraps create/delete agent filesystem IO in `asyncio.to_thread`. We mount this router; same API outcomes (201/409/500), closes a small TOCTOU window. Benign responsiveness improvement.
- `ae9e8bc0` fix(sandbox): missing `sandbox.mounts` host_path → loud ERROR (#3244) — **triple no-op for us:** just a `warning`→`error` log escalation (NOT a raise; skip-and-continue preserved, wrapped in a swallowing try/except), only in `LocalSandboxProvider` (we run `AioSandboxProvider`→`LocalContainerBackend`), and our `sandbox.mounts` is commented out. No startup risk.
- `5b81588b` fix(frontend): fallback Streamdown clipboard copy (#3397) — touches `about-settings-page.tsx`/`memory-settings-page.tsx` but **no rebrand collision** (our About rewrite `b19d83cf` touched `about-content.ts`/`about.md`, different files); both settings pages had no local edits → adopt upstream cleanly.
- `18bbb82f` Fix 'make dev' on Windows (#3236) — adds `backend/sitecustomize.py`, which **does auto-import in our prod** (backend/ on sys.path via `cd backend && PYTHONPATH=.`) but is **a verified Linux no-op** (only action is `if sys.platform != "win32": return`). serve.sh/wait-for-port.sh are dev-tooling.
- `63ce88f8` fix(replay-e2e): key fixtures by caller (#3453) — test/CI/docs only.

Earlier 2026-06-09 sync absorbed 5 commits cleanly
(2-agent verify + merge-tree sim; tree `8cd654eb`, zero conflicts; no local-patch overlap):

- `8db16bb3` fix(config): coerce null config.yaml list sections to empty list (#3434) — `app_config.py` `field_validator(mode="before")` coerces `None`→`[]` for `models`/`tools`/`tool_groups` + warns when no models configured. **No-op for our `config.yaml`** (well-formed lists: models=5, tools=10, tool_groups=4 — verified booting in the running container). Backward-compatible robustness fix, no new required config.
- `0fb18e36` refactor(lead-agent): make `build_middlewares` public (#3458) — pure mechanical rename `_build_middlewares`→`build_middlewares` (drops a cross-module private import) + docstring. **Middleware list/order unchanged**; no behavior change. (Frontend `middlewares.mdx` 1-line docstring rename, no rebrand collision.)
- `90e23bfd` fix(ci): consolidate PR/issue labeling into `triage.yml` (#3455) — `.github`-only; removes the labeler/issue-triage/pr-labeler/pr-triage workflows we synced in `aca7acc1` (upstream-tracked, no local edits) → single `triage.yml`. No image impact/redeploy. Dormant for our ops (fires on `pull_request_target`/issues, not `local-fixes` push).
- `37337b77` feat(models): StepFun reasoning model adapter (#3461) — purely additive (`patched_stepfun.py` + commented `config.example.yaml`/`.env.example`). We don't use StepFun; `config.yaml` untouched. Inert.
- `93e3281c` fix(dev): create backend/sandbox before uvicorn reload-exclude (#3459) — dev-mode `uvicorn --reload` only; our prod gateway has no `--reload`. No-op for prod.

No local-patch overlap (none of the 5 touch Dockerfile, docker-compose.yaml, prompt.py, frontend rebrand, or config.yaml). Lowest-impact batch in a while — mostly robustness/refactor/CI/additive.

Earlier 2026-06-08 (later) sync absorbed 8 commits cleanly
(4-dimension triage + merge-tree sim; tree `975be3d2`, zero conflicts; **both local-patched
files auto-merged** — `docker-compose.yaml` and `lead_agent/prompt.py`, see below):

- `3c2b60aa` fix(threads): assign new checkpoint ID in update_thread_state (#2391) — **genuine correctness fix.** `update_thread_state` was re-`aput`ing with the *same* checkpoint id read from the latest snapshot → Postgres `ON CONFLICT DO UPDATE` **replaced** the row instead of appending, so thread-state history never grew (broke history/rewind/fork). Fix assigns a fresh time-ordered `uuid6`. Verified against the installed `langgraph-checkpoint-postgres` UPSERT SQL + the new regression test (23/23 pass). Composes with our `AsyncConnectionPool`. User-visible: editing thread state mid-conversation now appends a checkpoint instead of clobbering the latest.
- `f92a26d5` fix(web_fetch): proxy for Jina reader in restricted networks (#3418) — opt-in proxy on DeerFlow's Jina `web_fetch`; **inert when unset** (our case): `trust_env=True` equals httpx's existing default → our web_fetch is byte-identical. **LGI Stage 2 unaffected** (`stage2_verify.py` builds its own httpx client, doesn't import DeerFlow's JinaClient). Adds `NO_PROXY`/`no_proxy` env to the gateway compose service (safe `${NO_PROXY:-}` → empty when unset). **This is the second edit to `docker/docker-compose.yaml` — it auto-merged with our `stop_grace_period` patch; both present** (NO_PROXY at L112-113 in the gateway `environment:`, `stop_grace_period: 30s` at L127).
- `cd5bedaa` feat: MiniMax provider for image/video/podcast skills + music-generation (#3437) — MiniMax models/skills opt-in & unconfigured here → inert. **One all-deployments UX change:** `view_image_middleware` now tags the injected image-context message `hide_from_ui: True` — **the model still receives the full images**; it only stops the "Here are the images you've viewed:" bubble from rendering in the chat UI. Benign.
- `3b105d1e` fix(suggestions): strip inline `<think>` before parsing follow-ups (#3435) — defensive; **not a fix we need** (MiMo/DeepSeek emit reasoning in a separate `reasoning_content` field, not inline `<think>`; default suggestions model is deepseek with thinking off). No required config.
- `3b6dd0a4` feat(subagents): extend deferred MCP tool loading to subagents (#3432) — **inert** (gated behind `tool_search.enabled` = FALSE → no-op everywhere). The `prompt.py` edit is a function *relocation* (`get_deferred_tools_prompt_section` moved to `tools/builtins/tool_search.py`); **auto-merged with our `<language>` block, both preserved.**
- `67ad6e23` fix(dev): exclude runtime state from gateway reload (#3426) — dev-mode `uvicorn --reload` watcher only; our prod gateway has no `--reload`. No-op for prod.
- `799bef6d` fix(replay-e2e): match by conversation (#3436) — test/CI infra only (follow-up to `88759015`).

**Both local patches survived the merge** with no manual resolution: `docker-compose.yaml` (our `stop_grace_period` + upstream's `NO_PROXY`) and `prompt.py` (our `<language>` block + upstream's relocation). The compose `NO_PROXY` is harmless on our host (var unset → expands to just the internal-hostnames exemption list).

Earlier 2026-06-08 sync absorbed 7 commits cleanly
(4-dimension triage + merge-tree sim; tree `3e44f618`, zero conflicts; no fork divergence —
every touched non-test file was byte-identical to merge-base):

- `40a371b8` fix(security): harden MCP config endpoint (#3425) — **security improvement.** Adds an **admin-auth gate** (`system_role == "admin"`) on BOTH GET and PUT `/api/mcp/config`, plus a PUT-side stdio command allowlist (default `{npx, uvx}`, env-extensible via `DEER_FLOW_MCP_STDIO_COMMAND_ALLOWLIST`) that rejects path-separators/whitespace/shell-metachars. Secret-masking was already present (#2667). For our secret-less stdio Playwright (`env: {}`): GET masking is a no-op, PUT allowlist accepts `npx`. **The MCP config endpoint is now admin-only** — no practical impact (the WRI frontend never calls it, and we hand-maintain config files), but note it if any automation reads `/api/mcp/config` as a non-admin (would now 403).
- `f725a963` fix(runtime): protect sync singleton init and reset (#3413) — adds `threading.Lock` around the **sync** checkpointer/store singleton accessors (double-checked locking, fixes a double-init/leaked-CM race). Our production runs the **async** `AsyncConnectionPool` path on `app.state` (from `031d6fbc`), which this does NOT touch; composes cleanly with the `268fdd69` shutdown drain (separate singletons). No new config. Latent/defensive for us.
- `51920072` fix(middleware): offload memory injection off event loop / tiktoken blocking (#3402) — offloads tiktoken token-counting via `asyncio.to_thread` (5s bounded) + a startup `warm_tiktoken_cache()`; fixes a real cold-BPE-download event-loop-block risk (~26 min stall in network-restricted envs). **No memory content change.** Mostly latent for us (`memory.injection_enabled: false`), but the startup warm-up + date-injection offload still apply.
- `64d923b0` fix(middleware): externalize oversized tool output for non-mounted sandboxes (#3417) — `ToolOutputBudgetMiddleware` IS enabled here (defaults; no `tool_output:` section), but our sandbox is **MOUNTED** (`AioSandboxProvider` → `LocalContainerBackend`, no `provisioner_url`), so the new non-mounted branch never fires — existing host-disk behavior unchanged.
- `10c1d9f4` fix(search): DDGS Wikipedia region (#3423) — **no effect:** we use **Tavily** for web_search (DDG commented out); `image_search` uses a separate `ddgs.images()` path the fix doesn't touch.
- `3b4c9ff7` fix(setup): LLM provider wizard defaults (#3421) — setup-wizard-only; inert (we hand-maintain `config.yaml`).
- `88759015` test(e2e): record/replay front-back contract (#3365) — **test/CI infra only.** Adds `.github/workflows/replay-e2e.yml`, but it triggers on push to **`main`** + PRs (not `local-fixes`), so it **won't activate** on our normal ops push; no secrets required. Would only run if pushed onto `origin/main`.

Earlier 2026-06-07 (later) sync absorbed 6 commits cleanly
(4-dimension triage + merge-tree sim; tree `2e95ab85`, zero conflicts; one both-sides
file `lead_agent/prompt.py` auto-merged — kept our `<language>` block + upstream's edit):

- `d8b728f7` fix(mcp): close stdio sessions on their owning loop to avoid cross-task cancel-scope error (#3379) — **fixes a bug our Playwright-stdio deployment can actually hit.** The embedded sync-tool path runs each MCP call via `asyncio.run` (fresh loop/task per call); session eviction then awaited the anyio `__aexit__` on a *different* task than opened it → `RuntimeError: Attempted to exit cancel scope in a different task`. +397 in `mcp/session_pool.py`. Backward-compatible for our single stdio server, hot-path latency unchanged, doesn't alter gotcha #19 (HTTP/SSE still unpooled), composes with the shutdown drain.
- `d133b111` fix(summarization): tag summary LLM calls nostream (#2503) — stops phantom/duplicate streamed summary messages from our **enabled** summarization middleware. Model-agnostic (LangGraph `TAG_NOSTREAM` streaming tag, not a model API flag). **Does NOT change our 32K summarization trigger.**
- `88e36d96` fix: prevent write_file streaming timeout on long reports (#3195) — **one behavior change worth knowing:** adds a **default-on 80 KB cap on single non-append `write_file` tool calls** (`sandbox/tools.py:55`, env-overridable via `DEERFLOW_WRITE_FILE_MAX_BYTES`, 0 disables). Oversized single-shot writes get a structured rejection steering the model to `append=True`/`str_replace`; lead-agent + general-purpose prompts updated to teach this. **The LGI pipeline is UNAFFECTED** — its stage scripts write via direct Python FS (`report.md` 208 KB / `data_tables.json` 456 KB bypass the agent tool). Also adds a 240s `stream_chunk_timeout` default — **only for `langchain_openai:ChatOpenAI`-path models** (qwen3.6-plus/qwen3.7-max/kimi); DeepSeek/MiMo stay on langchain's 60s unless `stream_chunk_timeout` is added to their `config.yaml` entry. Caps `StreamChunkTimeoutError` retries at 1.
- `befe334f` fix(config): make the reload boundary discoverable from code (#3144) — introspection/docs only (new `config/reload_boundary.py` registry; `STARTUP_ONLY_FIELDS` = database/checkpointer/run_events/stream_bridge/sandbox/log_level/channels). **Does NOT change which keys hot-reload** — our `config.yaml`/`extensions_config.json` mtime reload is untouched.
- `8d2e55a0` fix(subagent): structured subagent_status field over text parsing (#3146) — reporting/parsing only; subagent execution unchanged; frontend falls back to legacy prefix parsing for old threads. Backward-compatible for our general-purpose subagents.
- `7679f21e` fix(frontend): truncate overflowing text in agent cards (#3391) — UI fix in `workspace/agents/agent-card.tsx`; no rebrand overlap (our rebrand touched workspace-header/workspace-container).

**New optional config knobs (none required):** `DEERFLOW_WRITE_FILE_MAX_BYTES` (env; raise/disable the 80 KB agent-write_file cap) and per-model `stream_chunk_timeout` in `config.yaml`. Neither is currently set; defaults accepted.

Earlier 2026-06-07 sync absorbed 1 commit cleanly
(verified via merge-tree sim + drain-bounds/deploy-path analysis; tree `46b6f6ba`, zero conflicts)
**+ 1 local compose hardening** (`stop_grace_period`):

- `268fdd69` fix(gateway): drain in-flight runs before closing checkpointer on shutdown (#3381) — **directly relevant to our frequent redeploys.** Chat runs execute in fire-and-forget background asyncio tasks that write checkpoints through the shared Postgres checkpointer. On SIGTERM (which every `compose up --build` recreate sends), the old shutdown closed the `AsyncConnectionPool` (from `031d6fbc`) while runs were mid-graph → langgraph's `_checkpointer_put_after_previous` hit `psycopg_pool.PoolClosed`, surfacing as "unhandled exception during asyncio.run() shutdown" and potentially **losing the run's final checkpoint**. The fix adds `RunManager.shutdown()` that cancels + bounded-awaits in-flight runs BEFORE the `AsyncExitStack` closes the pool, so the final checkpoint write lands while the pool is open. **Bounded** by `_RUN_DRAIN_TIMEOUT_SECONDS = 5.0` (drain await + interrupted-status persist share one deadline) and **shielded** against a 2nd SIGTERM, so it cannot hang shutdown. **Multi-worker safe** (`GATEWAY_WORKERS=4`): per-worker drain, no shared state, concurrent → ~5s wall-clock not 4×5s. Idle gateway = instant no-op. Additive +497/−0 (`gateway/deps.py`, `runtime/runs/manager.py`, tests).
- **LOCAL compose patch (`809dbd67`):** set `stop_grace_period: 30s` on the gateway service. The lifespan now runs **two sequential 5s shutdown hooks** — channel-stop then the new run-drain = ~10s worst case — which brushes Docker's default 10s SIGTERM grace; a both-hooks-max teardown could SIGKILL mid-drain and re-expose the truncated-checkpoint case #3381 prevents. 30s keeps the bounded drain comfortably inside grace on every redeploy. (This is the first local edit to `docker/docker-compose.yaml` beyond the bind-mounts.)

Earlier 2026-06-06 sync absorbed 3 commits cleanly
(verified via merge-tree sim + upload-contract behavioral analysis; tree `22122e74`, zero conflicts):

- `1aac408d` fix upload file size contract (#3408) — **despite the name, NOT a max-size limit change.** It's a response data-type fix: the `size` field in upload/list API responses goes from JSON string (`"13"`) → integer (`13`), with proper Pydantic response models (`UploadedFileInfo`/`UploadListResponse`) replacing the buggy `list[dict[str,str]]` that forced size-stringification. **Limit enforcement unchanged** (50 MB/file, 100 MB total, 10 files, server-measured streaming, 413 errors). Our PDF/docx/xlsx doc-parsing pipeline is unaffected — the new `UploadedFileInfo` model explicitly whitelists our `markdown_file`/`markdown_path`/`markdown_virtual_path`/`markdown_artifact_url` fields, so stricter validation won't strip them; our frontend already types `size: number`. Touches `gateway/routers/uploads.py`, `client.py`, `uploads/manager.py`, `frontend/core/uploads/api.ts` (+`skipped_files` type), tests. **Reference (pre-existing, unchanged):** upload limits are `config.yaml`-configurable via an `uploads:` section (`max_file_size`/`max_total_size`/`max_files`), exposed at `GET /uploads/limits`; default per-file ceiling **50 MB** — relevant for large WRI report PDFs (override there if needed).
- `9a5de8d6` fix(ux): remove Backspace shortcut for deleting prompt attachments (#3410) — frontend UX fix in `ai-elements/prompt-input.tsx` (−13). File is byte-identical to merge-base; our WRI rebrand never touched it. Removes an accidental-delete keybinding.
- `dd8f9bf5` chore: AI-assistance disclosure in PR template + CONTRIBUTING (#3398) — `.github/` + `CONTRIBUTING.md` docs only. Inert.

Earlier 2026-06-05 sync absorbed 1 commit cleanly
(verified via merge-tree sim + behavioral analysis; inert for us — see MCP note):

- `2bbc7879` refactor(tool-search): consolidate MCP metadata tag + harden deferred-tool setup (#3370) — follow-up to `d9f47249` (#3342). Adds `tools/mcp_metadata.py` as the single source of truth for the `"deerflow_mcp"` tag (dedup), and hardens `tool_search.search` against malformed model queries (empty/whitespace/bare-`+` now return `[]` instead of matching-everything or `IndexError`). Verified behavior-preserving: same tag string/predicate/call-sites, ranking + deferral gate untouched. **Doubly inert for us** — it's a behavior-preserving refactor *and* `tool_search.enabled: false` here so the whole subsystem is a runtime no-op (see "## MCP servers"). Merge-tree sim: clean (tree `c76cac69`, zero conflicts). Backend-only → redeployed to keep image in sync, though behavior is identical either way.

Earlier 2026-06-04 sync absorbed 1 commit cleanly
(no overlap with local patches; redeploy needed — backend ships in image):

- `28b1da21` fix(agents): harden `update_agent` null-like args (#3237) — adds a pydantic `BeforeValidator` to the `update_agent` builtin tool that coerces literal string `"null"`/`"none"`/`"undefined"` into an actual `None`, across `soul`/`description`/`skills`/`tool_groups`/`model`. Plus a one-line lead-agent prompt note. Relevant since users customise agents via the workspace — prevents the model accidentally writing the literal string `"null"` into agent config. Purely defensive; no behaviour change for well-formed calls. Touches `tools/builtins/update_agent_tool.py` (+35/-9), `agents/lead_agent/prompt.py` (+1), plus tests.

Earlier 2026-06-03 (latest) sync absorbed 6 commits cleanly
— pre-merge `git merge-tree` simulation confirmed the whole batch auto-merges
to a single conflict-free tree; redeploy needed (frontend + backend ship in image):

- `9a53f9df` fix(frontend): preserve chronological order of thread history after context compression (#3354) — **genuine fix for us.** `core/threads/hooks.ts`: forward iteration restores chronological order after compression + a bounded (cap-5) auto-continue past empty/summarized runs. Directly relevant since we run `DeerFlowSummarizationMiddleware` — fixes user-visible out-of-order / blank history after compression. Merged byte-identical to upstream (file is purely upstream-tracked; our rebrand never touched `core/threads/`).
- `8fca56cf` fix(mcp): accept `transport` field as alias for `type` (#3238) — purely additive alias in `extensions_config.py`; only fires when `transport` present AND `type` absent. Our Playwright `"type": "stdio"` config is untouched and still valid.
- `3fddc24c` chore: remove stale LangGraph server runtime remnants (#3344) — drops `EXPOSE 2024` from both Dockerfile stages (doc-only metadata for the standalone LangGraph server we don't run; gateway listens on 8001) + a stale `@-rm -rf backend/.langgraph_api` from `make clean`. **Verified our readabilipy (Dockerfile:57) + Playwright MCP/chromium (Dockerfile:95-102) additions survived intact** — upstream's edits were 13+ lines away.
- `0ffa995f` feat: upgrade MiniMax default model to M3 (#3357) — touches only `config.example.yaml` + MiniMax tests/docs. Our live `config.yaml` and active models (qwen/deepseek/claude/mimo) untouched; we don't use MiniMax. Inert.
- `89ae74d4` fix(skills): surface offending line + quoting hint on SKILL.md YAML error (#3335) — error-message-only; never reached for a valid SKILL.md. Inert.
- `0d0968a3` chore: add sandbox memory profiling tools (#3249) — unwired standalone kubectl-shelling script + docs; nothing imports it (we run embedded LangGraph, not k8s sandbox). Additive.

Earlier 2026-06-03 (later) sync absorbed 2 commits cleanly
— **`.github/`-only repo hygiene, no redeploy needed** (nothing ships in the image):

- `f97b0c0f` feat(issue-templates): structured bug/feature issue forms (#3359) — replaces `runtime-information.yml` with `bug-report.yml` + `feature-request.yml` + `config.yml`.
- `aca7acc1` feat(ci): PR/issue auto-labeling + declarative label sync (#3360) — adds `labeler.yml`, `labels.yml` (29 labels), `scripts/sync_labels.py`, and 4 workflows (label-sync, pr-labeler, pr-triage, issue-triage). **Note:** merging+pushing to `jtaynl/deer-flow` may activate these GitHub Actions in the fork — `label-sync.yml` self-bootstraps the 29 labels on push to the default branch; triage workflows fire on PRs/issues. Harmless for this deployment fork (no external PRs/issues); the `pull_request_target` workflows never check out PR code (API metadata only). Disable Actions in fork settings if the noise is unwanted.

Earlier 2026-06-03 sync absorbed 1 commit cleanly
(no overlap with local patches; pre-merge impact analysis run — see below):

- `3ae82dc6` fix(mcp): add auth interceptor with channel user_id and keep header propagation to mcp tools (#3294) — touches `channels/manager.py`, `gateway/services.py`, `gateway/internal_auth.py`, `config/paths.py` (+20), `mcp/tools.py`, plus regression tests. Because it changes how `user_id` is normalised for filesystem paths, it got a 4-dimension impact analysis before merge (data-continuity / live-user-inventory / MCP-forwarding / channel-gateway-auth). Findings:
  - **No data-continuity risk.** The new `make_safe_user_id()` helper in `config/paths.py` is purely additive and is **not** wired into the directory resolvers — `user_dir`/`thread_dir`/`host_thread_dir` still call `_validate_user_id` (returns the id verbatim). Its only production caller is `channels/manager.py:679` on the inbound-channel path, which this channel-free deployment never executes. Existing per-user dirs (`backend/.deer-flow/users/{default, <uuid>}/`) resolve to identical paths post-merge. No migration needed.
  - **MCP forwarding inert for us.** Playwright is `stdio` with no OAuth/interceptors, so `tool_interceptors` is empty and the modified `base_handler` is never constructed; our calls take the unchanged `else` branch. Pure hardening.
  - **`internal_auth.py`** = pure refactor (magic string `"internal"` → shared constant, identical value). **`services.py`** new branches are dead code for cookie-authenticated users (better-auth `system_role` is only `admin`/`user`, never `internal`).
  - **Security-positive side effect:** a client-supplied `context.user_id` in a `/runs` body **cannot spoof** the authenticated user — `merge_run_context_overrides` uses `setdefault`, then `inject_authenticated_user_context` unconditionally overwrites with the server-authenticated id.

Earlier 2026-06-02 (later) sync absorbed 2 commits cleanly
(no overlap with local patches):

- `5dc2d6cb` fix(sandbox): close `AioSandbox` HTTP client during provider teardown (#2872) (#3245) — **directly relevant to this 24/7 deployment**. Long-running services were leaking host-side sockets because the `httpx.Client` nested inside cached `AioSandbox` instances was never explicitly closed during `AioSandboxProvider.release/destroy/shutdown`. Two-stage fix: first attempt closed `wrapper.httpx_client` (which turned out to be the Fern wrapper without a `close()`), then a follow-up resolves the real `_client_wrapper.httpx_client.httpx_client` socket-owning client. Provider teardown now calls `close()` under a lock with use-after-close/double-close safety. Touches `community/aio_sandbox/aio_sandbox.py` (+52), `aio_sandbox_provider.py` (+33), tests +159. Should reduce slow socket accumulation in gateway over time.
- `d9f47249` fix(tool-search): reliably hide deferred MCP schemas by removing the ContextVar (#3342) — major refactor: replaces `ContextVar`-based `DeferredToolRegistry` with closures + graph state (`ThreadState.promoted`, hash-scoped). Build the deferred catalog + tool_search per-agent from the policy-filtered tool list, pass `deferred_names`/`catalog_hash` explicitly to `DeferredToolFilterMiddleware` and the prompt, record promotions via a Command-returning `tool_search`. Removes `DeferredToolRegistry` and `_registry_var`. Affects MCP-tool surfacing (we run Playwright MCP). Net +1006/-1265 across 21 files including new dedicated tests.

Earlier 2026-06-02 sync absorbed 1 commit cleanly
(docs-only, no operational impact):

- `74e3e80c` docs: clean gateway runtime transition remnants (#3334) — docs cleanup touching `backend/docs/AUTH_TEST_DOCKER_GAP.md`, `backend/docs/AUTH_UPGRADE.md`, `docs/CODE_CHANGE_SUMMARY_BY_FILE.md`, plus +14 lines in `test_gateway_runtime_cleanup.py`. No code paths changed.

Earlier 2026-06-01 (latest) sync absorbed 1 commit cleanly
(no overlap with local patches):

- `019bd16a` fix: load paginated run history messages (#3305) — fixes message loading for paginated run history. Adds `backend/app/gateway/pagination.py`, tweaks `routers/runs.py` and `routers/thread_runs.py`, and rewires `frontend/src/core/threads/hooks.ts` (+86) and `types.ts` to fetch + merge paginated message pages. No conflict with our WRI rebrand (rebrand touches `landing/`, `workspace/`, `(auth)/`, `i18n/`; pagination touches `core/threads/`).

Earlier 2026-06-01 (later) sync absorbed 1 commit cleanly
— **directly retired gotcha #17** (psycopg stale-connection):

- `031d6fbc` fix(checkpointer): use `AsyncConnectionPool` for postgres to prevent stale connection errors (#3223) (#3226) — replaces `AsyncPostgresSaver.from_conn_string()` with an explicit `AsyncConnectionPool` that has `check_connection` enabled, plus TCP keepalive probes on each connection. Dead idle connections are now detected and replaced on checkout instead of raising `psycopg.OperationalError: the connection is closed`. **Retires the manual `docker restart deer-flow-gateway` runbook step** after DO managed Postgres maintenance / resize / failover events. Touches `backend/.../runtime/checkpointer/async_provider.py` (+55/-12) and tests.

Earlier 2026-06-01 sync absorbed 1 commit cleanly:

- `d6a604d5` fix(makefile): extract setup-sandbox inline bash to script for Windows compatibility (#3326) — extracts the `setup-sandbox` target's inline bash from the `Makefile` into a standalone `scripts/setup-sandbox.sh` so it works under git-bash/WSL on Windows. Same behaviour, just refactored shape. No operational impact on this Linux deployment.

Earlier 2026-05-31 sync absorbed 2 commits cleanly
(no overlap with local patches):

- `79cc2279` fix(middleware): fix LLM fallback run status (#3321) — corrects how `llm_error_handling_middleware` reports run status during model fallback, and optimises maker extraction in the streaming path. **Mildly relevant** since LGI Stage 1 exercises the qwen3.6-plus → qwen3.7-max fallback path, so cleaner status reporting helps batch-summary attribution. Touches `llm_error_handling_middleware.py` (+44/-5), runtime `journal.py` (+22), `worker.py` (+88), plus tests.
- `46ddc346` fix(channels): preserve Feishu clarification thread continuity (#3285) — Feishu-specific channel work. Irrelevant to this deployment (no Feishu integration). Touches `backend/app/channels/feishu.py` (+197), `manager.py` (+65), `message_bus.py` (+3), plus tests.

Earlier 2026-05-30 sync absorbed 1 commit cleanly:

- `9f3be2a9` fix(agents): offload `UploadsMiddleware` uploads scan off the event loop (#3311) — adds an `abefore_agent` async hook so the per-message uploads-directory scan (`exists`/`iterdir`/`stat` + sibling `.md` outline reads) runs in a worker thread via `run_in_executor` instead of blocking the asyncio loop. Copies the current context so `get_effective_user_id()` still resolves correctly. Relevant for any thread with an uploads dir. Touches `backend/packages/harness/deerflow/agents/middlewares/uploads_middleware.py` (+14), new `tests/blocking_io/test_uploads_middleware.py` (+56), plus 9 lines of docs.

Earlier 2026-05-29 (overnight) sync absorbed 4 commits cleanly
(no overlap with local patches):

- `ca487578` feat(agent): add `ToolOutputBudgetMiddleware` for oversized tool output protection (#3303) — new opt-in middleware that caps per-tool-call output size, externalising oversized results to a thread-local `.tool-results/` dir (model can re-read via `read_file` with offset/limit), with head+tail truncation fallback. Adds new config keys under `tool_output:` in `config.example.yaml` (+30 lines) — not enabled in our `config.yaml`; safe to ignore until we want to tune.
- `4093c833` refactor(provider): share assistant payload replay matching (#3307) — pulls the common replay matching out of `patched_mimo.py`, `patched_deepseek.py`, `patched_openai.py` into a shared `assistant_payload_replay.py`. **Also fixes a `reasoning_content` silently-dropped bug** when an earlier assistant message is reordered/dropped during serialization — directly relevant since we run MiMo (5th active model) and DeepSeek paths exercise this hot.
- `052b1e21` test(runtime): Blockbuster runtime anchor for `JsonlRunEventStore` async IO (#3313) — test-only; locks `put`, `put_batch`, `list_messages`, `list_events`, etc. under the Blockbuster gate so any reintroduced blocking IO fails CI.
- `e8e9edcb` fix(channels): ignore hidden control messages when extracting replies (#3219) (#3270) — channel reply extraction now skips internal control messages; backend non-overlap.

Earlier 2026-05-29 (late) sync absorbed 1 commit:

- `e683ed6a` fix(runtime): guide malformed write_file recovery (#3040) — runtime middleware tweak that teaches the dangling-tool-call middleware how to redirect the model after a malformed `write_file` call. Touches `backend/agents/middlewares/dangling_tool_call_middleware.py` (+25/-2) and its test (+19). No config or API surface change.

Earlier 2026-05-29 (evening) sync absorbed 1 commit:

- `872079b8` docs: clean standalone LangGraph server remnants (#3301) — removes references to a deprecated standalone-LangGraph-server deployment mode. We run embedded LangGraph (gateway-hosted), so no operational impact. Touches `backend/docs/AUTH_TEST_PLAN.md`, `backend/CLAUDE.md`, `backend/app/gateway/routers/mcp.py` (7 lines), `mcp/cache.py` (3 lines), new `test_gateway_runtime_cleanup.py`, and `frontend/.env.example`.

Earlier 2026-05-29 (later) sync absorbed 1 commit:

- `cbf8b194` fix(runtime): harden JSONL async I/O and DB put_batch thread validation (#3084) — hardens the run_events persistence layer. We use `run_events.backend: db` per config tuning, so the DB-side validation is the relevant part for us; the JSONL changes are precautionary for the other backend.

Earlier 2026-05-29 sync absorbed 1 commit:

- `d46a5779` fix(chat): preserve messages after summarization (#3280) — relevant for long research threads where summarization fires at our raised 32K trigger; prior bug could silently drop post-summary messages from chat history

Prior sync **2026-05-28 (later)** absorbed 7 commits
cleanly (no conflicts on overlap-risk paths):

- `2ace78d1` fix(frontend): surface backend detail when agent name check fails
- `8330b244` docs: add blocking IO detection usage and maintenance (`backend/docs/BLOCKING_IO_DETECTION.md`)
- `44677c5e` **feat(provider): add patched MiMo reasoning content support** — new model adapter for Xiaomi's MiMo reasoning model, with thinking-content stripping. `config.example.yaml` includes sample entry; not added to active config in this deployment
- `2fdfff0d` fix(frontend): Mermaid preview failure in historical messages
- `737abc0e` fix(api-client): ignore stale run reconnect conflicts (SSE)
- `8decfd32` Fix custom skill install permissions (mostly test coverage)
- `02872407` fix(frontend): show new thread in sidebar immediately on creation

Earlier sync (**2026-05-28**) absorbed 4 commits:

- `a5599c10` fix(gateway): honour `on_disconnect` on `/wait` endpoints — fixes resource leak when a `/api/threads/.../wait` client disconnects mid-request (relevant for downstream pipelines that call `wait` endpoints)
- `3cb75887` fix(memory): parse wrapped memory update JSON responses
- `37451500` fix(gateway): split `stream_existing_run` into per-method routes for unique OpenAPI operationIds — internal refactor, URL paths unchanged
- `9e332c59` chore(deps): bump frontend `uuid` 10.0.0 → 14.0.0

Prior sync (**2026-05-27**) absorbed 6 commits:

- `162fb214` fix(mcp): skip session pooling for HTTP/SSE transports — see gotcha #19
- `b00749a8` fix(auth): share internal gateway token across workers — see gotcha #18
- `92905e9e` fix(todo): reuse thread state schema
- `e344be8d` feat(tests): add Blockbuster runtime gate for event-loop blocking IO
- `da41701f` Add static blocking IO inventory (script + tests)
- `e0280194` chore: add a pull request template

The original commits remain in history but the file contents now match
upstream. Everything else still on `local-fixes` vs `upstream/main` is
UI/branding/i18n tweaks plus two Dockerfile additions (readabilipy
node-deps install, Playwright MCP + chromium baking) — none are
behavior fixes, they're capability additions for this deployment.

## Maintainer hygiene

If you push commits from this server to your fork (e.g. when adding new
local patches or updating docs), configure git to use your GitHub no-reply
email — not your personal email — so the public commit history doesn't
expose it:

```bash
# Per-repo (preferred; doesn't affect other clones on the host)
git config user.email "<your-github-user-id>+<your-github-login>@users.noreply.github.com"
git config user.name  "<your-github-login>"
```

Find your numeric user ID via `gh api user --jq '.id'` or
`https://api.github.com/users/<login>`. Belt-and-suspenders: enable both
checkboxes at https://github.com/settings/emails:

- **Keep my email addresses private**
- **Block command line pushes that expose my email**

The second one would have prevented past mistakes by failing the push
with a clear error pointing at the no-reply address. If old commits with
a personal email are already pushed, rewrite the recent N commits and
force-push to `local-fixes`:

```bash
git rebase HEAD~<N> --exec 'git commit --amend --no-edit --reset-author'
git push --force-with-lease origin local-fixes
```

GitHub's events API may cache the old email briefly (usually purges
within a day); the Git history itself is corrected immediately. Anyone
who already cloned `local-fixes` keeps the old commits locally — usually
that's just the production server, where it doesn't matter.

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
