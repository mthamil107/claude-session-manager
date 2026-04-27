"""
Enable persistent tab titles in Windows Terminal so CSM's `--title` sticks
instead of being overwritten by Claude Code's built-in "Claude Code" label.

What it does:
  1. Backs up settings.json next to itself
  2. Sets profiles.defaults.suppressApplicationTitle = true
     (applies to all profiles, including PowerShell)
  3. Reports what changed

Safe to re-run; idempotent.
"""

import json
import shutil
import sys
from datetime import datetime
from pathlib import Path

PACKAGED = Path.home() / "AppData/Local/Packages/Microsoft.WindowsTerminal_8wekyb3d8bbwe/LocalState/settings.json"
UNPACKAGED = Path.home() / "AppData/Local/Microsoft/Windows Terminal/settings.json"
PREVIEW = Path.home() / "AppData/Local/Packages/Microsoft.WindowsTerminalPreview_8wekyb3d8bbwe/LocalState/settings.json"


def find_settings():
    for p in (PACKAGED, PREVIEW, UNPACKAGED):
        if p.exists():
            return p
    return None


def main():
    settings_path = find_settings()
    if not settings_path:
        print("ERROR: Could not find Windows Terminal settings.json in any known location:",
              file=sys.stderr)
        for p in (PACKAGED, PREVIEW, UNPACKAGED):
            print(f"  {p}", file=sys.stderr)
        sys.exit(1)

    print(f"Found settings: {settings_path}")

    with open(settings_path, "r", encoding="utf-8") as f:
        text = f.read()

    try:
        data = json.loads(text)
    except json.JSONDecodeError as e:
        print(f"ERROR: settings.json is not valid JSON: {e}", file=sys.stderr)
        sys.exit(2)

    profiles = data.setdefault("profiles", {})
    defaults = profiles.setdefault("defaults", {})
    current = defaults.get("suppressApplicationTitle")

    if current is True:
        print("Already enabled — no changes needed. (suppressApplicationTitle=true in profiles.defaults)")
        return

    # Back up
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    backup = settings_path.with_suffix(f".json.csm-backup-{stamp}")
    shutil.copy2(settings_path, backup)
    print(f"Backup written: {backup}")

    defaults["suppressApplicationTitle"] = True
    with open(settings_path, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=4, ensure_ascii=False)

    print("Enabled: profiles.defaults.suppressApplicationTitle = true")
    print()
    print("Open a fresh Windows Terminal tab to see the change. Existing tabs keep their old behavior.")


if __name__ == "__main__":
    main()
