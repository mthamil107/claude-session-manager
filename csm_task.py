"""
csm-task: fire-and-forget task delegation across registered Claude sessions.

Usage:
    csm-task <alias-or-id> "<prompt>" [--with-context N] [--from <alias>]
                                       [--tools "Read,Edit,Bash"] [--continue]
                                       [--print]

Looks up the target session from sessions.json, optionally bundles context
from a source session's most recent .jsonl, and spawns `claude -p` in a
new Windows Terminal tab pinned to the target project's cwd.
"""

import argparse
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

SCRIPT_DIR = Path(__file__).parent
SESSIONS_FILE = SCRIPT_DIR / "sessions.json"
CLAUDE_HOME = Path.home() / ".claude"
PROJECTS_DIR = CLAUDE_HOME / "projects"


def load_sessions():
    if not SESSIONS_FILE.exists():
        return []
    with open(SESSIONS_FILE, "r", encoding="utf-8") as f:
        return json.load(f).get("sessions", [])


def find_session(query):
    """Find a session by exact alias, exact id, or fuzzy name match."""
    sessions = load_sessions()
    q = query.lower()
    # Exact alias
    for s in sessions:
        if s.get("alias", "").lower() == q:
            return s
    # Exact id
    for s in sessions:
        if s.get("session_id", "") == query:
            return s
    # Fuzzy name/alias contains
    for s in sessions:
        if q in s.get("alias", "").lower() or q in s.get("name", "").lower():
            return s
    return None


def encode_cwd_to_project_dir(cwd):
    """Encode a Windows path the way Claude Code does: : -> --, \\ -> -, _ -> -."""
    # Order matters: handle drive colon first, then backslashes, then underscores
    return cwd.replace(":\\", "--").replace("\\", "-").replace("_", "-").replace("/", "-")


def find_most_recent_jsonl(cwd):
    """Find the most-recently-modified .jsonl file in the encoded project folder for cwd."""
    if not cwd:
        return None
    encoded = encode_cwd_to_project_dir(cwd)
    proj_dir = PROJECTS_DIR / encoded
    if not proj_dir.is_dir():
        return None
    candidates = list(proj_dir.glob("*.jsonl"))
    if not candidates:
        return None
    return max(candidates, key=lambda p: p.stat().st_mtime)


def extract_context_digest(jsonl_path, last_n_turns):
    """Read the last N user/assistant text turns and produce a compact digest."""
    if not jsonl_path or last_n_turns <= 0:
        return ""
    turns = []
    try:
        with open(jsonl_path, "r", encoding="utf-8", errors="ignore") as f:
            for line in f:
                try:
                    rec = json.loads(line)
                except Exception:
                    continue
                rtype = rec.get("type")
                if rtype not in ("user", "assistant"):
                    continue
                msg = rec.get("message", {})
                content = msg.get("content", "")
                if isinstance(content, list):
                    parts = []
                    for c in content:
                        if isinstance(c, dict) and c.get("type") == "text":
                            t = c.get("text", "")
                            if t:
                                parts.append(t)
                    content = "\n".join(parts)
                if not isinstance(content, str):
                    continue
                content = content.strip()
                if not content or content.startswith("<"):
                    continue
                turns.append((rtype, content))
    except Exception:
        return ""

    turns = turns[-last_n_turns:]
    lines = []
    for role, text in turns:
        # Trim each turn to keep the digest compact
        snippet = text if len(text) <= 800 else text[:800] + "..."
        lines.append(f"[{role}]\n{snippet}")
    return "\n\n".join(lines)


def build_prompt(task, context_digest, source_label):
    if not context_digest:
        return task
    header = f"Context from sibling session \"{source_label}\":\n" if source_label else "Context from a sibling session:\n"
    return (
        f"{header}"
        f"---\n"
        f"{context_digest}\n"
        f"---\n\n"
        f"Task to perform in THIS project:\n{task}"
    )


