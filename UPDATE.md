# Updating DeerFlow Safely

This guide covers the safe workflow for pulling updates from the official `bytedance/deer-flow` upstream repository into this fork. Follow it every time you update — it's designed to keep your working setup recoverable if anything breaks.

## TL;DR — the standard update sequence

For a routine update of a clean working install, run these in order:

```bash
# 1. Stop services and backup
make docker-stop
git tag working-$(date +%Y%m%d)
mkdir -p ../deer-flow-backups/$(date +%Y%m%d)
cp config.yaml ../deer-flow-backups/$(date +%Y%m%d)/
cp .env ../deer-flow-backups/$(date +%Y%m%d)/
cp -r .deer-flow ../deer-flow-backups/$(date +%Y%m%d)/ 2>/dev/null || true

# 2. Review what's changing
git fetch upstream
git log --oneline HEAD..upstream/main
# (then read GitHub release notes at https://github.com/bytedance/deer-flow/releases)

# 3. Apply the update
git checkout main
git merge upstream/main
make doctor

# 4. Rebuild and restart
make docker-init
make docker-start

# 5. Smoke test at http://localhost:2026
```

Read the rest of this document for the reasoning behind each step and what to do when things go wrong.

---

## Before you update — pre-flight checks

### 1. Stop running services

Never update with containers running — it can corrupt Docker volumes and leave the database in an inconsistent state.

```bash
make docker-stop
```

### 2. Confirm clean working tree

```bash
git status
```

You should see `nothing to commit, working tree clean`. If there are modified files, you have two choices:

- **Recommended:** commit them to a feature branch first (see "Working with customizations" below)
- **Quick option:** stash them temporarily

```bash
git stash push -m "before-update-$(date +%Y%m%d)"
# ... do the update ...
git stash pop                      # to restore your changes after
```

### 3. Back up everything that matters

These files are uniquely yours and worth preserving:

```bash
mkdir -p ../deer-flow-backups/$(date +%Y%m%d)
cp config.yaml ../deer-flow-backups/$(date +%Y%m%d)/
cp .env ../deer-flow-backups/$(date +%Y%m%d)/
cp -r .deer-flow ../deer-flow-backups/$(date +%Y%m%d)/ 2>/dev/null || true
cp -r skills ../deer-flow-backups/$(date +%Y%m%d)/ 2>/dev/null || true
```

What each backup item is for:

- **`config.yaml`** — your model and tool configuration
- **`.env`** — your API keys
- **`.deer-flow/`** — runtime state, memory, conversation history. Losing this means losing your past chats.
- **`skills/`** — any custom skills you've built

### 4. Tag your current working state

Before merging, tag the current commit. This gives you a named restore point you can always return to:

```bash
git tag working-$(date +%Y%m%d)
git push origin working-$(date +%Y%m%d)
```

If a future update breaks something badly, you can always: `git checkout working-YYYYMMDD`.

---

## Pulling updates from upstream

### 5. Fetch but don't merge

This downloads ByteDance's changes without applying them, so you can review first:

```bash
git fetch upstream
```

### 6. Review what's about to change

```bash
# List commits you don't have yet
git log --oneline HEAD..upstream/main

# See actual file-level changes
git diff HEAD upstream/main --stat
```

Pay close attention if you see changes to any of these — they often require coordinated updates on your side:

- `config.example.yaml` — new config fields may have been added
- `Makefile` — command behaviors may have changed
- `docker/` — Docker setup may have shifted
- `backend/pyproject.toml` — Python dependencies changed
- `frontend/package.json` — Node dependencies changed
- `scripts/` — setup or runtime scripts changed
- `Install.md` or `README.md` — installation procedure may have changed

### 7. Read the release notes

Always check the official release notes before merging:

