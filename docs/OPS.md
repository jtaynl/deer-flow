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

14. **AIO sandbox provider does not health-check before reuse.** The
    gateway keeps an in-process map of `thread_id → sandbox_id`. If the
    underlying sandbox container dies between tool calls (idle eviction,
    OOM, `--rm` after exit, a host-level `docker rm`, or any DooD-side
    cleanup), the provider keeps reusing the dead ID. Every subsequent
    tool call in that thread hangs ~120 s and then fails with
    `Failed to execute command in sandbox: [Errno 110] Connection timed out`.
    The agent's reasoning loop keeps retrying tool calls that all time
    out, burning tokens and producing no progress.

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

17. **Managed Postgres resize / failover invalidates the gateway's psycopg
    connection pool.** When the DB host is resized (e.g. DO managed
    Postgres tier change), maintenance-restarted, or fails over, existing
    TCP connections from the gateway are dropped server-side. The
    gateway's psycopg async pool does **not** auto-recycle dropped
    connections — it hands stale handles to the next request, which then
    raise `psycopg.OperationalError: the connection is closed`. Symptoms:

    - `/api/threads/{id}/history` and other DB-backed endpoints start
      returning HTTP 500 right after a Postgres maintenance event.
    - Gateway logs show repeated
      `psycopg.OperationalError: the connection is closed` tracebacks.
    - `/api/health` keeps returning the auth challenge fine (no DB
      call), so naive uptime checks don't catch this.

    Fix: a plain Docker restart of the gateway is enough — no rebuild,
    no compose down. The image is unchanged; only the in-process
    connection pool needs to be re-created.

    ```bash
    sg docker -c 'docker restart deer-flow-gateway'
    ```

    Verification:

    ```bash
    sg docker -c 'docker logs --since 2m deer-flow-gateway 2>&1 \
      | grep -iE "connection is closed|connection|error" | tail -5'
    # expect: empty (no stale connection errors)
    ```

    The Langchain checkpointer (which also uses psycopg) auto-recovers
    on the next checkpoint write, but the gateway's app-level pool does
    not — hence the asymmetric failure mode.

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

Most recent upstream sync: **2026-05-31** absorbed 2 commits cleanly
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
