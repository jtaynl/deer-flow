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

Most recent upstream sync: **2026-06-11 (later)** absorbed 4 commits
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
