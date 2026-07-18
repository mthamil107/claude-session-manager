"""
CSM SessionEnd hook — event-driven per-session backup.

Reads JSON hook input from stdin:
    {"session_id": "...", "transcript_path": "..."}

Copies the transcript_path to
    <CSM_DIR>/session_backups/<project>/<session-id>/<YYYY-MM-DD_HHMMSS>.jsonl

Only copies if the file's (size, mtime) fingerprint has changed vs
session_backups/index.json — matches CSM's incremental logic exactly.

Exits 0 always (never blocks Claude Code, never surfaces errors to the user).
"""

import json
import shutil
import sys
from datetime import datetime
from pathlib import Path

CSM_DIR       = Path(__file__).parent
BACKUP_DIR    = CSM_DIR / "session_backups"
BACKUP_INDEX  = BACKUP_DIR / "index.json"
MAX_BACKUPS   = 10


def _load_index():
    if BACKUP_INDEX.exists():
        try:
            return json.loads(BACKUP_INDEX.read_text(encoding="utf-8"))
        except Exception:
            return {}
    return {}


def _save_index(index):
    BACKUP_DIR.mkdir(parents=True, exist_ok=True)
    BACKUP_INDEX.write_text(json.dumps(index, indent=2), encoding="utf-8")


def main():
    try:
        raw = sys.stdin.read()
        payload = json.loads(raw) if raw.strip() else {}
    except Exception:
        return 0

    transcript_path = payload.get("transcript_path")
    session_id      = payload.get("session_id")
    if not transcript_path or not session_id:
        return 0
    src = Path(transcript_path)
    if not src.is_file():
        return 0

    project_dir_name = src.parent.name
    try:
        stat = src.stat()
    except Exception:
        return 0
    fingerprint = f"{stat.st_size}:{int(stat.st_mtime)}"

    index = _load_index()
    key   = f"{project_dir_name}/{session_id}"
    if index.get(key, {}).get("fingerprint") == fingerprint:
        return 0

    target_dir = BACKUP_DIR / project_dir_name / session_id
    target_dir.mkdir(parents=True, exist_ok=True)
    ts = datetime.now().strftime("%Y-%m-%d_%H%M%S")
    dest = target_dir / f"{ts}.jsonl"
    try:
        shutil.copy2(src, dest)
    except Exception:
        return 0

    # Prune old snapshots
    try:
        snaps = sorted(target_dir.glob("*.jsonl"), key=lambda p: p.stat().st_mtime)
        while len(snaps) > MAX_BACKUPS:
            old = snaps.pop(0)
            try:
                old.unlink()
            except Exception:
                pass
    except Exception:
        pass

    index[key] = {"fingerprint": fingerprint, "last_backup": ts, "source": str(src)}
    _save_index(index)
    return 0


if __name__ == "__main__":
    sys.exit(main() or 0)