> [https://github.com/bytedance/deer-flow/releases](https://github.com/bytedance/deer-flow/releases)

Look for:

- ⚠️ Breaking changes
- New required config fields or environment variables
- Schema or storage migrations
- Required Docker image rebuilds
- Deprecated features

### 8. Merge

Once you're satisfied with the review:

```bash
git checkout main
git merge upstream/main
```

**If you get merge conflicts, stop and don't force anything.** Conflicts most commonly happen in `config.yaml` (because yours has secrets and customizations, theirs is the template). Resolve them carefully — usually you keep your values for keys/credentials and accept their changes for new structure.

To abort a merge that's gone sideways:

```bash
git merge --abort
```

This puts you back exactly where you were before `git merge`.

---

## Applying the update

### 9. Re-validate configuration

New versions sometimes add required config fields. Run the doctor:

```bash
make doctor
```

If doctor flags new missing config values, compare your `config.yaml` against the latest example:

```bash
diff config.yaml config.example.yaml
```

Or open both side-by-side in VS Code (`Ctrl+\` to split the editor).

### 10. Rebuild Docker images

This is essential — code changes need new images:

```bash
make docker-init
make docker-start
```

First run after an update takes longer because backend and frontend images are rebuilt.

### 11. Smoke test

Open `http://localhost:2026` and run a simple research query. Verify:

- The UI loads
- You can send a chat message
- The agent successfully completes a basic research task
- Web search returns results
- No red errors in `make docker-logs`

---

## When something breaks — rolling back

This is why you took backups. Rolling back is straightforward:

```bash
# Stop everything
make docker-stop

# Undo the merge
git reset --hard ORIG_HEAD

# Or, if you've done other commits since, restore from your tag
# git reset --hard working-YYYYMMDD

# Restore your config and env files
cp ../deer-flow-backups/YYYYMMDD/config.yaml .
cp ../deer-flow-backups/YYYYMMDD/.env .

# Restart
make docker-start
```

You're back to where you were before the update.

If volumes have been corrupted, you may also need:

```bash
docker compose -f docker/docker-compose-dev.yaml down -v
make docker-init
make docker-start
```

Note: `down -v` removes Docker volumes. Only do this if you're certain you've restored your backed-up `.deer-flow` directory.

---

## Working with customizations

This fork exists so you can customize DeerFlow. Keep customizations in a place that doesn't conflict with upstream merges.

### The branching strategy

**Always work on a branch, never directly on `main`:**

```bash
git checkout -b sipmm-customizations
# ... make changes ...
git add .
git commit -m "Customize DeerFlow for SIPMM use case"
git push origin sipmm-customizations
```

When you want to pull in upstream updates:

1. Do steps 1–11 above on `main`
2. Then merge `main` into your customization branch:

```bash
git checkout sipmm-customizations
git merge main
```

This keeps upstream merges separate from your custom changes — much easier to debug if something goes wrong.

### Preferred layers for customization

Each layer is more upgrade-safe than the next:

1. **Best (upgrade-proof):** customize via `config.yaml`, `.env`, the skills system, or MCP servers
2. **Acceptable:** add new files in your own subdirectories (e.g., `skills/sipmm/`, `custom-prompts/`). Upstream won't touch these.
3. **Risky:** modify existing core files in `backend/src/` or `frontend/`. Every upstream update will likely create conflicts here.

The deeper you customize, the more painful updates become. Stay in layers 1 and 2 wherever possible.

---

## How often should you update?

**Don't update for the sake of updating.** DeerFlow 2.0 is moving fast and not every release is stable.

Suggested cadence:

- **Never auto-update.** Never run `git pull upstream main` without reading release notes first.
- **Wait a week after major releases.** Let early adopters hit the bugs first.
- **Update for specific reasons:** a bug you're hitting, a feature you want, a security advisory. Not because there's a new commit available.
- **Tag every working state** before updating, so you always have a known-good restore point.

---

## Quick reference — useful commands

| Task | Command |
|------|---------|
| Stop all services | `make docker-stop` |
| Start all services | `make docker-start` |
| View live logs | `make docker-logs` |
| Health check | `make doctor` |
| See what upstream has | `git fetch upstream && git log --oneline HEAD..upstream/main` |
| Tag current state | `git tag working-$(date +%Y%m%d)` |
| List your tags | `git tag -l 'working-*'` |
| Roll back to a tag | `git reset --hard working-YYYYMMDD` |
| Abort a bad merge | `git merge --abort` |
| List your backups | `ls ../deer-flow-backups/` |

---

*Last reviewed: 2026-05-08. Update this doc if the underlying DeerFlow update procedure changes.*