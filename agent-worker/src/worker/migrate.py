"""One command to bring a Supabase database up to date: `python -m worker.migrate`.

Why this exists
---------------
Standing the schema up meant pasting 26 files into the SQL editor in the right
order, which is slow, easy to get wrong, and offers no way to tell afterwards
what actually ran. It also produced the failure we hit in production twice:
code deployed ahead of its schema, `insert_call_log` swallowing the resulting
error (see supabase_client.py), and whole call records disappearing with only a
log line to show for it. A migrate step that runs before the worker restarts
removes that class of bug outright.

Why not a single hand-written schema.sql
----------------------------------------
It is tempting -- one file, one paste, done -- and it is a trap. It becomes a
27th definition of the schema that has to be kept in step with the 26 files by
hand, and the day it drifts is the day a fresh install stops matching
production. The files in supabase/migrations are already the definition; this
just runs them, in order, in seconds. A fresh database gets all 26 applied in
one go, which is the "single script" part, without inventing a second source of
truth.

The three states a database can be in
-------------------------------------
Detected rather than asked about, because getting it wrong by hand is what
makes this dangerous:

  FRESH    no schema_migrations, no `agents` table -- an empty project. Apply
           everything.
  UNTRACKED  no schema_migrations, but `agents` exists -- a database that was
           set up by hand before this runner existed, which is exactly what
           production is. Applying 0001 to it would fail on the first CREATE.
           Refused unless --adopt is passed, which records the migrations as
           applied *without running them*.
  TRACKED  schema_migrations exists -- apply whatever is missing.

Safety
------
* One transaction per migration. Postgres DDL is transactional, so a file that
  fails half way leaves nothing behind.
* A session advisory lock, so two workers starting together can't both run it.
* Each file's sha256 is recorded, so a migration edited after it was applied is
  reported instead of quietly diverging.
* Only 3 of the 26 files use `if not exists`; the rest would error on a second
  run. That is fine because each runs exactly once -- but it is also why a
  partially-applied database is refused rather than retried.

Usage
-----
    python -m worker.migrate --dry-run     # what would run, changes nothing
    python -m worker.migrate               # apply pending
    python -m worker.migrate --adopt       # existing DB: record, don't run

Needs SUPABASE_DB_URL (the Postgres connection string, not the REST URL --
DDL cannot go through PostgREST). Optionally ADMIN_EMAIL, which is added to
allowed_users so somebody can actually sign in to a fresh install; migrations
0003/0004 seed Kodexo's own addresses and a new deployment needs its own.
"""

from __future__ import annotations

import argparse
import hashlib
import logging
import os
import pathlib
import sys

import psycopg
from dotenv import load_dotenv

logger = logging.getLogger("worker.migrate")

# supabase/migrations, found relative to this file so the command works from any
# working directory -- the deploy script runs it from agent-worker/.
MIGRATIONS_DIR = pathlib.Path(__file__).resolve().parents[3] / "supabase" / "migrations"

# Not a migration: a paste-into-the-SQL-editor copy of 0001+0002 from before
# this runner existed. Running it as well would duplicate those two.
EXCLUDED = {"run-in-sql-editor.sql"}

# Any 64-bit constant works; it only has to be the same in every process.
_LOCK_KEY = 8_274_419_003_115_672_001


def migration_files() -> list[pathlib.Path]:
    """Every migration, in the order their numeric prefix implies.

    Sorted by name, which is why the files are zero-padded -- `0009` before
    `0010` only holds while the width does. A file added as `27_foo.sql` would
    sort before `0001`, so keep the four digits.
    """
    if not MIGRATIONS_DIR.is_dir():
        sys.exit(f"migrations directory not found: {MIGRATIONS_DIR}")
    return sorted(p for p in MIGRATIONS_DIR.glob("*.sql") if p.name not in EXCLUDED)