def spawn_task(target_session, final_prompt, tools, use_continue, print_only):
    """Build and spawn the `claude -p` command in a new WT tab (or print it)."""
    cwd = target_session.get("cwd", ".")
    name = target_session.get("name", "Task")
    skip_perms = target_session.get("skip_permissions", False)
    model = (target_session.get("model") or "").strip()

    if not Path(cwd).is_dir():
        print(f"ERROR: Target session cwd does not exist: {cwd}", file=sys.stderr)
        sys.exit(2)

    # Single-quote escape the prompt for PowerShell
    safe_prompt = final_prompt.replace("'", "''")
    cmd_parts = ["claude", "-p", f"'{safe_prompt}'"]
    if use_continue:
        cmd_parts.append("--continue")
    elif target_session.get("session_id"):
        # Best effort: resume the target session's conversation if no --continue
        # (commented out by default; -p creates a fresh ephemeral context unless --continue or --resume)
        pass
    if skip_perms:
        cmd_parts.append("--dangerously-skip-permissions")
    if model:
        cmd_parts.extend(["--model", model])
    if tools:
        cmd_parts.extend(["--allowedTools", f'"{tools}"'])

    cmd_str = " ".join(cmd_parts)
    title_text = f"Task :: {name}"

    if print_only:
        print(f"# Would launch in: {cwd}")
        print(f"# Title: {title_text}")
        print(cmd_str)
        return

    wt_path = shutil.which("wt") or shutil.which("wt.exe")
    if wt_path:
        wt_args = [
            wt_path, "-w", "0", "new-tab",
            "--title", title_text,
            "-d", cwd,
            "powershell", "-NoExit", "-Command", cmd_str,
        ]
        subprocess.Popen(wt_args)
    else:
        subprocess.Popen(
            ["powershell", "-NoExit", "-Command", f'cd "{cwd}"; {cmd_str}'],
            creationflags=subprocess.CREATE_NEW_CONSOLE,
        )

    print(f"Task dispatched to '{name}' in {cwd}")


def main():
    p = argparse.ArgumentParser(prog="csm-task",
        description="Delegate a task to another registered Claude session via `claude -p`.")
    p.add_argument("target", help="Target session alias, id, or fuzzy name match")
    p.add_argument("prompt", help="Task prompt for the target session's Claude")
    p.add_argument("--with-context", type=int, default=0, metavar="N",
                   help="Bundle the last N user/assistant turns from the source session")
    p.add_argument("--from", dest="source", default=None,
                   help="Source session alias/id for context. Defaults to current dir's most-recent .jsonl")
    p.add_argument("--tools", default="Read,Edit,Bash",
                   help='Allowed tools for the spawned task (default: "Read,Edit,Bash")')
    p.add_argument("--continue", dest="cont", action="store_true",
                   help="Append to the target session's existing conversation (--continue)")
    p.add_argument("--print", action="store_true",
                   help="Don't launch — just print the command that would run")
    args = p.parse_args()

    target = find_session(args.target)
    if not target:
        print(f"ERROR: No session found matching '{args.target}'", file=sys.stderr)
        print("Available sessions:", file=sys.stderr)
        for s in load_sessions():
            print(f"  {s.get('alias',''):<18} {s.get('name','')}", file=sys.stderr)
        sys.exit(1)

    # Resolve source for context
    digest = ""
    source_label = ""
    if args.with_context > 0:
        if args.source:
            source = find_session(args.source)
            if not source:
                print(f"WARN: --from '{args.source}' not found in sessions.json. Skipping context.",
                      file=sys.stderr)
            else:
                jsonl = find_most_recent_jsonl(source.get("cwd", ""))
                if jsonl:
                    digest = extract_context_digest(jsonl, args.with_context)
                    source_label = source.get("name", "")
        else:
            # Auto-detect: current working directory
            jsonl = find_most_recent_jsonl(os.getcwd())
            if jsonl:
                digest = extract_context_digest(jsonl, args.with_context)
                source_label = f"cwd: {os.getcwd()}"

    final_prompt = build_prompt(args.prompt, digest, source_label)
    spawn_task(target, final_prompt, args.tools, args.cont, args.print)


if __name__ == "__main__":
    main()
