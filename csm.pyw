"""
Claude Session Manager (CSM) - Desktop Application
Notepad++-style professional interface for managing Claude Code sessions.
"""

import ctypes
import json
import os
import shutil
import subprocess
import sys
import threading
import tkinter as tk
from tkinter import ttk, messagebox, filedialog
from pathlib import Path
from datetime import datetime

import pricing as _pricing

# --- Windows DPI Awareness (fixes blurry/pixelated text) ---
try:
    ctypes.windll.shcore.SetProcessDpiAwareness(2)  # Per-monitor DPI aware
except Exception:
    try:
        ctypes.windll.user32.SetProcessDPIAware()
    except Exception:
        pass

# --- Config ---
SCRIPT_DIR = Path(__file__).parent
SESSIONS_FILE = SCRIPT_DIR / "sessions.json"
CLAUDE_HOME = Path.home() / ".claude"
PROJECTS_DIR = CLAUDE_HOME / "projects"
BACKUP_DIR = SCRIPT_DIR / "session_backups"
BACKUP_INDEX = BACKUP_DIR / "index.json"
MAX_BACKUPS_PER_SESSION = 10

# (Display label, model id passed to `claude --model`). "" = no flag (use Claude Code default).
MODEL_CHOICES = [
    ("Default (Claude Code chooses)", ""),
    ("Opus 4.7",    "claude-opus-4-7"),
    ("Opus 4.6",    "claude-opus-4-6"),
    ("Sonnet 4.6",  "claude-sonnet-4-6"),
    ("Haiku 4.5",   "claude-haiku-4-5-20251001"),
]
MODEL_LABEL_BY_ID = {mid: label for label, mid in MODEL_CHOICES}
MODEL_ID_BY_LABEL = {label: mid for label, mid in MODEL_CHOICES}

# --- Notepad++ Dark Theme Palette ---
C = {
    "bg":           "#1e1e1e",
    "bg_light":     "#252526",
    "bg_lighter":   "#2d2d2d",
    "bg_toolbar":   "#333333",
    "bg_menubar":   "#2d2d2d",
    "bg_tab_active":"#1e1e1e",
    "bg_tab_idle":  "#2d2d2d",
    "bg_input":     "#3c3c3c",
    "bg_status":    "#007acc",
    "bg_status_seg":"#16825d",
    "border":       "#3f3f3f",
    "border_light": "#4a4a4a",
    "text":         "#cccccc",
    "text_bright":  "#e8e8e8",
    "text_dim":     "#808080",
    "text_white":   "#ffffff",
    "select_bg":    "#264f78",
    "select_fg":    "#ffffff",
    "accent":       "#007acc",
    "accent_green": "#16825d",
    "accent_orange":"#cd9731",
    "accent_red":   "#c72e2f",
    "btn_hover":    "#3e3e3e",
    "toolbar_sep":  "#4a4a4a",
    "heading_bg":   "#333333",
}


# --- Session Data ---

def load_sessions():
    if SESSIONS_FILE.exists():
        with open(SESSIONS_FILE, "r") as f:
            return json.load(f).get("sessions", [])
    return []


def save_sessions(sessions):
    with open(SESSIONS_FILE, "w") as f:
        json.dump({"sessions": sessions}, f, indent=2)


# --- Session Backup ---