def _db_url() -> str:
    # Same lookup order the worker uses, so one .env.local drives both.
    load_dotenv(".env.local")
    load_dotenv(".env")
    url = (os.environ.get("SUPABASE_DB_URL") or "").strip()
    if not url:
        sys.exit(
            "SUPABASE_DB_URL is not set.\n"
            "This is the Postgres connection string from Supabase → Project Settings →\n"
            "Database → Connection string → URI. The REST URL and service-role key\n"
            "cannot create tables, so they are not enough here."
        )
    return url if "sslmode=" in url else f"{url}?sslmode=require"


def _table_exists(cur: psycopg.Cursor, table: str) -> bool:
    cur.execute("select to_regclass(%s) is not null", (f"public.{table}",))
    return bool(cur.fetchone()[0])


def _ensure_tracking_table(cur: psycopg.Cursor) -> None:
    cur.execute("""
        create table if not exists schema_migrations (
            filename    text primary key,
            sha256      text not null,
            applied_at  timestamptz not null default now(),
            -- True for a row written by --adopt: the file was never executed
            -- here, its effects were already present. Worth being able to tell
            -- apart when a schema and its history disagree.
            adopted     boolean not null default false
        )
    """)
    cur.execute(
        "comment on table schema_migrations is "
        "'Which files in supabase/migrations have been applied. Written by "
        "agent-worker''s python -m worker.migrate; do not edit by hand.'"
    )


def _sha256(path: pathlib.Path) -> str:
    """Checksum of the migration's *content*, with line endings normalised.

    Hashing raw bytes was wrong across platforms. 0019 was committed with CRLF
    while the other 25 files are LF, and with core.autocrlf in play the same
    commit yields different bytes on a Windows machine and on the Linux VPS.
    Both run identical SQL, but a raw-bytes checksum called that drift and
    warned on every single deploy -- which is worse than not checking at all,
    because a warning that always fires is a warning nobody reads when a real
    change appears.

    Normalising CRLF to LF compares what actually matters. A genuine edit still
    changes the hash; a checkout on a different OS no longer does.
    """
    text = path.read_bytes().replace(b"\r\n", b"\n")
    return hashlib.sha256(text).hexdigest()


def _record(cur: psycopg.Cursor, path: pathlib.Path, *, adopted: bool) -> None:
    """Record a migration as applied.

    --adopt overwrites an existing checksum; a normal apply does not. Adopting
    means "this database matches these files as they stand now", so it doubles
    as the way to re-baseline after the checksum rule itself changes -- which is
    exactly what the CRLF fix above needed. A normal run must never quietly
    rewrite a checksum, because that is the one record showing a migration was
    edited after it ran.
    """
    conflict = (
        "do update set sha256 = excluded.sha256, adopted = true, applied_at = now()"
        if adopted
        else "do nothing"
    )
    cur.execute(
        "insert into schema_migrations (filename, sha256, adopted) values (%s, %s, %s) "
        f"on conflict (filename) {conflict}",
        (path.name, _sha256(path), adopted),
    )


def _ensure_admin(cur: psycopg.Cursor, email: str) -> None:
    """Give somebody a way into the dashboard.

    Migrations 0003/0004 seed Kodexo's own addresses, so replaying them at a new
    location produces a dashboard nobody there can sign in to. Done here rather
    than by editing those files: they are history, and rewriting applied
    migrations to suit a later deployment is how a migration set stops being
    trustworthy.
    """
    if not _table_exists(cur, "allowed_users"):
        logger.warning("allowed_users doesn't exist yet; skipping ADMIN_EMAIL")
        return
    cur.execute(
        "insert into allowed_users (email) values (%s) on conflict (email) do nothing",
        (email.strip().lower(),),
    )
    logger.info("allowed_users: ensured %s can sign in", email.strip().lower())


