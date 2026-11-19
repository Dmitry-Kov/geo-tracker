# -*- coding: utf-8 -*-
"""Scheduled online backups: a live WAL database is copied whole, every copy
is verified before rotation, pre-migration snapshots in backups/ are never
touched, and a failed verification deletes only the bad copy. CLI contract:
exit 0 + one "Backup OK:" line, exit 1 on bad input, --help without queries.py.
"""
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

from app.backup import (DEFAULT_KEEP, main, prune_backups, run_backup,
                        verify_sqlite_backup)
from app.db import backup_database

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture(autouse=True)
def env(monkeypatch):
    # conftest already switches load_env off; keep CLI defaults deterministic
    for var in ("GEO_DB_PATH", "BACKUP_KEEP"):
        monkeypatch.delenv(var, raising=False)


def make_db(path, rows=1):
    conn = sqlite3.connect(str(path))
    try:
        conn.execute("CREATE TABLE t (x TEXT)")
        conn.executemany("INSERT INTO t VALUES (?)",
                         [(f"row {i}",) for i in range(rows)])
        conn.commit()
    finally:
        conn.close()
    return path


def seed_backup(path):
    """A valid SQLite copy at an exact (timestamped) path."""
    return make_db(path)


def test_online_backup_of_live_wal_db_captures_wal_rows(tmp_path):
    db = tmp_path / "live.db"
    writer = sqlite3.connect(str(db))
    try:
        writer.execute("PRAGMA journal_mode=WAL")
        writer.execute("PRAGMA wal_autocheckpoint=0")  # rows stay in the WAL
        writer.execute("CREATE TABLE t (x INTEGER)")
        writer.execute("INSERT INTO t VALUES (1)")
        writer.commit()
        writer.execute("INSERT INTO t VALUES (2)")
        writer.commit()
        assert (tmp_path / "live.db-wal").exists()  # not checkpointed yet

        res = run_backup(db)  # writer still open — online backup API
        verify_sqlite_backup(res["backup"])
        conn = sqlite3.connect(str(res["backup"]))
        try:
            assert conn.execute("SELECT x FROM t ORDER BY x").fetchall() == \
                [(1,), (2,)]
        finally:
            conn.close()
        assert res["size_bytes"] == res["backup"].stat().st_size
    finally:
        writer.close()


def test_scheduled_backups_stay_separate_from_pre_migration_snapshots(tmp_path):
    db = make_db(tmp_path / "svc.db")
    pre = backup_database(db)  # default dest: backups/ next to the DB
    assert pre.parent == tmp_path / "backups"
    pre_bytes = pre.read_bytes()
    sched = tmp_path / "backups" / "scheduled"
    sched.mkdir(parents=True)
    seed_backup(sched / "svc-2000-01-01T00-00-00-000000Z.db")

    res = run_backup(db, keep=1)

    assert res["backup"].parent == sched
    assert [p.name for p in sched.glob("svc-*.db")] == [res["backup"].name]
    assert res["pruned"] == [sched / "svc-2000-01-01T00-00-00-000000Z.db"]
    # the pre-migration snapshot is untouched even though keep=1
    assert pre.read_bytes() == pre_bytes
    assert sorted(p.name for p in (tmp_path / "backups").iterdir()) == \
        sorted([pre.name, "scheduled"])


def test_rotation_keeps_newest_and_spares_unrelated_files(tmp_path):
    db = make_db(tmp_path / "app.db")
    sched = tmp_path / "backups" / "scheduled"
    sched.mkdir(parents=True)
    old1 = seed_backup(sched / "app-2000-01-01T00-00-00-000000Z.db")
    old2 = seed_backup(sched / "app-2000-01-02T00-00-00-000000Z.db")
    notes = sched / "notes.txt"
    notes.write_text("keep me", encoding="utf-8")
    other = sched / "other-2000-01-01T00-00-00-000000Z.db"
    other.write_text("not ours", encoding="utf-8")
    decoy_dir = sched / "app-2000-01-03T00-00-00-000000Z.db"
    decoy_dir.mkdir()  # matches the pattern but is a directory: never touched

    r1 = run_backup(db, keep=2)   # 3 copies → oldest goes
    r2 = run_backup(db, keep=2)   # 3 copies → next oldest goes

    assert r1["pruned"] == [old1]
    assert r2["pruned"] == [old2]
    assert r1["backup"].name != r2["backup"].name
    assert sorted(p.name for p in sched.glob("app-*.db") if p.is_file()) == \
        sorted([r1["backup"].name, r2["backup"].name])
    assert notes.read_text(encoding="utf-8") == "keep me"
    assert other.read_text(encoding="utf-8") == "not ours"
    assert decoy_dir.is_dir()


def test_prune_always_protects_the_given_backup(tmp_path):
    sched = tmp_path / "scheduled"
    sched.mkdir()
    oldest = seed_backup(sched / "app-2000-01-01T00-00-00-000000Z.db")
    middle = seed_backup(sched / "app-2000-01-02T00-00-00-000000Z.db")
    newest = seed_backup(sched / "app-2000-01-03T00-00-00-000000Z.db")

    removed = prune_backups(sched, "app", ".db", keep=1, protect=oldest)

    assert removed == [middle]
    assert oldest.exists() and newest.exists()


def test_failed_verification_deletes_bad_copy_without_pruning(tmp_path, monkeypatch):
    db = make_db(tmp_path / "bad.db")
    sched = tmp_path / "backups" / "scheduled"
    sched.mkdir(parents=True)
    older = seed_backup(sched / "bad-2000-01-01T00-00-00-000000Z.db")

    def boom(path):
        raise RuntimeError("backup integrity check failed: boom")

    monkeypatch.setattr("app.backup.verify_sqlite_backup", boom)
    with pytest.raises(RuntimeError, match="integrity check failed"):
        run_backup(db, keep=1)

    # the bad copy is gone; the older valid one survives despite keep=1
    assert list(sched.iterdir()) == [older]


