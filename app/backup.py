# -*- coding: utf-8 -*-
"""Scheduled online SQLite backups with integrity verification and rotation.

Runnable as `python -m app.backup` (e.g. from a host cron against the Docker
container). Every copy is taken with SQLite's online backup API, verified
with `PRAGMA integrity_check`, and only then are older copies pruned — a
failed verification deletes the bad copy and keeps the existing ones.

Rotating copies live in backups/scheduled/ next to the DB, separate from the
pre-migration snapshots in backups/, which are never pruned here.
"""
from __future__ import annotations

import argparse
import os
import sqlite3
import sys
from pathlib import Path

import geo_tracker as gt
from app.db import BASE_DIR, backup_database

DEFAULT_KEEP = 14


def verify_sqlite_backup(path: Path) -> None:
    """Raise RuntimeError unless the copy passes PRAGMA integrity_check."""
    conn = sqlite3.connect(str(path))
    try:
        rows = conn.execute("PRAGMA integrity_check").fetchall()
    except sqlite3.Error as exc:
        # e.g. the file is not a SQLite database at all
        raise RuntimeError(f"backup integrity check failed: {exc}") from exc
    finally:
        conn.close()
    if rows != [("ok",)]:
        raise RuntimeError(f"backup integrity check failed: {rows}")


def prune_backups(dest_dir: Path, stem: str, suffix: str, keep: int,
                  *, protect: Path = None) -> list[Path]:
    """Keep the newest `keep` files named `{stem}-*{suffix}` in dest_dir
    (UTC timestamps sort chronologically by name), always keeping `protect`,
    and unlink the rest. Returns the removed paths. Files outside dest_dir
    and files not matching the pattern are never touched."""
    files = sorted((p for p in dest_dir.glob(f"{stem}-*{suffix}") if p.is_file()),
                   key=lambda p: p.name, reverse=True)
    kept = set(files[:keep])
    if protect is not None:
        kept.add(protect)
    removed = []
    for p in files:
        if p not in kept:
            p.unlink()
            removed.append(p)
    return removed


def run_backup(db_path: Path, *, keep: int = DEFAULT_KEEP,
               dest_dir: Path = None) -> dict:
    """Back up, verify, then rotate. On a failed verification the bad copy
    is deleted and the error re-raised WITHOUT pruning older backups."""
    if keep < 1:
        raise ValueError(f"keep must be >= 1, got {keep}")
    db_path = Path(db_path)
    if not db_path.is_file():
        raise FileNotFoundError(f"database not found: {db_path}")
    if dest_dir is None:
        dest_dir = db_path.parent / "backups" / "scheduled"
    backup_path = backup_database(db_path, dest_dir=dest_dir)
    try:
        verify_sqlite_backup(backup_path)
    except Exception:
        if backup_path.exists():
            backup_path.unlink()
        raise
    pruned = prune_backups(dest_dir, db_path.stem, db_path.suffix,
                           keep=keep, protect=backup_path)
    return {"backup": backup_path, "pruned": pruned,
            "size_bytes": backup_path.stat().st_size}


def main(argv=None) -> int:
    gt.load_env()  # here only, never on import (tests switch it off)
    parser = argparse.ArgumentParser(
        prog="python -m app.backup",
        description="Online SQLite backup with integrity check and rotation.")
    parser.add_argument("--db", type=Path,
                        default=Path(os.environ.get("GEO_DB_PATH")
                                     or BASE_DIR / "geo_tracker.db"),
                        help="database file (default: GEO_DB_PATH or "
                             "geo_tracker.db next to the app)")
    parser.add_argument("--keep", type=int,
                        default=int(os.environ.get("BACKUP_KEEP") or DEFAULT_KEEP),
                        help=f"copies to keep (default: BACKUP_KEEP or {DEFAULT_KEEP})")
    parser.add_argument("--dest", type=Path, default=None,
                        help="backup directory (default: backups/scheduled/ "
                             "next to the database)")
    args = parser.parse_args(argv)
    try:
        res = run_backup(args.db, keep=args.keep, dest_dir=args.dest)
    except (FileNotFoundError, ValueError, RuntimeError, sqlite3.Error) as exc:
        print(f"Backup failed: {exc}", file=sys.stderr)
        return 1
    print(f"Backup OK: {res['backup']} ({res['size_bytes']} bytes, "
          f"pruned {len(res['pruned'])})")
    return 0


if __name__ == "__main__":
    sys.exit(main())