def main() -> int:
    parser = argparse.ArgumentParser(
        prog="python -m worker.migrate",
        description="Bring a Supabase database up to date with supabase/migrations.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="report what would be applied and exit without changing anything",
    )
    parser.add_argument(
        "--adopt",
        action="store_true",
        help=(
            "for a database whose schema already exists but was applied by hand: "
            "record every migration as applied WITHOUT running it"
        ),
    )
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(message)s")

    files = migration_files()
    logger.info("%d migrations in %s", len(files), MIGRATIONS_DIR)

    with psycopg.connect(_db_url(), connect_timeout=30, autocommit=True) as conn:
        with conn.cursor() as cur:
            cur.execute("select current_database(), split_part(version(), ' on ', 1)")
            name, version = cur.fetchone()
            logger.info("connected: %s / %s", name, version)

            tracked = _table_exists(cur, "schema_migrations")
            has_schema = _table_exists(cur, "agents")

        if not tracked and has_schema and not args.adopt:
            logger.error(
                "\nThis database already has an `agents` table but no schema_migrations,\n"
                "so it was set up before this runner existed. Applying 0001 to it would\n"
                "fail on the first CREATE.\n\n"
                "If its schema is already current, adopt it into tracking -- this only\n"
                "writes the bookkeeping table, it runs no DDL:\n\n"
                "    python -m worker.migrate --adopt\n"
            )
            return 1

        state = "TRACKED" if tracked else ("UNTRACKED" if has_schema else "FRESH")
        logger.info("state: %s", state)

        if args.dry_run:
            with conn.cursor() as cur:
                applied: set[str] = set()
                if tracked:
                    cur.execute("select filename from schema_migrations")
                    applied = {r[0] for r in cur.fetchall()}
            pending = [f for f in files if f.name not in applied]
            logger.info("applied: %d", len(applied))
            logger.info("pending: %d", len(pending))
            for f in pending:
                logger.info("  would apply %s", f.name)
            return 0

        # Serialised across processes: a worker pool restarting together would
        # otherwise have several of these running 0001 at once. Session-scoped,
        # so it is released when the connection closes even on a crash.
        with conn.cursor() as cur:
            cur.execute("select pg_advisory_lock(%s)", (_LOCK_KEY,))
            logger.info("advisory lock held")

            _ensure_tracking_table(cur)

            cur.execute("select filename, sha256 from schema_migrations")
            recorded = dict(cur.fetchall())

            if args.adopt:
                for path in files:
                    _record(cur, path, adopted=True)
                logger.info("adopted %d migrations without running them", len(files))
            else:
                # Report drift rather than acting on it: a changed file may be a
                # harmless comment edit or a schema change that never ran, and
                # this cannot tell which.
                for path in files:
                    was = recorded.get(path.name)
                    if was and was != _sha256(path):
                        logger.warning(
                            "%s changed since it was applied -- not re-run", path.name
                        )

                pending = [f for f in files if f.name not in recorded]
                if not pending:
                    logger.info("nothing to apply; database is up to date")
                else:
                    logger.info("applying %d migration(s)", len(pending))

                for path in pending:
                    sql = path.read_text(encoding="utf-8")
                    try:
                        # Its own transaction, so a failure leaves the database
                        # exactly as it was rather than half-migrated.
                        with conn.transaction():
                            cur.execute(sql)
                            _record(cur, path, adopted=False)
                    except Exception as exc:
                        logger.error("FAILED %s: %s", path.name, exc)
                        logger.error(
                            "\nRolled back. Everything before it stayed applied, so fix this\n"
                            "file and run again -- it will resume here."
                        )
                        return 1
                    logger.info("  applied %s", path.name)

            admin = (os.environ.get("ADMIN_EMAIL") or "").strip()
            if admin:
                _ensure_admin(cur, admin)
            elif state == "FRESH":
                logger.warning(
                    "ADMIN_EMAIL not set. This database's allowed_users holds only the "
                    "addresses seeded by migrations 0003/0004, so nobody at this "
                    "deployment can sign in to the dashboard yet."
                )

            cur.execute("select count(*) from schema_migrations")
            logger.info("schema_migrations now holds %d row(s)", cur.fetchone()[0])

    logger.info("done")
    return 0


if __name__ == "__main__":
    sys.exit(main())