def test_verify_rejects_corrupted_and_non_sqlite_files(tmp_path):
    garbage = tmp_path / "garbage.db"
    garbage.write_bytes(b"this is not a sqlite database\n" * 64)
    with pytest.raises(RuntimeError, match="integrity check failed"):
        verify_sqlite_backup(garbage)

    # a real SQLite file whose first leaf page is wiped: integrity_check
    # returns error rows instead of ("ok",)
    db = make_db(tmp_path / "corrupt.db", rows=300)
    data = bytearray(db.read_bytes())
    page_size = int.from_bytes(data[16:18], "big")
    leaf = next(i for i in range(1, len(data) // page_size)
                if data[i * page_size] == 0x0D)
    off = leaf * page_size
    data[off + page_size // 2:off + page_size] = b"\x00" * (page_size // 2)
    db.write_bytes(data)
    with pytest.raises(RuntimeError, match="integrity check failed"):
        verify_sqlite_backup(db)


def test_missing_db_raises_and_creates_no_directories(tmp_path, capsys):
    missing = tmp_path / "nope.db"
    with pytest.raises(FileNotFoundError):
        run_backup(missing)
    assert not (tmp_path / "backups").exists()  # nothing created at all

    assert main(["--db", str(missing)]) == 1
    assert "Backup failed" in capsys.readouterr().err
    assert not (tmp_path / "backups").exists()


def test_keep_below_one_is_rejected(tmp_path, capsys):
    db = make_db(tmp_path / "k.db")
    for keep in (0, -1):
        with pytest.raises(ValueError):
            run_backup(db, keep=keep)
    assert not (tmp_path / "backups").exists()  # validated before any write

    assert main(["--db", str(db), "--keep", "0"]) == 1
    assert "keep" in capsys.readouterr().err
    assert not (tmp_path / "backups").exists()


def test_cli_success_prints_backup_ok(tmp_path, capsys):
    db = make_db(tmp_path / "cli.db")
    assert main(["--db", str(db), "--keep", "2"]) == 0
    out = capsys.readouterr().out
    assert "Backup OK:" in out and "pruned 0" in out
    copies = list((tmp_path / "backups" / "scheduled").glob("cli-*.db"))
    assert len(copies) == 1
    verify_sqlite_backup(copies[0])
    assert str(copies[0]) in out

    # a second run rotates within the same directory
    assert main(["--db", str(db), "--keep", "1"]) == 0
    out = capsys.readouterr().out
    assert "pruned 1" in out
    assert len(list((tmp_path / "backups" / "scheduled").glob("cli-*.db"))) == 1


def test_cli_dest_override_and_env_defaults(tmp_path, capsys, monkeypatch):
    db = make_db(tmp_path / "env.db")
    custom = tmp_path / "elsewhere"
    assert main(["--db", str(db), "--dest", str(custom)]) == 0
    capsys.readouterr()
    assert len(list(custom.glob("env-*.db"))) == 1
    assert not (tmp_path / "backups").exists()

    monkeypatch.setenv("GEO_DB_PATH", str(db))
    monkeypatch.setenv("BACKUP_KEEP", "1")
    assert main([]) == 0
    assert "Backup OK:" in capsys.readouterr().out
    assert len(list((tmp_path / "backups" / "scheduled").glob("env-*.db"))) == 1


def module_as_main_code(tmp_path, argv):
    """Run app.backup as __main__ (what `python -m` does) with queries.py
    blocked and .env lookup redirected to an empty dir."""
    return (
        "import sys\n"
        "class Block:\n"
        "    def find_spec(self, name, path=None, target=None):\n"
        "        if name == 'queries':\n"
        "            raise ModuleNotFoundError(\"No module named 'queries'\")\n"
        "sys.meta_path.insert(0, Block())\n"
        f"sys.path.insert(0, {str(ROOT)!r})\n"
        "from pathlib import Path\n"
        "import geo_tracker as gt\n"
        f"gt.BASE_DIR = Path({str(tmp_path)!r})\n"  # no .env there
        "import runpy\n"
        f"sys.argv = {argv!r}\n"
        "try:\n"
        "    runpy.run_module('app.backup', run_name='__main__')\n"
        "except SystemExit as e:\n"
        "    print('EXIT', e.code)\n")


def test_module_help_works_without_queries_py(tmp_path):
    code = module_as_main_code(tmp_path, ["app.backup", "--help"])
    p = subprocess.run([sys.executable, "-c", code], capture_output=True,
                       text=True, cwd=tmp_path)
    assert p.returncode == 0, p.stderr
    assert "EXIT 0" in p.stdout
    assert "--db" in p.stdout and "--keep" in p.stdout and "--dest" in p.stdout
    assert "Traceback" not in p.stderr


def test_module_runs_a_backup_without_queries_py(tmp_path):
    """The __main__ path end to end in a fresh process: exit 0, one line,
    a verified copy on disk — without queries.py and without the real .env."""
    db = make_db(tmp_path / "m.db")
    code = module_as_main_code(tmp_path, ["app.backup", "--db", str(db),
                                          "--keep", "2"])
    p = subprocess.run([sys.executable, "-c", code], capture_output=True,
                       text=True, cwd=tmp_path)
    assert p.returncode == 0, p.stderr
    assert "EXIT 0" in p.stdout and "Backup OK:" in p.stdout
    copies = list((tmp_path / "backups" / "scheduled").glob("m-*.db"))
    assert len(copies) == 1
    verify_sqlite_backup(copies[0])


def test_default_keep_is_fourteen():
    assert DEFAULT_KEEP == 14