def _load_backup_index():
    if BACKUP_INDEX.exists():
        try:
            with open(BACKUP_INDEX, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            return {}
    return {}


def _save_backup_index(index):
    BACKUP_DIR.mkdir(parents=True, exist_ok=True)
    with open(BACKUP_INDEX, "w", encoding="utf-8") as f:
        json.dump(index, f, indent=2)


def backup_all_sessions(progress_cb=None):
    """Copy every .jsonl in .claude/projects/ to session_backups, only if changed.
    Returns (new_backups_count, skipped_count, total_size_bytes)."""
    if not PROJECTS_DIR.exists():
        return (0, 0, 0)
    BACKUP_DIR.mkdir(parents=True, exist_ok=True)
    index = _load_backup_index()
    new_count = 0
    skipped = 0
    total_size = 0
    timestamp = datetime.now().strftime("%Y-%m-%d_%H%M%S")

    for proj_dir in PROJECTS_DIR.iterdir():
        if not proj_dir.is_dir():
            continue
        for jsonl in proj_dir.glob("*.jsonl"):
            try:
                stat = jsonl.stat()
            except Exception:
                continue
            session_id = jsonl.stem
            key = f"{proj_dir.name}/{session_id}"
            fingerprint = f"{stat.st_size}:{int(stat.st_mtime)}"
            last = index.get(key, {}).get("fingerprint")
            if last == fingerprint:
                skipped += 1
                continue

            # New or changed — copy
            target_dir = BACKUP_DIR / proj_dir.name / session_id
            target_dir.mkdir(parents=True, exist_ok=True)
            target_file = target_dir / f"{timestamp}.jsonl"
            try:
                shutil.copy2(jsonl, target_file)
                new_count += 1
                total_size += stat.st_size
                if progress_cb:
                    progress_cb(f"Backed up {session_id[:16]}... ({stat.st_size // 1024} KB)")

                # Prune old backups beyond the limit
                backups = sorted(target_dir.glob("*.jsonl"), key=lambda p: p.stat().st_mtime)
                while len(backups) > MAX_BACKUPS_PER_SESSION:
                    old = backups.pop(0)
                    try:
                        old.unlink()
                    except Exception:
                        pass

                index[key] = {
                    "fingerprint": fingerprint,
                    "last_backup": timestamp,
                    "source": str(jsonl),
                }
            except Exception as e:
                if progress_cb:
                    progress_cb(f"Failed to backup {session_id[:16]}: {e}")

    _save_backup_index(index)
    return (new_count, skipped, total_size)


def list_session_backups(session_id):
    """Return list of backup file paths for a given session_id, newest first."""
    result = []
    if not BACKUP_DIR.exists():
        return result
    for proj_dir in BACKUP_DIR.iterdir():
        if not proj_dir.is_dir():
            continue
        sess_dir = proj_dir / session_id
        if sess_dir.is_dir():
            for f in sess_dir.glob("*.jsonl"):
                result.append(f)
    result.sort(key=lambda p: p.stat().st_mtime, reverse=True)
    return result


# --- Prompt extraction (for View Prompts dialog) ---

_PROMPT_SKIP_PREFIXES = (
    "this session is being continued",
    "caveat: the messages below",
    "[request interrupted",
)


def format_last_used(mtime):
    """Render a file mtime (epoch float) as a friendly 'when' string."""
    if not mtime:
        return ""
    from datetime import datetime as _dt
    try:
        dt = _dt.fromtimestamp(mtime)
    except Exception:
        return ""
    now = _dt.now()
    delta = now - dt
    secs = delta.total_seconds()
    if secs < 60:
        return "just now"
    if secs < 3600:
        m = int(secs // 60)
        return f"{m} min ago"
    if secs < 86400:
        h = int(secs // 3600)
        return f"{h}h ago"
    if secs < 86400 * 7:
        d = int(secs // 86400)
        return f"{d}d ago"
    if secs < 86400 * 30:
        w = int(secs // (86400 * 7))
        return f"{w}w ago"
    # Older than a month: show absolute date
    return dt.strftime("%Y-%m-%d")


def extract_user_prompts(jsonl_path):
    """Walk a .jsonl and return a list of {idx, timestamp, text} dicts — one per
    real user prompt. Skips Claude Code's auto-generated noise (continuation
    markers, IDE caveats, tool-result echoes, system tags)."""
    prompts = []
    idx = 0
    try:
        with open(jsonl_path, "r", encoding="utf-8", errors="ignore") as f:
            for line in f:
                try:
                    rec = json.loads(line)
                except Exception:
                    continue
                if rec.get("type") != "user":
                    continue
                msg = rec.get("message", {})
                content = msg.get("content", "")
                # User messages can be string, or list of typed blocks (text, tool_result, image, ...)
                if isinstance(content, list):
                    parts = []
                    saw_tool_result = False
                    for c in content:
                        if not isinstance(c, dict):
                            continue
                        if c.get("type") == "tool_result":
                            saw_tool_result = True
                            continue
                        if c.get("type") == "text":
                            t = c.get("text", "")
                            if t:
                                parts.append(t)
                    if saw_tool_result and not parts:
                        # Pure tool-result echo, not a typed prompt
                        continue
                    content = "\n".join(parts).strip()
                if not isinstance(content, str):
                    continue
                content = content.strip()
                if not content:
                    continue
                lower = content.lower()
                if (content.startswith("<")
                        or any(lower.startswith(p) for p in _PROMPT_SKIP_PREFIXES)):
                    continue
                idx += 1
                prompts.append({
                    "idx": idx,
                    "timestamp": rec.get("timestamp") or "",
                    "text": content,
                })
    except Exception:
        return prompts
    return prompts


# --- Auto-rename helpers ---

def _clean_name(text, max_len):
    """Normalize a string for use as a session name: collapse whitespace,
    drop leading/trailing punctuation, hard-cap length."""
    if not text:
        return ""
    text = " ".join(text.split())
    text = text.strip(" \t\n\r\"'`-—_:;,.()[]{}")
    if len(text) > max_len:
        text = text[:max_len].rstrip() + "..."
    return text


def compute_rename_proposals(sessions, discovered):
    """Build a list of {session, current, proposed, source} dicts.

    - Root sessions: proposed = first user prompt (truncated)
    - Branched sessions: proposed = "<parent name> » <first prompt>"
    - Missing-on-disk sessions: proposed = "" (skipped)
    """
    by_id = {d["session_id"]: d for d in discovered}
    registered_by_id = {s.get("session_id"): s for s in sessions}

    proposals = []
    for s in sessions:
        sid = s.get("session_id", "")
        current = s.get("name", "")
        disc = by_id.get(sid)
        if not disc:
            proposals.append({
                "session": s,
                "current": current,
                "proposed": "",
                "source": "missing",
            })
            continue

        first_prompt = (disc.get("name") or "").strip()
        # If discover_sessions fell back to a session-id stub, treat as empty
        if first_prompt.endswith("...") and len(first_prompt) <= 20 and first_prompt.replace("-", "").replace(".", "").isalnum():
            first_prompt = ""

        forked_from = disc.get("forked_from")
        if forked_from:
            # Prefer parent's discovered first prompt (clean, single-layer) over
            # its registered name to avoid cascading "A » B » C" nesting.
            parent_disc = by_id.get(forked_from)
            parent_reg = registered_by_id.get(forked_from)
            parent_name = ""
            if parent_disc and parent_disc.get("name") and not parent_disc.get("name", "").endswith("..."):
                parent_name = parent_disc["name"]
            elif parent_reg and parent_reg.get("name"):
                # Strip any existing branch prefix so we don't compound it
                pname = parent_reg["name"]
                parent_name = pname.split(" » ")[0] if " » " in pname else pname
            parent_short = _clean_name(parent_name, 25)
            prompt_short = _clean_name(first_prompt, 50) or sid[:8]
            if parent_short:
                proposed = f"{parent_short} » {prompt_short}"
            else:
                proposed = prompt_short
            source = "branch"
        else:
            proposed = _clean_name(first_prompt, 75) or sid[:8]
            source = "root"

        proposals.append({
            "session": s,
            "current": current,
            "proposed": proposed,
            "source": source,
        })
    return proposals


def apply_rename_proposals(sessions, accepted_renames):
    """Apply {session_id -> new_name} updates to sessions and save with a backup.

    Returns (updated_count, backup_path)."""
    if not accepted_renames:
        return (0, None)

    # Backup current sessions.json
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    backup_path = SESSIONS_FILE.with_suffix(f".json.pre-rename-{timestamp}")
    if SESSIONS_FILE.exists():
        shutil.copy2(SESSIONS_FILE, backup_path)

    updated = 0
    for s in sessions:
        sid = s.get("session_id", "")
        new_name = accepted_renames.get(sid)
        if not new_name:
            continue
        if s.get("name") != new_name:
            s["name"] = new_name
            updated += 1

    save_sessions(sessions)
    return (updated, backup_path)


def restore_session_backup(backup_path, session_id, project_dir_name):
    """Copy a backup file back to ~/.claude/projects/<project>/<session_id>.jsonl.
    Also ensures a companion session folder exists (Claude Code requires both)."""
    dest_dir = PROJECTS_DIR / project_dir_name
    dest_dir.mkdir(parents=True, exist_ok=True)
    dest_file = dest_dir / f"{session_id}.jsonl"
    # Safety: if a current file exists, back it up first
    if dest_file.exists():
        safety_copy = dest_file.with_suffix(f".jsonl.pre-restore-{datetime.now().strftime('%Y%m%d_%H%M%S')}")
        shutil.copy2(dest_file, safety_copy)
    shutil.copy2(backup_path, dest_file)

    # Claude Code requires a companion folder with the same session id; create it if missing
    companion_dir = dest_dir / session_id
    companion_dir.mkdir(parents=True, exist_ok=True)

    return dest_file


def discover_sessions():
    """Scan ~/.claude/projects/ for real resumable conversations (.jsonl files).
    Reads the actual cwd and first user message from each file."""
    discovered = []
    if not PROJECTS_DIR.exists():
        return discovered
    for proj_dir in PROJECTS_DIR.iterdir():
        if not proj_dir.is_dir():
            continue
        dir_name = proj_dir.name
        # Fallback cwd derived from folder name (may be wrong for underscored paths)
        fallback_cwd = dir_name.replace("--", ":\\", 1).replace("-", "\\")
        if not (len(fallback_cwd) > 1 and fallback_cwd[1] == ':'):
            fallback_cwd = dir_name

        for jsonl in proj_dir.glob("*.jsonl"):
            session_id = jsonl.stem
            preview = ""
            real_cwd = None
            forked_from = None
            logical_parent = None
            try:
                with open(jsonl, "r", encoding="utf-8", errors="ignore") as f:
                    for i, line in enumerate(f):
                        if i > 40:
                            break
                        try:
                            rec = json.loads(line)
                        except Exception:
                            continue
                        if real_cwd is None and isinstance(rec.get("cwd"), str) and rec["cwd"]:
                            real_cwd = rec["cwd"]
                        if forked_from is None:
                            ff = rec.get("forkedFrom")
                            if isinstance(ff, dict) and ff.get("sessionId"):
                                forked_from = ff["sessionId"]
                        if logical_parent is None and rec.get("logicalParentUuid"):
                            logical_parent = rec["logicalParentUuid"]
                        if not preview and rec.get("type") == "user":
                            msg = rec.get("message", {})
                            content = msg.get("content", "")
                            if isinstance(content, list):
                                for c in content:
                                    if isinstance(c, dict) and c.get("type") == "text":
                                        content = c.get("text", "")
                                        break
                            if isinstance(content, str):
                                stripped = content.strip()
                                # Skip Claude Code's auto-generated continuation marker, system tags,
                                # tool-result pings, and IDE selections — none are real user prompts.
                                if (stripped
                                        and not stripped.startswith("<")
                                        and not stripped.lower().startswith("this session is being continued")
                                        and not stripped.lower().startswith("caveat: the messages below")
                                        and not stripped.lower().startswith("[request interrupted")):
                                    preview = stripped.replace("\n", " ")[:60]
                        if real_cwd and preview and forked_from is not None and logical_parent is not None:
                            break
            except Exception:
                pass
            try:
                mtime = jsonl.stat().st_mtime
                size = jsonl.stat().st_size
            except Exception:
                mtime = 0
                size = 0
            discovered.append({
                "session_id": session_id,
                "name": preview or session_id[:16] + "...",
                "cwd": real_cwd or fallback_cwd,
                "source_dir": dir_name,
                "mtime": mtime,
                "size": size,
                "forked_from": forked_from,
                "logical_parent": logical_parent,
            })
    # Most recent first
    discovered.sort(key=lambda d: d.get("mtime", 0), reverse=True)
    return discovered


# --- Toolbar Button Widget ---

class ToolbarButton(tk.Label):
    """Notepad++-style flat toolbar button with hover."""

    def __init__(self, parent, text, command=None, **kwargs):
        super().__init__(parent, text=text, bg=C["bg_toolbar"], fg=C["text"],
                         font=("Segoe UI", 13), padx=10, pady=4, cursor="hand2",
                         **kwargs)
        self._command = command
        self.bind("<Enter>", lambda e: self.config(bg=C["btn_hover"]))
        self.bind("<Leave>", lambda e: self.config(bg=C["bg_toolbar"]))
        self.bind("<ButtonRelease-1>", lambda e: self._on_click())

    def _on_click(self):
        self.config(bg=C["bg_toolbar"])
        if self._command:
            self._command()


class ToolbarSep(tk.Frame):
    """Vertical separator for toolbar."""

    def __init__(self, parent):
        super().__init__(parent, width=1, bg=C["toolbar_sep"], height=22)


# --- Tab Button ---

class TabButton(tk.Label):
    """Notepad++-style tab."""

    def __init__(self, parent, text, active=False, command=None):
        bg = C["bg_tab_active"] if active else C["bg_tab_idle"]
        fg = C["text_bright"] if active else C["text_dim"]
        super().__init__(parent, text=text, bg=bg, fg=fg,
                         font=("Segoe UI", 13), padx=14, pady=5, cursor="hand2")
        self._command = command
        self._active = active
        if not active:
            self.bind("<Enter>", lambda e: self.config(bg=C["bg_lighter"]))
            self.bind("<Leave>", lambda e: self.config(bg=C["bg_tab_idle"]))
        self.bind("<ButtonRelease-1>", lambda e: self._on_click())

    def _on_click(self):
        if self._command:
            self._command()

    def set_active(self, active):
        self._active = active
        if active:
            self.config(bg=C["bg_tab_active"], fg=C["text_bright"])
            self.unbind("<Enter>")
            self.unbind("<Leave>")
        else:
            self.config(bg=C["bg_tab_idle"], fg=C["text_dim"])
            self.bind("<Enter>", lambda e: self.config(bg=C["bg_lighter"]))
            self.bind("<Leave>", lambda e: self.config(bg=C["bg_tab_idle"]))


# --- Multi-Segment Status Bar ---

class StatusBar(tk.Frame):
    """Notepad++-style segmented status bar."""

    def __init__(self, parent):
        super().__init__(parent, bg=C["bg_status"], height=32)
        self.pack_propagate(False)
        self.segments = {}
        self._build()

    def _build(self):
        # Main message
        self.segments["main"] = tk.Label(self, text="Ready", bg=C["bg_status"],
                                          fg=C["text_white"], font=("Segoe UI", 13),
                                          anchor="w", padx=10)
        self.segments["main"].pack(side="left", fill="y")

        # Right-side segments
        for key, text, width in [
            ("remote", "Remote: ON", 100),
            ("mode", "Auto Mode", 90),
            ("count", "0 sessions", 90),
        ]:
            sep = tk.Frame(self, width=1, bg="#005fa3")
            sep.pack(side="right", fill="y", pady=2)
            lbl = tk.Label(self, text=text, bg=C["bg_status"], fg=C["text_white"],
                           font=("Segoe UI", 11), width=width // 8, anchor="center")
            lbl.pack(side="right", fill="y")
            self.segments[key] = lbl

    def set_main(self, text):
        self.segments["main"].config(text=text)

    def set_segment(self, key, text, bg=None):
        if key in self.segments:
            self.segments[key].config(text=text)
            if bg:
                self.segments[key].config(bg=bg)

    def flash(self, text, color=None):
        self.segments["main"].config(text=text, bg=color or C["bg_status"])
        if color:
            self.after(3000, lambda: self.segments["main"].config(bg=C["bg_status"]))


# --- Main Application ---

class SessionManagerApp:
    def __init__(self, root):
        self.root = root
        self.root.title("Claude Session Manager")
        self.root.geometry("1000x700")
        self.root.minsize(960, 620)
        self.root.configure(bg=C["bg"])

        self.sessions = load_sessions()
        self.remote_control_var = tk.BooleanVar(value=True)
        self.skip_permissions_var = tk.BooleanVar(value=True)
        self.search_var = tk.StringVar()
        self.search_var.trace_add("write", lambda *a: self._populate_list())
        self.current_tab = "sessions"
        self.sort_col = None
        self.sort_reverse = False

        # Cost cache: session_id -> {"usd": float, "fingerprint": "size:mtime"}
        self._cost_cache = {}
        # View mode: "flat" (registered sessions only) or "tree" (all on disk, grouped by project + parent)
        self.view_mode = "flat"
        # Cache discovery for tree mode
        self._discovered_cache = []

        self._configure_styles()
        self._build_menubar()
        self._build_toolbar()
        self._build_tabs()
        self._build_main_area()
        self._build_status_bar()
        self._populate_list()
        self._bind_shortcuts()
        self._update_status_segments()

        # Auto-backup all .jsonl sessions on startup (background thread)
        self.root.after(500, self._auto_backup_on_start)
        # Cost computation also in background once at startup
        self.root.after(800, self._compute_all_costs_async)
        # Seed the discovered cache so the Last Used column populates without first opening Tree view
        self.root.after(900, self._seed_discovered_async)

    def _configure_styles(self):
        style = ttk.Style()
        style.theme_use("clam")

        style.configure("Treeview",
                        background=C["bg"],
                        foreground=C["text"],
                        fieldbackground=C["bg"],
                        rowheight=40,
                        font=("Segoe UI", 13),
                        borderwidth=0)
        style.configure("Treeview.Heading",
                        background=C["heading_bg"],
                        foreground=C["text_dim"],
                        font=("Segoe UI", 13),
                        borderwidth=1,
                        relief="flat")
        style.map("Treeview",
                  background=[("selected", C["select_bg"])],
                  foreground=[("selected", C["select_fg"])])
        style.map("Treeview.Heading",
                  background=[("active", C["border_light"])])
        style.layout("Treeview", [("Treeview.treearea", {"sticky": "nswe"})])

        style.configure("Vertical.TScrollbar",
                        background=C["bg_lighter"],
                        troughcolor=C["bg_light"],
                        borderwidth=0, arrowsize=14)
        style.map("Vertical.TScrollbar",
                  background=[("active", C["border_light"])])

        style.configure("TCheckbutton",
                        background=C["bg_lighter"],
                        foreground=C["text"],
                        font=("Segoe UI", 13))
        style.map("TCheckbutton",
                  background=[("active", C["bg_lighter"])])

    # --- Menu Bar ---
    def _build_menubar(self):
        menubar = tk.Menu(self.root, bg=C["bg_menubar"], fg=C["text"],
                          activebackground=C["accent"], activeforeground=C["text_white"],
                          font=("Segoe UI", 13), relief="flat", borderwidth=0)

        # File
        file_menu = tk.Menu(menubar, tearoff=0, bg=C["bg_lighter"], fg=C["text"],
                            activebackground=C["accent"], activeforeground=C["text_white"],
                            font=("Segoe UI", 13))
        file_menu.add_command(label="New Session...          Ctrl+N", command=self.add_session)
        file_menu.add_command(label="Open Session Folder     Ctrl+O", command=self.open_folder)
        file_menu.add_separator()
        file_menu.add_command(label="Scan Claude Storage", command=self.scan_sessions)
        file_menu.add_command(label="Reload Sessions         F5", command=self._reload)
        file_menu.add_separator()
        file_menu.add_command(label="Exit                    Alt+F4", command=self.root.quit)
        menubar.add_cascade(label="File", menu=file_menu)

        # Session
        session_menu = tk.Menu(menubar, tearoff=0, bg=C["bg_lighter"], fg=C["text"],
                               activebackground=C["accent"], activeforeground=C["text_white"],
                               font=("Segoe UI", 13))
        session_menu.add_command(label="Launch                  Enter", command=self.launch_session)
        session_menu.add_command(label="Launch All              Ctrl+L", command=self.launch_all_sessions)
        session_menu.add_separator()
        session_menu.add_command(label="Edit Session...         F2", command=self.edit_session)
        session_menu.add_command(label="Duplicate Session", command=self.duplicate_session)
        session_menu.add_command(label="Remove Session          Del", command=self.remove_session)
        session_menu.add_separator()
        session_menu.add_checkbutton(label="Remote Control", variable=self.remote_control_var)
        session_menu.add_checkbutton(label="Skip Permissions", variable=self.skip_permissions_var)
        menubar.add_cascade(label="Session", menu=session_menu)

        # Search
        search_menu = tk.Menu(menubar, tearoff=0, bg=C["bg_lighter"], fg=C["text"],
                              activebackground=C["accent"], activeforeground=C["text_white"],
                              font=("Segoe UI", 13))
        search_menu.add_command(label="Find...                 Ctrl+F", command=self._focus_search)
        search_menu.add_command(label="Clear Search            Esc", command=self._clear_search)
        menubar.add_cascade(label="Search", menu=search_menu)

        # View
        view_menu = tk.Menu(menubar, tearoff=0, bg=C["bg_lighter"], fg=C["text"],
                            activebackground=C["accent"], activeforeground=C["text_white"],
                            font=("Segoe UI", 13))
        view_menu.add_command(label="Sessions Tab            1", command=lambda: self._switch_tab("sessions"))
        view_menu.add_command(label="Settings Tab            2", command=lambda: self._switch_tab("settings"))
        view_menu.add_separator()
        view_menu.add_command(label="Sort by Name", command=lambda: self._sort_by("name"))
        view_menu.add_command(label="Sort by Alias", command=lambda: self._sort_by("alias"))
        view_menu.add_command(label="Sort by Directory", command=lambda: self._sort_by("cwd"))
        menubar.add_cascade(label="View", menu=view_menu)

        # Help
        help_menu = tk.Menu(menubar, tearoff=0, bg=C["bg_lighter"], fg=C["text"],
                            activebackground=C["accent"], activeforeground=C["text_white"],
                            font=("Segoe UI", 13))
        help_menu.add_command(label="Keyboard Shortcuts", command=self._show_shortcuts)
        help_menu.add_separator()
        help_menu.add_command(label="About CSM", command=self._show_about)
        menubar.add_cascade(label="Help", menu=help_menu)

        self.root.config(menu=menubar)

    # --- Toolbar ---
    def _build_toolbar(self):
        toolbar = tk.Frame(self.root, bg=C["bg_toolbar"], height=44)
        toolbar.pack(fill="x")
        toolbar.pack_propagate(False)

        # Left group: session actions
        for text, cmd in [
            ("Launch", self.launch_session),
            ("Launch All", self.launch_all_sessions),
            ("Run Task", self.run_task),
        ]:
            ToolbarButton(toolbar, text=text, command=cmd).pack(side="left", padx=1)

        ToolbarSep(toolbar).pack(side="left", padx=6, pady=5)

        for text, cmd in [
            ("New", self.add_session),
            ("Edit", self.edit_session),
            ("Remove", self.remove_session),
            ("Auto-rename", self.auto_rename_sessions),
        ]:
            ToolbarButton(toolbar, text=text, command=cmd).pack(side="left", padx=1)

        ToolbarSep(toolbar).pack(side="left", padx=6, pady=5)

        ToolbarButton(toolbar, text="Folder", command=self.open_folder).pack(side="left", padx=1)
        ToolbarButton(toolbar, text="Scan", command=self.scan_sessions).pack(side="left", padx=1)
        ToolbarButton(toolbar, text="Backup", command=self.backup_now).pack(side="left", padx=1)
        ToolbarButton(toolbar, text="Restore", command=self.restore_backup).pack(side="left", padx=1)
        ToolbarButton(toolbar, text="Sync Costs", command=self.sync_costs).pack(side="left", padx=1)
        self.view_btn = ToolbarButton(toolbar, text="View: Flat", command=self.toggle_view)
        self.view_btn.pack(side="left", padx=1)

        ToolbarSep(toolbar).pack(side="left", padx=6, pady=5)

        # Remote control toggle in toolbar
        rc_frame = tk.Frame(toolbar, bg=C["bg_toolbar"])
        rc_frame.pack(side="left", padx=4)
        ttk.Checkbutton(rc_frame, text="Remote", variable=self.remote_control_var,
                        command=self._update_status_segments).pack(side="left")
        ttk.Checkbutton(rc_frame, text="Auto", variable=self.skip_permissions_var,
                        command=self._update_status_segments).pack(side="left", padx=(6, 0))

        # Right: search
        search_frame = tk.Frame(toolbar, bg=C["bg_toolbar"])
        search_frame.pack(side="right", padx=8, pady=3)

        tk.Label(search_frame, text="Find:", bg=C["bg_toolbar"], fg=C["text_dim"],
                 font=("Segoe UI", 13)).pack(side="left", padx=(0, 4))

        self.search_entry = tk.Entry(search_frame, textvariable=self.search_var,
                                      bg=C["bg_input"], fg=C["text_bright"],
                                      insertbackground=C["text_bright"],
                                      font=("Segoe UI", 13), relief="flat", width=22,
                                      highlightthickness=1,
                                      highlightbackground=C["border"],
                                      highlightcolor=C["accent"])
        self.search_entry.pack(side="left", ipady=2)

        # Border below toolbar
        tk.Frame(self.root, bg=C["border"], height=1).pack(fill="x")

    # --- Tabs ---
    def _build_tabs(self):
        self.tab_bar = tk.Frame(self.root, bg=C["bg_tab_idle"])
        self.tab_bar.pack(fill="x")

        self.tab_buttons = {}
        for key, label in [("sessions", "Sessions"), ("settings", "Settings")]:
            tb = TabButton(self.tab_bar, text=f"  {label}  ",
                           active=(key == "sessions"),
                           command=lambda k=key: self._switch_tab(k))
            tb.pack(side="left")
            self.tab_buttons[key] = tb

        # Filler for tab bar right side
        tk.Label(self.tab_bar, text="", bg=C["bg_tab_idle"]).pack(side="left", fill="x", expand=True)

        # Bottom border for tabs
        tk.Frame(self.root, bg=C["border"], height=1).pack(fill="x")

    def _switch_tab(self, tab_key):
        self.current_tab = tab_key
        for key, btn in self.tab_buttons.items():
            btn.set_active(key == tab_key)

        # Show/hide frames
        if tab_key == "sessions":
            self.settings_frame.pack_forget()
            self.main_frame.pack(fill="both", expand=True)
        elif tab_key == "settings":
            self.main_frame.pack_forget()
            self.settings_frame.pack(fill="both", expand=True)

    # --- Main Content ---
    def _build_main_area(self):
        # Container that holds both tabs' content
        self.content_area = tk.Frame(self.root, bg=C["bg"])
        self.content_area.pack(fill="both", expand=True)

        # --- Sessions Tab ---
        self.main_frame = tk.Frame(self.content_area, bg=C["bg"])
        self.main_frame.pack(fill="both", expand=True)

        paned = tk.Frame(self.main_frame, bg=C["bg"])
        paned.pack(fill="both", expand=True)

        # Treeview
        tree_frame = tk.Frame(paned, bg=C["bg"])
        tree_frame.pack(side="left", fill="both", expand=True)

        columns = ("name", "alias", "directory", "mode", "model", "cost", "last_used", "session_id")
        # show="tree headings" gives us an expandable left column for the tree view;
        # the tree column is hidden in flat mode via column width.
        self.tree = ttk.Treeview(tree_frame, columns=columns, show="tree headings",
                                  selectmode="browse")
        # Hide the tree column by default (flat mode); _apply_view_mode toggles its width.
        self.tree.column("#0", width=0, stretch=False, minwidth=0)

        self.tree.heading("name", text="Name", anchor="w",
                          command=lambda: self._sort_by("name"))
        self.tree.heading("alias", text="Alias", anchor="w",
                          command=lambda: self._sort_by("alias"))
        self.tree.heading("directory", text="Working Directory", anchor="w",
                          command=lambda: self._sort_by("cwd"))
        self.tree.heading("mode", text="Mode", anchor="center")
        self.tree.heading("model", text="Model", anchor="w")
        self.tree.heading("cost", text="Cost", anchor="e",
                          command=lambda: self._sort_by("_cost"))
        self.tree.heading("last_used", text="Last Used", anchor="w",
                          command=lambda: self._sort_by("_last_used"))
        self.tree.heading("session_id", text="Session ID", anchor="w")

        self.tree.column("name", width=200, minwidth=120)
        self.tree.column("alias", width=110, minwidth=70)
        self.tree.column("directory", width=240, minwidth=140)
        self.tree.column("mode", width=60, minwidth=50, anchor="center")
        self.tree.column("model", width=100, minwidth=80)
        self.tree.column("cost", width=75, minwidth=65, anchor="e")
        self.tree.column("last_used", width=130, minwidth=100, anchor="w")
        self.tree.column("session_id", width=200, minwidth=120)

        scrollbar = ttk.Scrollbar(tree_frame, orient="vertical", command=self._on_scroll)
        self.tree.configure(yscrollcommand=self._sync_scroll)

        self.tree.pack(side="left", fill="both", expand=True)
        scrollbar.pack(side="right", fill="y")

        self.tree.bind("<Double-1>", lambda e: self.launch_session())
        self.tree.bind("<<TreeviewSelect>>", self._on_select)
        self.tree.bind("<Button-3>", self._show_context_menu)
        self._build_context_menu()

        # Alternating row tags
        self.tree.tag_configure("odd", background=C["bg"])
        self.tree.tag_configure("even", background=C["bg_light"])
        # Tree-mode project header style
        self.tree.tag_configure("project", background=C["bg_toolbar"], foreground=C["text_bright"])

        # --- Detail bar below list ---
        detail_bar = tk.Frame(self.main_frame, bg=C["bg_lighter"], height=38)
        detail_bar.pack(fill="x")
        detail_bar.pack_propagate(False)

        tk.Frame(detail_bar, bg=C["border"], height=1).pack(fill="x", side="top")

        self.detail_label = tk.Label(detail_bar, text="  Select a session to view details",
                                      bg=C["bg_lighter"], fg=C["text_dim"],
                                      font=("Segoe UI", 13), anchor="w")
        self.detail_label.pack(side="left", fill="both", expand=True, padx=4)

        # --- Settings Tab ---
        self.settings_frame = tk.Frame(self.content_area, bg=C["bg"])
        self._build_settings_tab()

    def _build_settings_tab(self):
        pad = tk.Frame(self.settings_frame, bg=C["bg"])
        pad.pack(fill="both", expand=True, padx=40, pady=30)

        tk.Label(pad, text="Settings", bg=C["bg"], fg=C["text_bright"],
                 font=("Segoe UI", 20, "bold")).pack(anchor="w", pady=(0, 20))

        # Settings groups
        for group_title, options in [
            ("Launch Defaults", [
                ("remote_control", "Enable Remote Control by default", self.remote_control_var),
                ("skip_perms", "Skip permission prompts by default", self.skip_permissions_var),
            ]),
        ]:
            tk.Label(pad, text=group_title, bg=C["bg"], fg=C["text"],
                     font=("Segoe UI", 14, "bold")).pack(anchor="w", pady=(16, 8))
            tk.Frame(pad, bg=C["border"], height=1).pack(fill="x", pady=(0, 8))

            for key, label, var in options:
                row = tk.Frame(pad, bg=C["bg"])
                row.pack(fill="x", pady=3)
                ttk.Checkbutton(row, text=label, variable=var).pack(side="left")

        # Paths info
        tk.Label(pad, text="Paths", bg=C["bg"], fg=C["text"],
                 font=("Segoe UI", 14, "bold")).pack(anchor="w", pady=(24, 8))
        tk.Frame(pad, bg=C["border"], height=1).pack(fill="x", pady=(0, 8))

        for label, path in [
            ("Sessions File:", str(SESSIONS_FILE)),
            ("Claude Home:", str(CLAUDE_HOME)),
            ("Projects Dir:", str(PROJECTS_DIR)),
        ]:
            row = tk.Frame(pad, bg=C["bg"])
            row.pack(fill="x", pady=2)
            tk.Label(row, text=label, bg=C["bg"], fg=C["text_dim"],
                     font=("Segoe UI", 13), width=14, anchor="w").pack(side="left")
            tk.Label(row, text=path, bg=C["bg"], fg=C["text"],
                     font=("Segoe UI", 13), anchor="w").pack(side="left")

        # Shortcuts reference
        tk.Label(pad, text="Keyboard Shortcuts", bg=C["bg"], fg=C["text"],
                 font=("Segoe UI", 14, "bold")).pack(anchor="w", pady=(24, 8))
        tk.Frame(pad, bg=C["border"], height=1).pack(fill="x", pady=(0, 8))

        shortcuts_text = (
            "Ctrl+N  New Session    |  Enter   Launch       |  Ctrl+L  Launch All\n"
            "F2      Edit Session   |  Del     Remove       |  Ctrl+F  Find\n"
            "Ctrl+O  Open Folder    |  F5      Reload       |  Esc     Clear Search"
        )
        tk.Label(pad, text=shortcuts_text, bg=C["bg"], fg=C["text_dim"],
                 font=("Segoe UI", 13), justify="left", anchor="w").pack(anchor="w")

    # --- Status Bar ---
    def _build_status_bar(self):
        self.status = StatusBar(self.root)
        self.status.pack(fill="x", side="bottom")

    def _update_status_segments(self):
        remote = "ON" if self.remote_control_var.get() else "OFF"
        mode = "Auto" if self.skip_permissions_var.get() else "Normal"
        self.status.set_segment("remote", f"Remote: {remote}")
        self.status.set_segment("mode", f"{mode} Mode")
        self.status.set_segment("count", f"{len(self.sessions)} sessions")

    # --- Gutter / Scroll Sync ---

    def _sync_scroll(self, first, last):
        # Update scrollbar position
        self.tree.yview_moveto(first)

    def _on_scroll(self, *args):
        self.tree.yview(*args)

    # --- Keyboard Shortcuts ---
    def _bind_shortcuts(self):
        self.root.bind("<Control-f>", lambda e: self._focus_search())
        self.root.bind("<Return>", lambda e: self.launch_session())
        self.root.bind("<Escape>", lambda e: self._clear_search())
        self.root.bind("<Control-n>", lambda e: self.add_session())
        self.root.bind("<Delete>", lambda e: self.remove_session())
        self.root.bind("<Control-l>", lambda e: self.launch_all_sessions())
        self.root.bind("<Control-o>", lambda e: self.open_folder())
        self.root.bind("<F2>", lambda e: self.edit_session())
        self.root.bind("<F5>", lambda e: self._reload())
        self.root.bind("<Key-1>", lambda e: self._switch_tab("sessions") if not self._is_entry_focused() else None)
        self.root.bind("<Key-2>", lambda e: self._switch_tab("settings") if not self._is_entry_focused() else None)

    def _is_entry_focused(self):
        focused = self.root.focus_get()
        return isinstance(focused, tk.Entry)

    def _focus_search(self):
        self.search_entry.focus_set()
        self.search_entry.select_range(0, "end")

    def _clear_search(self):
        self.search_var.set("")
        self.root.focus_set()

    # --- Data Methods ---

    def _populate_list(self):
        if self.view_mode == "tree":
            self._populate_tree()
            return
        self.tree.delete(*self.tree.get_children())
        filter_text = self.search_var.get().lower()
        count = 0
        for s in self.sessions:
            name = s.get("name", "")
            alias = s.get("alias", "")
            cwd = s.get("cwd", "")
            mode = "Auto" if s.get("skip_permissions") else "Std"
            sid = s.get("session_id", "")
            model_id = s.get("model", "")
            model_label = MODEL_LABEL_BY_ID.get(model_id, model_id) if model_id else "Default"
            model_short = model_label.replace("Default (Claude Code chooses)", "Default")
            cost_usd = self._cost_cache.get(sid, {}).get("usd")
            cost_str = _pricing.format_cost(cost_usd) if cost_usd is not None else "—"
            mtime = self._mtime_for(sid)
            last_used = format_last_used(mtime) if mtime else "—"

            if filter_text:
                if (filter_text not in name.lower() and
                    filter_text not in alias.lower() and
                    filter_text not in cwd.lower() and
                    filter_text not in sid.lower() and
                    filter_text not in model_short.lower()):
                    continue

            tag = "odd" if count % 2 == 0 else "even"
            self.tree.insert("", "end",
                             values=(name, alias, cwd, mode, model_short, cost_str, last_used, sid),
                             tags=(tag,))
            count += 1

        self.status.set_segment("count", f"{count} sessions")
        matches = f" ({count} matches)" if filter_text else ""
        self.status.set_main(f"Ready{matches}")

    def toggle_view(self):
        self.view_mode = "tree" if self.view_mode == "flat" else "flat"
        self.view_btn.config(text=f"View: {'Tree' if self.view_mode == 'tree' else 'Flat'}")
        self._apply_view_mode()
        self._populate_list()

    def _apply_view_mode(self):
        if self.view_mode == "tree":
            self.tree.column("#0", width=320, stretch=False, minwidth=200)
            # Hide redundant columns in tree mode (the tree column shows them via labels)
            self.tree.column("name", width=0, stretch=False, minwidth=0)
            self.tree.column("alias", width=0, stretch=False, minwidth=0)
        else:
            self.tree.column("#0", width=0, stretch=False, minwidth=0)
            self.tree.column("name", width=200, minwidth=120, stretch=True)
            self.tree.column("alias", width=110, minwidth=70, stretch=False)

    def _populate_tree(self):
        """Show all .jsonl on disk grouped by project, then by branch parent chain."""
        self.tree.delete(*self.tree.get_children())
        if not self._discovered_cache:
            self._discovered_cache = discover_sessions()
        filter_text = self.search_var.get().lower()

        # Index everything by session_id and known status
        registered_by_id = {s.get("session_id"): s for s in self.sessions}
        by_id = {d["session_id"]: d for d in self._discovered_cache}

        # Bucket by project folder
        by_project = {}
        for d in self._discovered_cache:
            by_project.setdefault(d["source_dir"], []).append(d)

        # Sort projects by most-recent activity desc
        proj_order = sorted(
            by_project.keys(),
            key=lambda p: max((x.get("mtime", 0) for x in by_project[p]), default=0),
            reverse=True,
        )

        total_rows = 0
        for proj in proj_order:
            children = by_project[proj]
            # Build parent map: child_id -> parent_id (only when parent exists in same project)
            project_ids = {c["session_id"] for c in children}
            roots = [c for c in children if not c.get("forked_from") or c.get("forked_from") not in project_ids]
            kids_of = {}
            for c in children:
                pf = c.get("forked_from")
                if pf and pf in project_ids:
                    kids_of.setdefault(pf, []).append(c)

            # Project header row
            proj_iid = f"proj::{proj}"
            self.tree.insert("", "end", iid=proj_iid, text=f"  📁 {proj}  ({len(children)})",
                             values=("", "", "", "", "", "", "", ""), open=True, tags=("project",))

            # Sort roots by mtime desc, recurse
            def _row_label(d):
                reg = registered_by_id.get(d["session_id"])
                star = "★ " if reg else ""
                nm = (reg.get("name") if reg else d.get("name", "")) or d["session_id"][:8]
                return f"{star}{nm}"

            def _row_values(d):
                reg = registered_by_id.get(d["session_id"])
                alias = (reg or {}).get("alias", "")
                mode_v = "Auto" if (reg or {}).get("skip_permissions") else ""
                model_id = (reg or {}).get("model", "")
                model_lbl = MODEL_LABEL_BY_ID.get(model_id, model_id) if model_id else "Default"
                model_short = model_lbl.replace("Default (Claude Code chooses)", "Default")
                cost_usd = self._cost_cache.get(d["session_id"], {}).get("usd")
                cost_str = _pricing.format_cost(cost_usd) if cost_usd is not None else "—"
                last_used = format_last_used(d.get("mtime", 0)) if d.get("mtime") else "—"
                return ("", alias, d.get("cwd", ""), mode_v, model_short, cost_str, last_used, d["session_id"])

            def _matches_filter(d):
                if not filter_text:
                    return True
                hay = " ".join([
                    (registered_by_id.get(d["session_id"]) or {}).get("name", "") or "",
                    (registered_by_id.get(d["session_id"]) or {}).get("alias", "") or "",
                    d.get("name", ""),
                    d.get("cwd", ""),
                    d.get("session_id", ""),
                ]).lower()
                return filter_text in hay

            def _insert(parent_iid, node, depth=0):
                if not (_matches_filter(node) or any(_matches_filter(k) for k in _all_descendants(node))):
                    return 0
                iid = node["session_id"]
                try:
                    self.tree.insert(parent_iid, "end", iid=iid, text=_row_label(node),
                                     values=_row_values(node), open=(depth < 1))
                except tk.TclError:
                    # Duplicate iid (shouldn't happen but be safe)
                    return 0
                count_local = 1
                for kid in sorted(kids_of.get(node["session_id"], []), key=lambda x: x.get("mtime", 0), reverse=True):
                    count_local += _insert(iid, kid, depth + 1)
                return count_local

            def _all_descendants(node):
                stack = list(kids_of.get(node["session_id"], []))
                out = []
                while stack:
                    n = stack.pop()
                    out.append(n)
                    stack.extend(kids_of.get(n["session_id"], []))
                return out

            for r in sorted(roots, key=lambda x: x.get("mtime", 0), reverse=True):
                total_rows += _insert(proj_iid, r)

            # Hide empty project headers when search filtered everything out
            if not self.tree.get_children(proj_iid):
                self.tree.delete(proj_iid)

        self.status.set_segment("count", f"{total_rows} sessions")
        matches = f" ({total_rows} matches)" if filter_text else ""
        self.status.set_main(f"Tree view  ({len(proj_order)} projects){matches}")

    def _sort_by(self, key):
        if self.sort_col == key:
            self.sort_reverse = not self.sort_reverse
        else:
            self.sort_col = key
            self.sort_reverse = False
        if key == "_cost":
            self.sessions.sort(
                key=lambda s: self._cost_cache.get(s.get("session_id", ""), {}).get("usd", 0.0),
                reverse=self.sort_reverse,
            )
        elif key == "_last_used":
            self.sessions.sort(
                key=lambda s: self._mtime_for(s.get("session_id", "")),
                reverse=self.sort_reverse,
            )
        else:
            self.sessions.sort(key=lambda s: str(s.get(key, "")).lower(), reverse=self.sort_reverse)
        self._populate_list()
        arrow = " v" if self.sort_reverse else " ^"
        self.status.set_main(f"Sorted by {key}{arrow}")

    def _reload(self):
        self.sessions = load_sessions()
        self._populate_list()
        self._update_status_segments()
        self.status.flash("Sessions reloaded", C["accent_green"])

    def _on_select(self, event):
        session = self._get_selected_session()
        if session:
            sid = session["session_id"]
            sid_display = sid if len(sid) <= 32 else sid[:32] + "..."
            self.detail_label.config(
                text=f"  {session['name']}  |  {session.get('cwd','')}  |  ID: {sid_display}",
                fg=C["text"])

    def _get_selected_session(self):
        sel = self.tree.selection()
        if not sel:
            return None
        iid = sel[0]
        # Tree mode: skip project headers; lookup by session_id in registered list,
        # otherwise build an ad-hoc session dict from the discovered cache.
        if iid.startswith("proj::"):
            return None
        if self.view_mode == "tree":
            for s in self.sessions:
                if s.get("session_id") == iid:
                    return s
            for d in self._discovered_cache:
                if d.get("session_id") == iid:
                    return {
                        "name": d.get("name", "")[:40] or iid[:8],
                        "alias": iid[:8],
                        "session_id": iid,
                        "cwd": d.get("cwd", ""),
                        "skip_permissions": True,
                        "model": "",
                    }
            return None
        # Flat mode: original behavior
        values = self.tree.item(iid, "values")
        name = values[0] if values else ""
        for s in self.sessions:
            if s.get("name") == name:
                return s
        return None

    # --- Session Actions ---

    def launch_session(self):
        session = self._get_selected_session()
        if not session:
            self.status.flash("Select a session first")
            return
        self._do_launch(session)

    def launch_all_sessions(self):
        if not self.sessions:
            return
        count = len(self.sessions)
        if not messagebox.askyesno("Launch All Sessions",
                                    f"This will open {count} PowerShell windows,\n"
                                    f"each with Remote Control enabled.\n\n"
                                    f"Continue?"):
            return
        for s in self.sessions:
            self._do_launch(s, quiet=True)
        self.status.flash(f"Launched {count} sessions", C["accent_green"])

    def run_task(self):
        """Open the Run Task dialog: spawn `claude -p` in a target session's project."""
        target = self._get_selected_session()
        if not target:
            messagebox.showinfo("Run Task", "Select a target session in the list first.")
            return
        RunTaskDialog(self.root, target, self.sessions,
                      on_done=lambda name: self.status.flash(
                          f"Task dispatched to '{name}'", C["accent_green"]))

    def _do_launch(self, session, quiet=False):
        import shutil
        session_id = session["session_id"]
        cwd = session.get("cwd", ".")
        name = session.get("name", "Session")
        skip_perms = self.skip_permissions_var.get() and session.get("skip_permissions", False)
        remote = self.remote_control_var.get()

        # Fallback if cwd doesn't exist
        if not os.path.isdir(cwd):
            if not quiet:
                if not messagebox.askyesno(
                    "Directory Missing",
                    f"Working directory does not exist:\n{cwd}\n\nLaunch in home directory instead?"
                ):
                    return
            cwd = str(Path.home())

        cmd_parts = ["claude", "--resume", session_id]
        if skip_perms:
            cmd_parts.append("--dangerously-skip-permissions")
        model = (session.get("model") or "").strip()
        if model:
            cmd_parts.extend(["--model", model])
        if remote:
            # Use single quotes — PowerShell treats as literal string, no escaping issues
            safe_rc_name = name.replace("'", "''")
            cmd_parts.extend(["--remote-control", f"'{safe_rc_name}'"])
        cmd_str = " ".join(cmd_parts)

        title_text = f"Claude :: {name}"
        wt_path = shutil.which("wt") or shutil.which("wt.exe")

        try:
            if wt_path:
                # Launch as a new tab in Windows Terminal with proper title
                wt_args = [
                    wt_path, "-w", "0", "new-tab",
                    "--title", title_text,
                    "-d", cwd,
                    "powershell", "-NoExit", "-Command", cmd_str,
                ]
                subprocess.Popen(wt_args)
            else:
                # Fallback: classic PowerShell console
                safe_name = name.replace("'", "''")
                ps_title = (
                    f"$Host.UI.RawUI.WindowTitle = 'Claude :: {safe_name}';"
                    f" [Console]::Write([char]27 + ']0;Claude :: {safe_name}' + [char]7);"
                )
                ps_command = f'{ps_title} cd \"{cwd}\"; {cmd_str}'
                subprocess.Popen(
                    ["powershell", "-NoExit", "-Command", ps_command],
                    creationflags=subprocess.CREATE_NEW_CONSOLE
                )
            if not quiet:
                remote_str = "  [Remote]" if remote else ""
                self.status.flash(f"Launched: {name}{remote_str}  ->  {cwd}", C["accent_green"])
        except Exception as e:
            messagebox.showerror("Launch Failed", f"Could not launch session:\n{e}")

    def open_folder(self):
        session = self._get_selected_session()
        if not session:
            return
        cwd = session.get("cwd", ".")
        if os.path.isdir(cwd):
            os.startfile(cwd)
        else:
            messagebox.showwarning("Directory Not Found", f"Path does not exist:\n{cwd}")

    def add_session(self):
        dialog = SessionDialog(self.root, title="New Session")
        self.root.wait_window(dialog)
        if dialog.result:
            self.sessions.append(dialog.result)
            save_sessions(self.sessions)
            self._populate_list()
            self._update_status_segments()

    def edit_session(self):
        session = self._get_selected_session()
        if not session:
            self.status.flash("Select a session to edit")
            return
        dialog = SessionDialog(self.root, title="Edit Session", session=session)
        self.root.wait_window(dialog)
        if dialog.result:
            idx = self.sessions.index(session)
            self.sessions[idx] = dialog.result
            save_sessions(self.sessions)
            self._populate_list()

    def duplicate_session(self):
        session = self._get_selected_session()
        if not session:
            self.status.flash("Select a session to duplicate")
            return
        new = dict(session)
        new["name"] = session["name"] + " (copy)"
        new["alias"] = session.get("alias", "") + "-copy"
        self.sessions.append(new)
        save_sessions(self.sessions)
        self._populate_list()
        self._update_status_segments()
        self.status.flash(f"Duplicated: {session['name']}")

    def remove_session(self):
        session = self._get_selected_session()
        if not session:
            return
        if messagebox.askyesno("Remove Session",
                                f"Remove \"{session['name']}\" from the manager?\n\n"
                                f"The Claude session itself will not be deleted."):
            self.sessions.remove(session)
            save_sessions(self.sessions)
            self._populate_list()
            self._update_status_segments()
            self.detail_label.config(text="  Select a session to view details",
                                      fg=C["text_dim"])

    def view_prompts(self):
        """Open a dialog showing every user prompt in the selected session, in order."""
        session = self._get_selected_session()
        if not session:
            return
        jsonl = self._resolve_session_jsonl(session)
        if not jsonl:
            messagebox.showinfo("View Prompts",
                                f"No .jsonl on disk for this session.\n\n"
                                f"Session ID: {session.get('session_id','')}")
            return
        prompts = extract_user_prompts(jsonl)
        if not prompts:
            messagebox.showinfo("View Prompts",
                                "No user prompts found in this session.\n"
                                "(All entries were system markers, tool results, or continuations.)")
            return
        SessionPromptsDialog(self.root, session, prompts)

    def _build_context_menu(self):
        """Right-click popup for individual session rows."""
        m = tk.Menu(self.root, tearoff=False, bg=C["bg_menubar"], fg=C["text"],
                    activebackground=C["accent"], activeforeground=C["text_white"],
                    font=("Segoe UI", 12), relief="flat", borderwidth=0)
        m.add_command(label="Launch                              Enter",
                      command=self.launch_session)
        m.add_command(label="Run Task...",
                      command=self.run_task)
        m.add_separator()
        m.add_command(label="View Prompts...",
                      command=self.view_prompts)
        m.add_command(label="Auto-rename this session",
                      command=self.auto_rename_selected)
        m.add_command(label="Edit...                              F2",
                      command=self.edit_session)
        m.add_command(label="Duplicate",
                      command=self.duplicate_session)
        m.add_separator()
        m.add_command(label="Open folder                  Ctrl+O",
                      command=self.open_folder)
        m.add_command(label="Restore from backup...",
                      command=self.restore_backup)
        m.add_separator()
        m.add_command(label="Remove                             Del",
                      command=self.remove_session)
        self._ctx_menu = m

    def _show_context_menu(self, event):
        """Select the row under the pointer, then pop up the context menu."""
        row_id = self.tree.identify_row(event.y)
        if row_id and row_id.startswith("proj::"):
            return  # don't pop a menu on a tree-view project header
        if row_id:
            self.tree.selection_set(row_id)
            self.tree.focus(row_id)
        if not self._get_selected_session():
            return
        try:
            self._ctx_menu.tk_popup(event.x_root, event.y_root)
        finally:
            self._ctx_menu.grab_release()

    def auto_rename_selected(self):
        """Rename just the currently-selected session using the same proposal logic."""
        session = self._get_selected_session()
        if not session:
            return
        if not self._discovered_cache:
            self._discovered_cache = discover_sessions()
        proposals = compute_rename_proposals([session], self._discovered_cache)
        if not proposals:
            return
        p = proposals[0]
        if p["source"] == "missing":
            messagebox.showinfo("Auto-rename",
                                f"\"{session.get('name','')}\"\n\nNo .jsonl on disk for this session.")
            return
        if not p["proposed"] or p["proposed"] == p["current"]:
            messagebox.showinfo("Auto-rename",
                                f"\"{session.get('name','')}\"\n\nAlready matches its first prompt — nothing to change.")
            return

        proposed = self._prompt_for_name(
            title="Auto-rename this session",
            label=f"Source: {p['source'].upper()}\n\nCurrent:   {p['current']}\n\nProposed:",
            initial=p["proposed"],
        )
        if not proposed or proposed == p["current"]:
            return

        updated, backup = apply_rename_proposals(self.sessions, {session.get("session_id"): proposed})
        self._populate_list()
        if updated:
            self.status.flash(
                f'Renamed to "{proposed[:50]}"  (backup: {backup.name if backup else "none"})',
                C["accent_green"],
            )

    def _prompt_for_name(self, title, label, initial):
        """Small inline dialog with a single editable text field. Returns trimmed value or None."""
        top = tk.Toplevel(self.root)
        top.title(title)
        top.configure(bg=C["bg"])
        top.transient(self.root)
        top.grab_set()
        W, H = 640, 240
        top.geometry(f"{W}x{H}")
        top.minsize(520, 220)
        self.root.update_idletasks()
        x = self.root.winfo_x() + (self.root.winfo_width() - W) // 2
        y = self.root.winfo_y() + (self.root.winfo_height() - H) // 2
        top.geometry(f"+{x}+{y}")

        tk.Label(top, text=label, bg=C["bg"], fg=C["text"], justify="left",
                 font=("Segoe UI", 12), anchor="w").pack(fill="x", padx=18, pady=(16, 6))
        var = tk.StringVar(value=initial)
        entry = tk.Entry(top, textvariable=var, bg=C["bg_input"], fg=C["text_bright"],
                         insertbackground=C["text_bright"], font=("Segoe UI", 13),
                         relief="flat", highlightthickness=1,
                         highlightbackground=C["border"], highlightcolor=C["accent"])
        entry.pack(fill="x", padx=18, pady=4, ipady=4)
        entry.select_range(0, "end")
        entry.focus_set()

        result = {"value": None}
        def _ok():
            result["value"] = var.get().strip()
            top.destroy()
        def _cancel():
            top.destroy()

        btn_bar = tk.Frame(top, bg=C["bg_lighter"])
        btn_bar.pack(fill="x", side="bottom")
        tk.Button(btn_bar, text="Cancel", bg=C["bg_toolbar"], fg=C["text"],
                  font=("Segoe UI", 12), relief="flat", padx=14, pady=6,
                  cursor="hand2", command=_cancel).pack(side="right", padx=8, pady=10)
        tk.Button(btn_bar, text="Save", bg=C["accent"], fg=C["text_white"],
                  font=("Segoe UI", 12, "bold"), relief="flat", padx=14, pady=6,
                  cursor="hand2", command=_ok).pack(side="right", pady=10)

        entry.bind("<Return>", lambda e: _ok())
        entry.bind("<Escape>", lambda e: _cancel())
        self.root.wait_window(top)
        return result["value"]

    def auto_rename_sessions(self):
        """Compute proposed names from each session's first prompt + parent and
        show a preview dialog the user can edit before applying."""
        if not self.sessions:
            messagebox.showinfo("Auto-rename", "No registered sessions.")
            return
        self.status.flash("Reading first prompts from each session...", C["accent_orange"])
        self.root.update()
        # Refresh discovery so we have current first-prompts and forked_from info
        if not self._discovered_cache:
            self._discovered_cache = discover_sessions()
        proposals = compute_rename_proposals(self.sessions, self._discovered_cache)

        # Skip rows where current == proposed already (no change)
        changing = [p for p in proposals if p["proposed"] and p["proposed"] != p["current"]]
        if not changing:
            messagebox.showinfo("Auto-rename",
                                "All session names already match their first prompts. Nothing to change.")
            return

        AutoRenameDialog(self.root, self, proposals, on_apply=self._on_rename_applied)

    def _on_rename_applied(self, accepted_renames):
        """Called by AutoRenameDialog when the user clicks Apply."""
        updated, backup = apply_rename_proposals(self.sessions, accepted_renames)
        self._populate_list()
        self._update_status_segments()
        if updated:
            msg = f"Renamed {updated} sessions  (backup: {backup.name if backup else 'none'})"
            self.status.flash(msg, C["accent_green"])
        else:
            self.status.flash("No sessions were renamed.")

    def scan_sessions(self):
        self.status.flash("Scanning Claude storage...", C["accent_orange"])
        self.root.update()
        discovered = discover_sessions()
        self._discovered_cache = discovered
        existing_ids = {s["session_id"] for s in self.sessions}
        new_sessions = [d for d in discovered if d["session_id"] not in existing_ids]

        if not new_sessions:
            self.status.flash("No new sessions found")
            messagebox.showinfo("Scan Complete", "All discovered sessions are already registered.")
            return

        self.status.flash(f"Found {len(new_sessions)} new sessions", C["accent_green"])
        ScanDialog(self.root, new_sessions, self.sessions,
                   lambda: (self._populate_list(), self._update_status_segments()))

    # --- Backup ---
    def _auto_backup_on_start(self):
        """Run backup silently in background thread on app startup."""
        def worker():
            try:
                new_count, skipped, size = backup_all_sessions()
                def report():
                    if new_count > 0:
                        mb = size / (1024 * 1024)
                        self.status.flash(
                            f"Auto-backup: {new_count} new/changed  ({mb:.1f} MB)  |  {skipped} up-to-date",
                            C["accent_green"]
                        )
                    else:
                        self.status.flash(f"Auto-backup: all {skipped} sessions up-to-date")
                self.root.after(0, report)
            except Exception as e:
                self.root.after(0, lambda: self.status.flash(f"Backup error: {e}", C["accent_red"]))
        threading.Thread(target=worker, daemon=True).start()

    def backup_now(self):
        """Manual backup triggered from toolbar."""
        self.status.flash("Backing up all sessions...", C["accent_orange"])
        self.root.update()

        def worker():
            try:
                new_count, skipped, size = backup_all_sessions()
                def report():
                    mb = size / (1024 * 1024)
                    msg = f"Backup complete: {new_count} new/changed ({mb:.1f} MB), {skipped} unchanged"
                    self.status.flash(msg, C["accent_green"])
                    messagebox.showinfo(
                        "Backup Complete",
                        f"New or changed backups: {new_count}\n"
                        f"Unchanged (skipped):   {skipped}\n"
                        f"Total new data:        {mb:.2f} MB\n\n"
                        f"Backup location:\n{BACKUP_DIR}"
                    )
                self.root.after(0, report)
            except Exception as e:
                self.root.after(0, lambda: messagebox.showerror("Backup Failed", str(e)))
        threading.Thread(target=worker, daemon=True).start()

    def restore_backup(self):
        """Show dialog to pick a session and restore from a backup copy."""
        session = self._get_selected_session()
        if not session:
            messagebox.showinfo("Restore Backup", "Select a session first to see its backups.")
            return
        session_id = session["session_id"]
        backups = list_session_backups(session_id)
        if not backups:
            messagebox.showinfo(
                "No Backups",
                f"No backups found for this session.\n\nSession ID: {session_id}\n\n"
                f"Run 'Backup' first to create snapshots."
            )
            return
        RestoreDialog(self.root, session, backups,
                      lambda: self.status.flash("Session restored from backup", C["accent_green"]))

    # --- Cost ---
    def _seed_discovered_async(self):
        """Background-warm the discovered cache so Last Used / Tree are instant."""
        def worker():
            try:
                disc = discover_sessions()
                def apply():
                    self._discovered_cache = disc
                    self._populate_list()
                self.root.after(0, apply)
            except Exception:
                pass
        threading.Thread(target=worker, daemon=True).start()

    def _mtime_for(self, session_id):
        """Return mtime of the session's .jsonl on disk, preferring discovered cache."""
        for d in self._discovered_cache:
            if d.get("session_id") == session_id:
                return d.get("mtime", 0)
        # Fall back to direct disk lookup
        for proj_dir in PROJECTS_DIR.iterdir():
            if not proj_dir.is_dir():
                continue
            candidate = proj_dir / f"{session_id}.jsonl"
            if candidate.exists():
                try:
                    return candidate.stat().st_mtime
                except Exception:
                    return 0
        return 0

    def _resolve_session_jsonl(self, session):
        """Find the .jsonl file for a session by id under ~/.claude/projects/*/."""
        sid = session.get("session_id", "")
        if not sid:
            return None
        for proj_dir in PROJECTS_DIR.iterdir():
            if not proj_dir.is_dir():
                continue
            candidate = proj_dir / f"{sid}.jsonl"
            if candidate.exists():
                return candidate
        return None

    def _compute_all_costs_async(self):
        """Walk every registered session's .jsonl and compute its cost in a thread."""
        def worker():
            try:
                pricing_table = _pricing.load_pricing()
            except Exception:
                pricing_table = {}

            updated = 0
            for s in self.sessions:
                sid = s.get("session_id", "")
                jsonl = self._resolve_session_jsonl(s)
                if not jsonl:
                    continue
                try:
                    stat = jsonl.stat()
                except Exception:
                    continue
                fp = f"{stat.st_size}:{int(stat.st_mtime)}"
                cached = self._cost_cache.get(sid)
                if cached and cached.get("fingerprint") == fp:
                    continue
                try:
                    result = _pricing.compute_session_cost(jsonl, pricing_table)
                    self._cost_cache[sid] = {
                        "usd": result["total_usd"],
                        "fingerprint": fp,
                        "by_model": result["by_model"],
                        "tokens": result["tokens"],
                    }
                    updated += 1
                except Exception:
                    pass

            def report():
                if updated > 0:
                    total = sum(v.get("usd", 0) for v in self._cost_cache.values())
                    self.status.flash(
                        f"Costs updated for {updated} sessions  |  Total: {_pricing.format_cost(total)}",
                        C["accent_green"]
                    )
                self._populate_list()
            self.root.after(0, report)
        threading.Thread(target=worker, daemon=True).start()

    def sync_costs(self):
        """Fetch latest pricing from LiteLLM, then recompute all session costs."""
        meta = _pricing.get_pricing_meta()
        last = meta.get("synced_at") if meta else "never"
        self.status.flash(f"Syncing pricing from LiteLLM... (last: {last})", C["accent_orange"])
        self.root.update()

        def worker():
            try:
                count, ts = _pricing.sync_pricing()
                # Invalidate cache to force recompute against new prices
                self._cost_cache.clear()
                self.root.after(0, lambda: self.status.flash(
                    f"Pricing synced ({count} models) — recomputing session costs...",
                    C["accent_green"]))
                self._compute_all_costs_async()
            except Exception as e:
                msg = str(e)
                self.root.after(0, lambda: messagebox.showerror(
                    "Sync Failed",
                    f"Could not fetch pricing from LiteLLM:\n{msg}\n\n"
                    f"Source: https://raw.githubusercontent.com/BerriAI/litellm/"
                    f"main/model_prices_and_context_window.json"
                ))
        threading.Thread(target=worker, daemon=True).start()

    # --- Help ---

    def _show_shortcuts(self):
        messagebox.showinfo("Keyboard Shortcuts",
            "Ctrl+N    New Session\n"
            "F2        Edit Session\n"
            "Del       Remove Session\n"
            "Enter     Launch Session\n"
            "Ctrl+L    Launch All\n"
            "Ctrl+O    Open Folder\n"
            "Ctrl+F    Find / Search\n"
            "Esc       Clear Search\n"
            "F5        Reload Sessions\n"
            "1 / 2     Switch Tab"
        )

    def _show_about(self):
        messagebox.showinfo("About Claude Session Manager",
            "Claude Session Manager (CSM) v1.0\n\n"
            "Manage, launch, and remote-control\n"
            "your Claude Code sessions.\n\n"
            f"Sessions file: {SESSIONS_FILE}\n"
            f"Claude home: {CLAUDE_HOME}"
        )


# --- Dialogs ---

class SessionDialog(tk.Toplevel):
    def __init__(self, parent, title="Session", session=None):
        super().__init__(parent)
        self.title(title)
        W, H = 700, 560
        self.geometry(f"{W}x{H}")
        self.configure(bg=C["bg"])
        self.resizable(True, True)
        self.minsize(600, 520)
        self.result = None
        self.transient(parent)
        self.grab_set()

        self.update_idletasks()
        x = parent.winfo_x() + (parent.winfo_width() - W) // 2
        y = parent.winfo_y() + (parent.winfo_height() - H) // 2
        self.geometry(f"+{x}+{y}")

        # Header
        header = tk.Frame(self, bg=C["bg_toolbar"], height=36)
        header.pack(fill="x")
        header.pack_propagate(False)
        tk.Label(header, text=f"  {title}", bg=C["bg_toolbar"], fg=C["text_bright"],
                 font=("Segoe UI", 13, "bold")).pack(side="left", fill="y")
        tk.Frame(self, bg=C["border"], height=1).pack(fill="x")

        # Form
        form = tk.Frame(self, bg=C["bg"])
        form.pack(fill="both", expand=True, padx=20, pady=12)

        fields = [
            ("Session Name:", "name", "Name displayed in the list"),
            ("Alias:", "alias", "Short keyword for CLI (e.g. domain)"),
            ("Session ID:", "session_id", "Claude session UUID or named key"),
            ("Working Directory:", "cwd", "Project folder path"),
        ]

        self.entries = {}
        for i, (label, key, hint) in enumerate(fields):
            tk.Label(form, text=label, bg=C["bg"], fg=C["text"],
                     font=("Segoe UI", 13)).grid(row=i * 2, column=0, sticky="w",
                                                  pady=(10, 0), columnspan=2)

            entry = tk.Entry(form, bg=C["bg_input"], fg=C["text_bright"],
                             insertbackground=C["text_bright"],
                             font=("Segoe UI", 13), relief="flat",
                             highlightthickness=1,
                             highlightbackground=C["border"],
                             highlightcolor=C["accent"])
            entry.grid(row=i * 2 + 1, column=0, sticky="ew", ipady=4)

            if session and key in session:
                entry.insert(0, session[key])

            self.entries[key] = entry

            if key == "cwd":
                browse = tk.Button(form, text="...", bg=C["bg_toolbar"], fg=C["text"],
                                    font=("Segoe UI", 13), relief="flat", width=3,
                                    cursor="hand2", command=self._browse_dir,
                                    activebackground=C["btn_hover"])
                browse.grid(row=i * 2 + 1, column=1, padx=(4, 0), ipady=4, sticky="ns")

        form.columnconfigure(0, weight=1)

        # Model selector
        model_row = len(fields) * 2
        tk.Label(form, text="Model:", bg=C["bg"], fg=C["text"],
                 font=("Segoe UI", 13)).grid(row=model_row, column=0, sticky="w",
                                              pady=(14, 0), columnspan=2)
        current_model_id = (session.get("model", "") if session else "")
        current_label = MODEL_LABEL_BY_ID.get(current_model_id, MODEL_CHOICES[0][0])
        self.model_var = tk.StringVar(value=current_label)
        model_box = ttk.Combobox(form, textvariable=self.model_var,
                                 values=[lbl for lbl, _ in MODEL_CHOICES],
                                 state="readonly", font=("Segoe UI", 13))
        model_box.grid(row=model_row + 1, column=0, sticky="ew", ipady=4, columnspan=2)

        # Skip permissions
        self.skip_var = tk.BooleanVar(
            value=session.get("skip_permissions", True) if session else True)
        ttk.Checkbutton(form, text="Skip permission prompts",
                        variable=self.skip_var).grid(
            row=model_row + 2, column=0, sticky="w", pady=(14, 0), columnspan=2)

        # Buttons
        tk.Frame(self, bg=C["border"], height=1).pack(fill="x", side="bottom", pady=(0, 0))
        btn_bar = tk.Frame(self, bg=C["bg_lighter"], height=44)
        btn_bar.pack(fill="x", side="bottom")
        btn_bar.pack_propagate(False)

        tk.Button(btn_bar, text="Cancel", bg=C["bg_toolbar"], fg=C["text"],
                  font=("Segoe UI", 13), relief="flat", padx=16, pady=4,
                  cursor="hand2", command=self.destroy,
                  activebackground=C["btn_hover"]).pack(side="right", padx=8, pady=8)

        tk.Button(btn_bar, text="Save", bg=C["accent"], fg=C["text_white"],
                  font=("Segoe UI", 12, "bold"), relief="flat", padx=16, pady=4,
                  cursor="hand2", command=self._save,
                  activebackground="#1a8ad4").pack(side="right", pady=8)

    def _browse_dir(self):
        path = filedialog.askdirectory()
        if path:
            self.entries["cwd"].delete(0, "end")
            self.entries["cwd"].insert(0, path)

    def _save(self):
        name = self.entries["name"].get().strip()
        session_id = self.entries["session_id"].get().strip()

        if not name or not session_id:
            messagebox.showwarning("Required", "Session Name and Session ID are required.")
            return

        self.result = {
            "name": name,
            "alias": self.entries["alias"].get().strip() or name.lower().replace(" ", "-"),
            "session_id": session_id,
            "cwd": self.entries["cwd"].get().strip() or ".",
            "skip_permissions": self.skip_var.get(),
            "model": MODEL_ID_BY_LABEL.get(self.model_var.get(), ""),
        }
        self.destroy()


class ScanDialog(tk.Toplevel):
    def __init__(self, parent, new_sessions, sessions_list, refresh_cb):
        super().__init__(parent)
        self.title(f"Scan Results - {len(new_sessions)} Found")
        self.geometry("700x480")
        self.configure(bg=C["bg"])
        self.transient(parent)
        self.grab_set()

        self.new_sessions = new_sessions
        self.sessions_list = sessions_list
        self.refresh = refresh_cb

        self.update_idletasks()
        x = parent.winfo_x() + (parent.winfo_width() - 700) // 2
        y = parent.winfo_y() + (parent.winfo_height() - 480) // 2
        self.geometry(f"+{x}+{y}")

        # Header
        header = tk.Frame(self, bg=C["bg_toolbar"], height=36)
        header.pack(fill="x")
        header.pack_propagate(False)
        tk.Label(header, text=f"  {len(new_sessions)} sessions discovered",
                 bg=C["bg_toolbar"], fg=C["text_bright"],
                 font=("Segoe UI", 13, "bold")).pack(side="left", fill="y")
        tk.Frame(self, bg=C["border"], height=1).pack(fill="x")

        tk.Label(self, text="Select sessions to import (Ctrl+click for multiple):",
                 bg=C["bg"], fg=C["text_dim"], font=("Segoe UI", 13)
                 ).pack(padx=12, pady=(8, 4), anchor="w")

        # List
        tree_frame = tk.Frame(self, bg=C["bg"])
        tree_frame.pack(fill="both", expand=True, padx=12, pady=4)

        self.scan_tree = ttk.Treeview(tree_frame, columns=("id", "name", "dir"),
                                       show="headings", selectmode="extended")
        self.scan_tree.heading("id", text="Session ID", anchor="w")
        self.scan_tree.heading("name", text="Name", anchor="w")
        self.scan_tree.heading("dir", text="Source", anchor="w")
        self.scan_tree.column("id", width=240)
        self.scan_tree.column("name", width=160)
        self.scan_tree.column("dir", width=260)
        self.scan_tree.pack(fill="both", expand=True)

        for i, ns in enumerate(new_sessions):
            tag = "odd" if i % 2 == 0 else "even"
            sid = ns["session_id"]
            sid_display = sid[:28] + "..." if len(sid) > 28 else sid
            self.scan_tree.insert("", "end", iid=ns["session_id"],
                                   values=(sid_display, ns.get("name", ""), ns.get("source_dir", "")),
                                   tags=(tag,))

        # Bottom buttons
        tk.Frame(self, bg=C["border"], height=1).pack(fill="x")
        btn_bar = tk.Frame(self, bg=C["bg_lighter"], height=44)
        btn_bar.pack(fill="x")
        btn_bar.pack_propagate(False)

        tk.Button(btn_bar, text="Cancel", bg=C["bg_toolbar"], fg=C["text"],
                  font=("Segoe UI", 13), relief="flat", padx=12, pady=4,
                  cursor="hand2", command=self.destroy).pack(side="right", padx=8, pady=8)

        tk.Button(btn_bar, text="Import All", bg=C["accent_green"], fg=C["text_white"],
                  font=("Segoe UI", 12, "bold"), relief="flat", padx=12, pady=4,
                  cursor="hand2", command=self._import_all).pack(side="right", pady=8, padx=2)

        tk.Button(btn_bar, text="Import Selected", bg=C["accent"], fg=C["text_white"],
                  font=("Segoe UI", 12, "bold"), relief="flat", padx=12, pady=4,
                  cursor="hand2", command=self._import_selected).pack(side="right", pady=8)

    def _import_selected(self):
        selected = self.scan_tree.selection()
        count = 0
        for sid in selected:
            for ns in self.new_sessions:
                if ns["session_id"] == sid:
                    self.sessions_list.append({
                        "name": ns.get("name", sid[:12]),
                        "alias": ns.get("name", sid[:8]).lower().replace(" ", "-"),
                        "session_id": ns["session_id"],
                        "cwd": ns.get("cwd", ""),
                        "skip_permissions": True,
                    })
                    count += 1
                    break
        save_sessions(self.sessions_list)
        self.refresh()
        self.destroy()

    def _import_all(self):
        for ns in self.new_sessions:
            self.sessions_list.append({
                "name": ns.get("name", ns["session_id"][:12]),
                "alias": ns.get("name", ns["session_id"][:8]).lower().replace(" ", "-"),
                "session_id": ns["session_id"],
                "cwd": ns.get("cwd", ""),
                "skip_permissions": True,
            })
        save_sessions(self.sessions_list)
        self.refresh()
        self.destroy()


class RestoreDialog(tk.Toplevel):
    def __init__(self, parent, session, backups, on_success):
        super().__init__(parent)
        self.session = session
        self.backups = backups
        self.on_success = on_success
        self.title(f"Restore Backup - {session.get('name', '')}")
        self.geometry("760x460")
        self.configure(bg=C["bg"])
        self.transient(parent)
        self.grab_set()

        self.update_idletasks()
        x = parent.winfo_x() + (parent.winfo_width() - 760) // 2
        y = parent.winfo_y() + (parent.winfo_height() - 460) // 2
        self.geometry(f"+{x}+{y}")

        # Header
        header = tk.Frame(self, bg=C["bg_toolbar"], height=44)
        header.pack(fill="x")
        header.pack_propagate(False)
        tk.Label(header, text=f"  Restore: {session.get('name', '')}",
                 bg=C["bg_toolbar"], fg=C["text_bright"],
                 font=("Segoe UI", 14, "bold")).pack(side="left", fill="y")
        tk.Frame(self, bg=C["border"], height=1).pack(fill="x")

        tk.Label(self, text=f"Session ID: {session['session_id']}",
                 bg=C["bg"], fg=C["text_dim"], font=("Segoe UI", 11)
                 ).pack(padx=12, pady=(8, 0), anchor="w")
        tk.Label(self, text=f"Found {len(backups)} backup snapshot(s). Newest first.",
                 bg=C["bg"], fg=C["text_dim"], font=("Segoe UI", 11)
                 ).pack(padx=12, pady=(0, 6), anchor="w")

        # List
        tree_frame = tk.Frame(self, bg=C["bg"])
        tree_frame.pack(fill="both", expand=True, padx=12, pady=4)

        self.backup_tree = ttk.Treeview(tree_frame, columns=("when", "size", "path"),
                                         show="headings", selectmode="browse")
        self.backup_tree.heading("when", text="Backup Time", anchor="w")
        self.backup_tree.heading("size", text="Size", anchor="e")
        self.backup_tree.heading("path", text="Location", anchor="w")
        self.backup_tree.column("when", width=180)
        self.backup_tree.column("size", width=100, anchor="e")
        self.backup_tree.column("path", width=440)
        self.backup_tree.pack(fill="both", expand=True)

        for i, bp in enumerate(backups):
            stat = bp.stat()
            when = datetime.fromtimestamp(stat.st_mtime).strftime("%Y-%m-%d  %H:%M:%S")
            size_mb = stat.st_size / (1024 * 1024)
            size_str = f"{size_mb:.2f} MB" if size_mb >= 0.1 else f"{stat.st_size // 1024} KB"
            self.backup_tree.insert("", "end", iid=str(i), values=(when, size_str, str(bp)))

        if backups:
            self.backup_tree.selection_set("0")

        # Bottom buttons
        tk.Frame(self, bg=C["border"], height=1).pack(fill="x")
        btn_bar = tk.Frame(self, bg=C["bg_lighter"], height=52)
        btn_bar.pack(fill="x")
        btn_bar.pack_propagate(False)

        tk.Button(btn_bar, text="Cancel", bg=C["bg_toolbar"], fg=C["text"],
                  font=("Segoe UI", 13), relief="flat", padx=14, pady=6,
                  cursor="hand2", command=self.destroy).pack(side="right", padx=8, pady=10)

        tk.Button(btn_bar, text="Restore Selected", bg=C["accent"], fg=C["text_white"],
                  font=("Segoe UI", 13, "bold"), relief="flat", padx=14, pady=6,
                  cursor="hand2", command=self._do_restore).pack(side="right", pady=10)

    def _do_restore(self):
        sel = self.backup_tree.selection()
        if not sel:
            return
        idx = int(sel[0])
        backup_path = self.backups[idx]

        # Determine source_dir from the backup path: <BACKUP_DIR>/<project>/<session_id>/<file>
        try:
            project_dir_name = backup_path.parent.parent.name
        except Exception:
            project_dir_name = None

        if not project_dir_name:
            messagebox.showerror("Restore Failed", "Could not determine source project directory.")
            return

        confirm = messagebox.askyesno(
            "Confirm Restore",
            f"Restore this backup?\n\n"
            f"From: {backup_path.name}\n"
            f"Into: ~/.claude/projects/{project_dir_name}/{self.session['session_id']}.jsonl\n\n"
            f"If a current file exists, it will be saved as a .pre-restore-* copy."
        )
        if not confirm:
            return

        try:
            dest = restore_session_backup(backup_path, self.session["session_id"], project_dir_name)
            messagebox.showinfo("Restored", f"Session restored to:\n{dest}")
            self.on_success()
            self.destroy()
        except Exception as e:
            messagebox.showerror("Restore Failed", str(e))


class AutoRenameDialog(tk.Toplevel):
    """Preview proposed session names side-by-side and let the user edit each
    line before applying. Empty proposed name = skip that row."""

    SOURCE_COLORS = {
        "root":    C["accent_green"],
        "branch":  C["accent_orange"],
        "missing": C["text_dim"],
    }
    SOURCE_LABELS = {
        "root":    "ROOT",
        "branch":  "BRANCH",
        "missing": "MISSING",
    }

    def __init__(self, parent, app, proposals, on_apply):
        super().__init__(parent)
        self.app = app
        self.proposals = proposals
        self.on_apply = on_apply
        self.entry_vars = {}  # session_id -> tk.StringVar
        self.row_checked = {}  # session_id -> tk.BooleanVar

        W, H = 1100, 680
        self.title("Auto-rename Sessions")
        self.geometry(f"{W}x{H}")
        self.minsize(900, 540)
        self.configure(bg=C["bg"])
        self.transient(parent)
        self.grab_set()
        self.update_idletasks()
        x = parent.winfo_x() + (parent.winfo_width() - W) // 2
        y = parent.winfo_y() + (parent.winfo_height() - H) // 2
        self.geometry(f"+{x}+{y}")

        # Header
        header = tk.Frame(self, bg=C["bg_toolbar"], height=46)
        header.pack(fill="x")
        header.pack_propagate(False)
        tk.Label(header, text="  Auto-rename Sessions",
                 bg=C["bg_toolbar"], fg=C["text_bright"],
                 font=("Segoe UI", 15, "bold")).pack(side="left", fill="y")
        tk.Frame(self, bg=C["border"], height=1).pack(fill="x")

        # Counts summary
        n_change   = sum(1 for p in proposals if p["proposed"] and p["proposed"] != p["current"])
        n_same     = sum(1 for p in proposals if p["proposed"] and p["proposed"] == p["current"])
        n_skipped  = sum(1 for p in proposals if not p["proposed"])
        summary = tk.Frame(self, bg=C["bg"])
        summary.pack(fill="x", padx=18, pady=(10, 6))
        tk.Label(summary,
                 text=f"{n_change} will change   ·   {n_same} already match   ·   {n_skipped} skipped (missing .jsonl)",
                 bg=C["bg"], fg=C["text_dim"], font=("Segoe UI", 12)).pack(anchor="w")
        tk.Label(summary,
                 text="Uncheck rows you want to keep. Edit the proposed name inline if you want a different label.",
                 bg=C["bg"], fg=C["text_dim"], font=("Segoe UI", 11)).pack(anchor="w", pady=(2, 0))

        # Scrollable list of rows
        list_outer = tk.Frame(self, bg=C["bg"])
        list_outer.pack(fill="both", expand=True, padx=18, pady=4)

        self.canvas = tk.Canvas(list_outer, bg=C["bg"], highlightthickness=0)
        scrollbar = ttk.Scrollbar(list_outer, orient="vertical", command=self.canvas.yview)
        self.canvas.configure(yscrollcommand=scrollbar.set)
        self.canvas.pack(side="left", fill="both", expand=True)
        scrollbar.pack(side="right", fill="y")

        self.rows_frame = tk.Frame(self.canvas, bg=C["bg"])
        self.canvas_window = self.canvas.create_window((0, 0), window=self.rows_frame, anchor="nw")

        def _on_frame_configure(event):
            self.canvas.configure(scrollregion=self.canvas.bbox("all"))
        def _on_canvas_configure(event):
            self.canvas.itemconfig(self.canvas_window, width=event.width)
        def _on_mousewheel(event):
            self.canvas.yview_scroll(int(-1 * (event.delta / 120)), "units")
        self.rows_frame.bind("<Configure>", _on_frame_configure)
        self.canvas.bind("<Configure>", _on_canvas_configure)
        self.canvas.bind_all("<MouseWheel>", _on_mousewheel)

        # Column headers
        hdr = tk.Frame(self.rows_frame, bg=C["bg_lighter"])
        hdr.pack(fill="x", pady=(0, 2))
        tk.Label(hdr, text="", bg=C["bg_lighter"], width=3).pack(side="left", padx=(6, 0))
        tk.Label(hdr, text="Current name", bg=C["bg_lighter"], fg=C["text_dim"],
                 font=("Segoe UI", 11, "bold"), width=32, anchor="w").pack(side="left", padx=4, pady=4)
        tk.Label(hdr, text="Proposed name (editable)", bg=C["bg_lighter"], fg=C["text_dim"],
                 font=("Segoe UI", 11, "bold"), anchor="w").pack(side="left", fill="x", expand=True,
                                                                  padx=4, pady=4)
        tk.Label(hdr, text="Type", bg=C["bg_lighter"], fg=C["text_dim"],
                 font=("Segoe UI", 11, "bold"), width=10, anchor="center").pack(side="right", padx=8)

        # Rows — only show entries with a proposed name (skip missing-on-disk)
        for i, p in enumerate(proposals):
            sid = p["session"].get("session_id", "")
            if not p["proposed"]:
                continue
            changed = p["proposed"] != p["current"]
            bg = C["bg"] if i % 2 == 0 else C["bg_light"]
            row = tk.Frame(self.rows_frame, bg=bg)
            row.pack(fill="x")

            # Checkbox — pre-checked only if the row changes something
            chk_var = tk.BooleanVar(value=changed)
            self.row_checked[sid] = chk_var
            chk = tk.Checkbutton(row, variable=chk_var, bg=bg,
                                  activebackground=bg, borderwidth=0,
                                  highlightthickness=0, selectcolor=C["bg_input"])
            chk.pack(side="left", padx=(6, 0))

            # Current name
            tk.Label(row, text=p["current"] or "—", bg=bg, fg=C["text"],
                     font=("Segoe UI", 11), width=32, anchor="w").pack(side="left", padx=4, pady=4)

            # Editable proposed name
            var = tk.StringVar(value=p["proposed"])
            self.entry_vars[sid] = var
            entry = tk.Entry(row, textvariable=var, bg=C["bg_input"], fg=C["text_bright"],
                             insertbackground=C["text_bright"], font=("Segoe UI", 11),
                             relief="flat", highlightthickness=1,
                             highlightbackground=C["border"], highlightcolor=C["accent"])
            entry.pack(side="left", fill="x", expand=True, padx=4, pady=4, ipady=2)

            # Type badge
            badge_text = self.SOURCE_LABELS.get(p["source"], "?")
            badge_color = self.SOURCE_COLORS.get(p["source"], C["text_dim"])
            tk.Label(row, text=badge_text, bg=bg, fg=badge_color,
                     font=("Segoe UI", 10, "bold"), width=10, anchor="center").pack(side="right", padx=8)

        # Buttons
        tk.Frame(self, bg=C["border"], height=1).pack(fill="x")
        btn_bar = tk.Frame(self, bg=C["bg_lighter"], height=56)
        btn_bar.pack(fill="x")
        btn_bar.pack_propagate(False)

        tk.Button(btn_bar, text="Cancel", bg=C["bg_toolbar"], fg=C["text"],
                  font=("Segoe UI", 13), relief="flat", padx=16, pady=6,
                  cursor="hand2", command=self._on_cancel).pack(side="right", padx=8, pady=10)

        tk.Button(btn_bar, text="Select all", bg=C["bg_toolbar"], fg=C["text"],
                  font=("Segoe UI", 12), relief="flat", padx=14, pady=6,
                  cursor="hand2", command=lambda: self._toggle_all(True)
                  ).pack(side="left", padx=(14, 4), pady=10)
        tk.Button(btn_bar, text="Select none", bg=C["bg_toolbar"], fg=C["text"],
                  font=("Segoe UI", 12), relief="flat", padx=14, pady=6,
                  cursor="hand2", command=lambda: self._toggle_all(False)
                  ).pack(side="left", padx=4, pady=10)

        tk.Button(btn_bar, text="Apply Selected", bg=C["accent"], fg=C["text_white"],
                  font=("Segoe UI", 13, "bold"), relief="flat", padx=16, pady=6,
                  cursor="hand2", command=self._on_apply).pack(side="right", pady=10)

    def _toggle_all(self, value):
        for var in self.row_checked.values():
            var.set(value)

    def _on_cancel(self):
        self.canvas.unbind_all("<MouseWheel>")
        self.destroy()

    def _on_apply(self):
        accepted = {}
        for sid, var in self.entry_vars.items():
            if not self.row_checked.get(sid, tk.BooleanVar(value=False)).get():
                continue
            name = var.get().strip()
            if name:
                accepted[sid] = name
        if not accepted:
            messagebox.showinfo("Auto-rename", "No rows selected. Nothing to do.")
            return
        if not messagebox.askyesno(
            "Confirm rename",
            f"Rename {len(accepted)} sessions?\n\n"
            f"Your current sessions.json will be backed up as\n"
            f"sessions.json.pre-rename-<timestamp> before changes are applied."
        ):
            return
        self.canvas.unbind_all("<MouseWheel>")
        self.on_apply(accepted)
        self.destroy()


class SessionPromptsDialog(tk.Toplevel):
    """Two-pane dialog: searchable list of every user prompt (top), full text of
    the selected prompt (bottom). Click a row to expand it."""

    def __init__(self, parent, session, prompts):
        super().__init__(parent)
        self.session = session
        self.all_prompts = prompts

        W, H = 1100, 720
        self.title(f"Prompts — {session.get('name','')}")
        self.geometry(f"{W}x{H}")
        self.minsize(820, 560)
        self.configure(bg=C["bg"])
        self.transient(parent)
        self.update_idletasks()
        x = parent.winfo_x() + (parent.winfo_width() - W) // 2
        y = parent.winfo_y() + (parent.winfo_height() - H) // 2
        self.geometry(f"+{x}+{y}")

        # Header
        header = tk.Frame(self, bg=C["bg_toolbar"], height=52)
        header.pack(fill="x")
        header.pack_propagate(False)
        tk.Label(header, text=f"  {session.get('name','')}",
                 bg=C["bg_toolbar"], fg=C["text_bright"],
                 font=("Segoe UI", 14, "bold")).pack(side="left", fill="y")
        tk.Label(header, text=f"{len(prompts)} prompts   ·   {session.get('session_id','')[:8]}  ",
                 bg=C["bg_toolbar"], fg=C["text_dim"],
                 font=("Segoe UI", 11)).pack(side="right", fill="y")
        tk.Frame(self, bg=C["border"], height=1).pack(fill="x")

        # Search row
        search_row = tk.Frame(self, bg=C["bg"])
        search_row.pack(fill="x", padx=14, pady=(10, 6))
        tk.Label(search_row, text="Find:", bg=C["bg"], fg=C["text_dim"],
                 font=("Segoe UI", 12)).pack(side="left", padx=(0, 6))
        self.search_var = tk.StringVar()
        self.search_var.trace_add("write", lambda *a: self._refilter())
        search_entry = tk.Entry(search_row, textvariable=self.search_var,
                                 bg=C["bg_input"], fg=C["text_bright"],
                                 insertbackground=C["text_bright"],
                                 font=("Segoe UI", 12), relief="flat",
                                 highlightthickness=1, highlightbackground=C["border"],
                                 highlightcolor=C["accent"])
        search_entry.pack(side="left", fill="x", expand=True, ipady=3)
        search_entry.focus_set()

        # Match count label updated on filter
        self.match_label = tk.Label(search_row, text="", bg=C["bg"], fg=C["text_dim"],
                                     font=("Segoe UI", 11))
        self.match_label.pack(side="right", padx=(10, 0))

        # PanedWindow: top = list, bottom = full text
        paned = tk.PanedWindow(self, orient="vertical", bg=C["border"], sashwidth=4,
                                bd=0, relief="flat")
        paned.pack(fill="both", expand=True, padx=14, pady=(2, 12))

        # ----- Top: prompt list -----
        list_frame = tk.Frame(paned, bg=C["bg"])
        paned.add(list_frame, minsize=200, height=380)

        cols = ("idx", "when", "snippet")
        self.tree = ttk.Treeview(list_frame, columns=cols, show="headings", selectmode="browse")
        self.tree.heading("idx", text="#", anchor="e")
        self.tree.heading("when", text="When", anchor="w")
        self.tree.heading("snippet", text="Prompt", anchor="w")
        self.tree.column("idx", width=50, anchor="e", stretch=False)
        self.tree.column("when", width=170, anchor="w", stretch=False)
        self.tree.column("snippet", width=720, anchor="w", stretch=True)
        self.tree.tag_configure("odd", background=C["bg"])
        self.tree.tag_configure("even", background=C["bg_light"])

        scrollbar = ttk.Scrollbar(list_frame, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=scrollbar.set)
        self.tree.pack(side="left", fill="both", expand=True)
        scrollbar.pack(side="right", fill="y")
        self.tree.bind("<<TreeviewSelect>>", lambda e: self._on_select())

        # ----- Bottom: full text -----
        detail_frame = tk.Frame(paned, bg=C["bg"])
        paned.add(detail_frame, minsize=160, height=240)

        tk.Label(detail_frame, text="Full text of selected prompt:",
                 bg=C["bg"], fg=C["text_dim"], font=("Segoe UI", 11),
                 anchor="w").pack(fill="x", padx=4, pady=(6, 4))

        text_outer = tk.Frame(detail_frame, bg=C["bg"])
        text_outer.pack(fill="both", expand=True, padx=2, pady=2)
        self.text = tk.Text(text_outer, bg=C["bg_input"], fg=C["text_bright"],
                             insertbackground=C["text_bright"], font=("Consolas", 12),
                             relief="flat", highlightthickness=1,
                             highlightbackground=C["border"], wrap="word",
                             padx=10, pady=8)
        text_scroll = ttk.Scrollbar(text_outer, orient="vertical", command=self.text.yview)
        self.text.configure(yscrollcommand=text_scroll.set, state="disabled")
        self.text.pack(side="left", fill="both", expand=True)
        text_scroll.pack(side="right", fill="y")

        # Buttons
        tk.Frame(self, bg=C["border"], height=1).pack(fill="x", side="bottom")
        btn_bar = tk.Frame(self, bg=C["bg_lighter"], height=48)
        btn_bar.pack(fill="x", side="bottom")
        btn_bar.pack_propagate(False)
        tk.Button(btn_bar, text="Close", bg=C["bg_toolbar"], fg=C["text"],
                  font=("Segoe UI", 12), relief="flat", padx=16, pady=6,
                  cursor="hand2", command=self.destroy).pack(side="right", padx=8, pady=8)
        tk.Button(btn_bar, text="Copy selected prompt", bg=C["bg_toolbar"], fg=C["text"],
                  font=("Segoe UI", 12), relief="flat", padx=14, pady=6,
                  cursor="hand2", command=self._copy_selected).pack(side="right", padx=4, pady=8)

        self.bind("<Escape>", lambda e: self.destroy())

        self._populate(prompts)

    def _populate(self, prompts):
        self.tree.delete(*self.tree.get_children())
        for i, p in enumerate(prompts):
            snippet = " ".join(p["text"].split())
            if len(snippet) > 200:
                snippet = snippet[:200] + "..."
            when = self._fmt_when(p.get("timestamp", ""))
            tag = "odd" if i % 2 == 0 else "even"
            self.tree.insert("", "end", iid=str(p["idx"]),
                             values=(p["idx"], when, snippet), tags=(tag,))
        self.match_label.config(text=f"{len(prompts)} of {len(self.all_prompts)} shown")
        # Auto-select first row so the detail pane has content
        if prompts:
            first_iid = str(prompts[0]["idx"])
            self.tree.selection_set(first_iid)
            self.tree.focus(first_iid)

    def _refilter(self):
        q = self.search_var.get().strip().lower()
        if not q:
            self._populate(self.all_prompts)
            return
        filtered = [p for p in self.all_prompts if q in p["text"].lower()]
        self._populate(filtered)

    def _on_select(self):
        sel = self.tree.selection()
        if not sel:
            return
        try:
            idx = int(sel[0])
        except ValueError:
            return
        p = next((x for x in self.all_prompts if x["idx"] == idx), None)
        if not p:
            return
        self.text.config(state="normal")
        self.text.delete("1.0", "end")
        self.text.insert("1.0", p["text"])
        self.text.config(state="disabled")

    def _copy_selected(self):
        sel = self.tree.selection()
        if not sel:
            return
        try:
            idx = int(sel[0])
        except ValueError:
            return
        p = next((x for x in self.all_prompts if x["idx"] == idx), None)
        if not p:
            return
        self.clipboard_clear()
        self.clipboard_append(p["text"])
        # Brief flash on the title bar
        original = self.title()
        self.title(original + "   (copied)")
        self.after(900, lambda: self.title(original))

    @staticmethod
    def _fmt_when(ts_iso):
        if not ts_iso:
            return ""
        # Examples: "2026-04-13T11:32:45.123Z" -> "2026-04-13 11:32"
        if "T" in ts_iso:
            date, rest = ts_iso.split("T", 1)
            time = rest.split(".")[0].split("Z")[0][:5]
            return f"{date} {time}"
        return ts_iso[:16]


class RunTaskDialog(tk.Toplevel):
    """Compose a one-shot `claude -p` task to run in a target session's project."""

    def __init__(self, parent, target_session, all_sessions, on_done):
        super().__init__(parent)
        self.target = target_session
        self.all_sessions = all_sessions
        self.on_done = on_done

        W, H = 760, 620
        self.title(f"Run Task - {target_session.get('name','')}")
        self.geometry(f"{W}x{H}")
        self.minsize(640, 540)
        self.configure(bg=C["bg"])
        self.transient(parent)
        self.grab_set()

        self.update_idletasks()
        x = parent.winfo_x() + (parent.winfo_width() - W) // 2
        y = parent.winfo_y() + (parent.winfo_height() - H) // 2
        self.geometry(f"+{x}+{y}")

        # Header
        header = tk.Frame(self, bg=C["bg_toolbar"], height=44)
        header.pack(fill="x")
        header.pack_propagate(False)
        tk.Label(header, text=f"  Run Task in: {target_session.get('name','')}",
                 bg=C["bg_toolbar"], fg=C["text_bright"],
                 font=("Segoe UI", 14, "bold")).pack(side="left", fill="y")
        tk.Frame(self, bg=C["border"], height=1).pack(fill="x")

        body = tk.Frame(self, bg=C["bg"])
        body.pack(fill="both", expand=True, padx=18, pady=12)

        tk.Label(body, text=f"Target cwd:  {target_session.get('cwd','')}",
                 bg=C["bg"], fg=C["text_dim"], font=("Segoe UI", 11)
                 ).pack(anchor="w", pady=(0, 8))

        # Prompt
        tk.Label(body, text="Task prompt:", bg=C["bg"], fg=C["text"],
                 font=("Segoe UI", 13)).pack(anchor="w")
        self.prompt_text = tk.Text(body, height=8, bg=C["bg_input"], fg=C["text_bright"],
                                    insertbackground=C["text_bright"],
                                    font=("Segoe UI", 13), relief="flat",
                                    highlightthickness=1, highlightbackground=C["border"],
                                    highlightcolor=C["accent"], wrap="word")
        self.prompt_text.pack(fill="both", expand=True, pady=(4, 10))

        # Tools
        row1 = tk.Frame(body, bg=C["bg"])
        row1.pack(fill="x", pady=(0, 8))
        tk.Label(row1, text="Allowed tools:", bg=C["bg"], fg=C["text"],
                 font=("Segoe UI", 13), width=14, anchor="w").pack(side="left")
        self.tools_var = tk.StringVar(value="Read,Edit,Bash")
        tk.Entry(row1, textvariable=self.tools_var, bg=C["bg_input"], fg=C["text_bright"],
                 insertbackground=C["text_bright"], font=("Segoe UI", 13), relief="flat",
                 highlightthickness=1, highlightbackground=C["border"],
                 highlightcolor=C["accent"]).pack(side="left", fill="x", expand=True, ipady=3)

        # Continue checkbox
        self.continue_var = tk.BooleanVar(value=False)
        ttk.Checkbutton(body, text="Append to target's existing conversation (--continue)",
                        variable=self.continue_var).pack(anchor="w", pady=(4, 6))

        # Context group
        ctx_frame = tk.LabelFrame(body, text="Include context from another session (optional)",
                                   bg=C["bg"], fg=C["text"], font=("Segoe UI", 12),
                                   bd=1, relief="solid", labelanchor="nw")
        ctx_frame.pack(fill="x", pady=(8, 6))

        ctx_row = tk.Frame(ctx_frame, bg=C["bg"])
        ctx_row.pack(fill="x", padx=8, pady=8)

        tk.Label(ctx_row, text="From session:", bg=C["bg"], fg=C["text"],
                 font=("Segoe UI", 12), width=14, anchor="w").pack(side="left")
        names = ["(none)"] + [s.get("name", s.get("alias", s.get("session_id", "?")))
                              for s in all_sessions]
        self.source_var = tk.StringVar(value="(none)")
        ttk.Combobox(ctx_row, textvariable=self.source_var, values=names,
                     state="readonly", font=("Segoe UI", 12), width=32
                     ).pack(side="left", padx=(0, 12))

        tk.Label(ctx_row, text="Last N turns:", bg=C["bg"], fg=C["text"],
                 font=("Segoe UI", 12)).pack(side="left", padx=(0, 4))
        self.turns_var = tk.IntVar(value=20)
        tk.Spinbox(ctx_row, from_=0, to=200, textvariable=self.turns_var, width=5,
                   font=("Segoe UI", 12)).pack(side="left")

        # Buttons
        tk.Frame(self, bg=C["border"], height=1).pack(fill="x", side="bottom")
        btn_bar = tk.Frame(self, bg=C["bg_lighter"], height=52)
        btn_bar.pack(fill="x", side="bottom")
        btn_bar.pack_propagate(False)

        tk.Button(btn_bar, text="Cancel", bg=C["bg_toolbar"], fg=C["text"],
                  font=("Segoe UI", 13), relief="flat", padx=14, pady=6,
                  cursor="hand2", command=self.destroy).pack(side="right", padx=8, pady=10)

        tk.Button(btn_bar, text="Run Task", bg=C["accent"], fg=C["text_white"],
                  font=("Segoe UI", 13, "bold"), relief="flat", padx=14, pady=6,
                  cursor="hand2", command=self._run).pack(side="right", pady=10)

        self.prompt_text.focus_set()

    def _run(self):
        prompt = self.prompt_text.get("1.0", "end").strip()
        if not prompt:
            messagebox.showwarning("Required", "Please type a task prompt.")
            return

        # Resolve source session for context (if any)
        source_session = None
        sel_name = self.source_var.get()
        if sel_name and sel_name != "(none)":
            for s in self.all_sessions:
                if s.get("name") == sel_name:
                    source_session = s
                    break

        n = max(0, int(self.turns_var.get() or 0))

        try:
            # Reuse the CLI helper module so we have one source of truth
            csm_task_path = SCRIPT_DIR / "csm_task.py"
            if not csm_task_path.exists():
                messagebox.showerror("Missing helper",
                                      f"csm_task.py not found beside csm.pyw:\n{csm_task_path}")
                return
            cmd = [sys.executable, str(csm_task_path),
                   self.target.get("alias") or self.target.get("session_id"),
                   prompt,
                   "--tools", self.tools_var.get().strip() or "Read,Edit,Bash"]
            if self.continue_var.get():
                cmd.append("--continue")
            if source_session and n > 0:
                cmd.extend(["--with-context", str(n),
                            "--from", source_session.get("alias") or source_session.get("session_id")])

            subprocess.Popen(cmd, creationflags=subprocess.CREATE_NO_WINDOW)
            self.on_done(self.target.get("name", ""))
            self.destroy()
        except Exception as e:
            messagebox.showerror("Run Task Failed", str(e))


# --- Entry Point ---

def main():
    root = tk.Tk()
    # Scale UI for high-DPI displays
    try:
        dpi = root.winfo_fpixels("1i")
        scale = dpi / 96.0
        if scale > 1.0:
            root.tk.call("tk", "scaling", scale)
    except Exception:
        pass
    app = SessionManagerApp(root)
    root.mainloop()


if __name__ == "__main__":
    main()
