"""Consistent SQLite backups for the web app and Windows launch script."""

from __future__ import annotations

import argparse
import json
import os
import shutil
import sqlite3
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent
DEFAULT_DB = ROOT / "data" / "pcba.db"


def mirror_directory() -> Path | None:
    """An optional second disk or network share, read by both CLI and web server."""
    configured = os.getenv("PCBA_BACKUP_MIRROR_DIR", "").strip()
    if not configured:
        env_file = ROOT / "server" / ".env"
        if env_file.is_file():
            for line in env_file.read_text(encoding="utf-8-sig").splitlines():
                if line.lstrip().startswith("#") or "=" not in line:
                    continue
                key, value = line.split("=", 1)
                if key.strip() == "PCBA_BACKUP_MIRROR_DIR":
                    configured = value.strip().strip('"\'')
                    break
    return Path(configured).expanduser() if configured else None


def backup_status(db_path: Path = DEFAULT_DB) -> dict:
    status_file = Path(db_path).parent / "backups" / "backup_status.json"
    if status_file.is_file():
        try:
            status = json.loads(status_file.read_text(encoding="utf-8"))
            if isinstance(status, dict):
                return status
        except (OSError, ValueError):
            pass
    return {}


def backup_runtime_status(db_path: Path = DEFAULT_DB, *, stale_after_hours: int = 36) -> dict:
    """Report actual local backup freshness and free space, not only saved metadata.

    A daily backup can be up to 36 hours old around the 02:00 run.  This
    check does not re-read each SQLite backup; integrity is checked when it is
    created and again before a restore.
    """
    db_path = Path(db_path)
    backup_dir = db_path.parent / "backups"
    candidates = []
    if backup_dir.is_dir():
        for path in backup_dir.glob("pcba_*.db"):
            try:
                candidates.append((path.stat().st_mtime, path))
            except OSError:  # A backup may be pruned during this health check.
                continue
    latest = max(candidates, key=lambda candidate: candidate[0], default=None)
    age_hours = None
    last_backup_at = None
    local_status = "missing"
    if latest is not None:
        modified = datetime.fromtimestamp(latest[0], timezone.utc)
        age_hours = round(max(0.0, (datetime.now(timezone.utc) - modified).total_seconds() / 3600), 1)
        last_backup_at = modified.isoformat(timespec="seconds")
        local_status = "stale" if age_hours > stale_after_hours else "fresh"

    disk_free_gib = None
    disk_low = None
    try:
        free_bytes = shutil.disk_usage(db_path.parent).free
        database_bytes = db_path.stat().st_size if db_path.is_file() else 0
        disk_free_gib = round(free_bytes / 1024**3, 2)
        disk_low = free_bytes < max(1024**3, 3 * database_bytes)
    except OSError:
        pass
    return {"last_local_backup_at": last_backup_at,
            "last_local_filename": latest[1].name if latest else None,
            "local_status": local_status, "local_age_hours": age_hours,
            "local_stale_after_hours": stale_after_hours,
            "disk_free_gib": disk_free_gib, "disk_low": disk_low}


def save_backup_status(db_path: Path, status: dict) -> None:
    status_file = Path(db_path).parent / "backups" / "backup_status.json"
    temporary = status_file.with_suffix(".tmp")
    temporary.write_text(json.dumps(status, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(temporary, status_file)


def backup_database(db_path: Path = DEFAULT_DB, keep: int = 30) -> Path:
    db_path = Path(db_path)
    if not db_path.exists():
        raise FileNotFoundError(db_path)
    backup_dir = db_path.parent / "backups"
    backup_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
    destination = backup_dir / f"pcba_{stamp}.db"
    try:
        with closing(sqlite3.connect(db_path)) as source, closing(sqlite3.connect(destination)) as target, source, target:
            source.backup(target)
            result = target.execute("PRAGMA integrity_check").fetchone()[0]
            if result != "ok":
                raise RuntimeError(f"Backup integrity check failed: {result}")
    except Exception:
        destination.unlink(missing_ok=True)
        failed = backup_status(db_path)
        failed["last_local_failure_at"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
        try:
            save_backup_status(db_path, failed)
        except OSError:
            pass
        raise
    for stale in sorted(backup_dir.glob("pcba_*.db"), reverse=True)[keep:]:
        stale.unlink()
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    previous = backup_status(db_path)
    mirror = mirror_directory()
    status = {"last_local_backup_at": now, "last_local_filename": destination.name,
              "last_local_failure_at": None,
              "mirror_configured": mirror is not None, "mirror_ok": None,
              "last_mirror_success_at": previous.get("last_mirror_success_at")}
    if mirror is not None:
        copied = mirror / destination.name
        temporary = mirror / (destination.name + ".part")
        try:
            if mirror.resolve() == backup_dir.resolve():
                raise ValueError("异地备份目录不能与本机备份目录相同")
            mirror.mkdir(parents=True, exist_ok=True)
            shutil.copy2(destination, temporary)
            with closing(sqlite3.connect(temporary)) as check:
                result = check.execute("PRAGMA integrity_check").fetchone()[0]
                if result != "ok":
                    raise RuntimeError(f"镜像备份完整性检查失败：{result}")
            os.replace(temporary, copied)
            for stale in sorted(mirror.glob("pcba_*.db"), reverse=True)[keep:]:
                stale.unlink()
            status["mirror_ok"] = True
            status["last_mirror_success_at"] = now
        except Exception as exc:
            try:
                temporary.unlink(missing_ok=True)
            except OSError:
                pass
            status["mirror_ok"] = False
            status["mirror_error"] = str(exc)
            print(f"PCBA mirror backup failed (local backup is intact): {exc}", flush=True)
    try:
        save_backup_status(db_path, status)
    except OSError as exc:
        print(f"PCBA backup status could not be saved: {exc}", flush=True)
    return destination


def main() -> None:
    parser = argparse.ArgumentParser(description="Back up the PCBA SQLite database")
    parser.add_argument("--db", type=Path, default=DEFAULT_DB)
    parser.add_argument("--keep", type=int, default=30)
    args = parser.parse_args()
    print(backup_database(args.db, args.keep))


if __name__ == "__main__":
    main()
