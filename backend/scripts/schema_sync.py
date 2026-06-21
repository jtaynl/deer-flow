"""schema_sync.py — reconcile the live DB schema with the current ORM models.

WHY: deer-flow builds its schema with ``Base.metadata.create_all``, which only CREATES missing tables —
it never ALTERs an existing one. So when an upstream upgrade adds a column to an already-provisioned
table's model (e.g. 2.0 added ``runs.token_usage_by_model``), that column is silently missing on existing
deployments and every query touching it raises ``UndefinedColumnError`` → HTTP 500. ``create_all`` is even
labelled "dev convenience; production should use Alembic" in engine.py, but the deploy never runs Alembic
and upstream doesn't ship migrations for these columns. This tool closes that gap.

It diffs each ORM table against the live DB and ADDs any missing columns:
  * nullable columns           → added as-is (no backfill needed)
  * NOT NULL + a safe default  → added NOT NULL with a backfill default, then DROP DEFAULT
                                 (so the result matches what a fresh create_all would produce)
  * NOT NULL + no safe backfill→ REPORTED but not applied (operator decides the backfill)
Whole missing tables are left to create_all. All DDL runs in one transaction.

USAGE (inside the gateway container):
  cd /app/backend && PYTHONPATH=/app/backend .venv/bin/python scripts/schema_sync.py            # check-only; exit 1 on drift
  cd /app/backend && PYTHONPATH=/app/backend .venv/bin/python scripts/schema_sync.py --apply     # add the safe missing columns
"""
from __future__ import annotations

import argparse
import asyncio
import sys

from sqlalchemy import text
from sqlalchemy.dialects import postgresql

from app.gateway.deps import get_config
from deerflow.persistence.base import Base
from deerflow.persistence.engine import get_engine, init_engine_from_config
import deerflow.persistence.models  # noqa: F401 — registers every table on Base.metadata


def _backfill_literal(col, type_sql: str) -> str | None:
    """A safe server-default literal to backfill existing rows for a NOT NULL add, or None if unsafe to guess."""
    t = type_sql.upper()
    d = col.default.arg if (col.default is not None and getattr(col.default, "is_scalar", False)) else None
    if "JSON" in t:
        return "'{}'::jsonb" if "JSONB" in t else "'{}'::json"
    if any(k in t for k in ("INT", "NUMERIC", "DECIMAL", "FLOAT", "DOUBLE", "REAL")):
        return str(d if isinstance(d, (int, float)) else 0)
    if "BOOL" in t:
        return "true" if d is True else "false"
    if any(k in t for k in ("VARCHAR", "TEXT", "CHAR")):
        return "'" + str(d).replace("'", "''") + "'" if isinstance(d, str) else "''"
    return None  # timestamps / unknown → don't guess; report for manual handling


async def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true", help="apply the safe ALTERs (default: check-only, exit 1 on drift)")
    args = ap.parse_args()

    cfg = get_config()
    await init_engine_from_config(cfg.database)
    eng = get_engine()
    if eng is None:
        print("persistence backend is not SQL — nothing to reconcile")
        return 0

    drift: list[str] = []
    ddl: list[str] = []
    manual: list[str] = []
    async with eng.connect() as conn:
        for table in Base.metadata.sorted_tables:
            exists = (await conn.execute(text("select to_regclass(:t)"), {"t": table.name})).scalar()
            if exists is None:
                continue  # brand-new table → create_all handles it
            rows = await conn.execute(text("select column_name from information_schema.columns where table_name=:t"), {"t": table.name})
            live = {r[0] for r in rows}
            for col in table.columns:
                if col.name in live:
                    continue
                type_sql = col.type.compile(dialect=postgresql.dialect())
                if col.nullable:
                    drift.append(f"{table.name}.{col.name} (nullable)")
                    ddl.append(f'ALTER TABLE "{table.name}" ADD COLUMN IF NOT EXISTS "{col.name}" {type_sql}')
                else:
                    bf = _backfill_literal(col, type_sql)
                    if bf is None:
                        drift.append(f"{table.name}.{col.name} (NOT NULL, no safe backfill — MANUAL)")
                        manual.append(f"{table.name}.{col.name}")
                    else:
                        drift.append(f"{table.name}.{col.name} (NOT NULL, backfill {bf})")
                        ddl.append(f'ALTER TABLE "{table.name}" ADD COLUMN IF NOT EXISTS "{col.name}" {type_sql} NOT NULL DEFAULT {bf}')
                        ddl.append(f'ALTER TABLE "{table.name}" ALTER COLUMN "{col.name}" DROP DEFAULT')

    if not drift:
        print("schema in sync — no missing columns")
        return 0
    print(f"SCHEMA DRIFT — {len(drift)} missing column(s):")
    for d in drift:
        print("  -", d)
    if not args.apply:
        print("\nRun with --apply to add the safe columns.")
        return 1
    if ddl:
        async with eng.begin() as conn:
            for stmt in ddl:
                await conn.execute(text(stmt))
                print("OK:", stmt)
    if manual:
        print(f"\n⚠ {len(manual)} NOT-NULL column(s) need a MANUAL backfill decision: {', '.join(manual)}")
        return 1
    print(f"\napplied — schema now in sync ({len(ddl)} statements).")
    return 0


sys.exit(asyncio.run(main()))
