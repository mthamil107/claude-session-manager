"""CLI companion for quick session launch: csm <alias>"""

import json
import os
import subprocess
import sys
from pathlib import Path

SESSIONS_FILE = Path(__file__).parent / "sessions.json"

def load_sessions():
    with open(SESSIONS_FILE, "r") as f:
        return json.load(f).get("sessions", [])

def find_session(query):
    sessions = load_sessions()
    query = query.lower()
    # Exact alias match
    for s in sessions:
        if s.get("alias", "").lower() == query:
            return s
    # Fuzzy match on name/alias
    for s in sessions:
        if query in s.get("alias", "").lower() or query in s.get("name", "").lower():
            return s
    return None

def main():
    if len(sys.argv) < 2:
        print("Usage: csm <alias> [--no-remote] [--no-skip]")
        sys.exit(1)

    alias = sys.argv[1]
    no_remote = "--no-remote" in sys.argv
    no_skip = "--no-skip" in sys.argv

    if alias == "list":
        sessions = load_sessions()
        print(f"\n{'Name':<25} {'Alias':<15} {'Directory'}")
        print("-" * 70)
        for s in sessions:
            print(f"{s['name']:<25} {s.get('alias',''):<15} {s.get('cwd','')}")
        return

    session = find_session(alias)
    if not session:
        print(f"No session matching '{alias}' found.")
        print("\nAvailable sessions:")
        for s in load_sessions():
            print(f"  {s.get('alias',''):<15} - {s['name']}")
        sys.exit(1)

    cmd_parts = ["claude", "--resume", session["session_id"]]

    if session.get("skip_permissions") and not no_skip:
        cmd_parts.append("--dangerously-skip-permissions")

    if not no_remote:
        cmd_parts.extend(["--remote-control", f'"{session["name"]}"'])

    cmd_str = " ".join(cmd_parts)
    cwd = session.get("cwd", ".")

    print(f"Launching: {session['name']}")
    print(f"Directory: {cwd}")
    print(f"Remote Control: {'ON' if not no_remote else 'OFF'}")
    print(f"Command: {cmd_str}")
    print()

    ps_command = f'cd "{cwd}"; {cmd_str}'
    subprocess.Popen(
        ["powershell", "-NoExit", "-Command", ps_command],
        creationflags=subprocess.CREATE_NEW_CONSOLE
    )

if __name__ == "__main__":
    main()
