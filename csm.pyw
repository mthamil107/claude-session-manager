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
            try:
                with open(jsonl, "r", encoding="utf-8", errors="ignore") as f:
                    for line in f:
                        try:
                            rec = json.loads(line)
                        except Exception:
                            continue
                        # Grab cwd from first record that has it
                        if real_cwd is None and isinstance(rec.get("cwd"), str) and rec["cwd"]:
                            real_cwd = rec["cwd"]
                        # Grab first real user message as preview
                        if not preview and rec.get("type") == "user":
                            msg = rec.get("message", {})
                            content = msg.get("content", "")
                            if isinstance(content, list):
                                for c in content:
                                    if isinstance(c, dict) and c.get("type") == "text":
                                        content = c.get("text", "")
                                        break
                            if isinstance(content, str) and content.strip() and not content.startswith("<"):
                                preview = content.strip().replace("\n", " ")[:60]
                        if real_cwd and preview:
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
        ]:
            ToolbarButton(toolbar, text=text, command=cmd).pack(side="left", padx=1)

        ToolbarSep(toolbar).pack(side="left", padx=6, pady=5)

        ToolbarButton(toolbar, text="Folder", command=self.open_folder).pack(side="left", padx=1)
        ToolbarButton(toolbar, text="Scan", command=self.scan_sessions).pack(side="left", padx=1)
        ToolbarButton(toolbar, text="Backup", command=self.backup_now).pack(side="left", padx=1)
        ToolbarButton(toolbar, text="Restore", command=self.restore_backup).pack(side="left", padx=1)

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

        columns = ("name", "alias", "directory", "mode", "model", "session_id")
        self.tree = ttk.Treeview(tree_frame, columns=columns, show="headings",
                                  selectmode="browse")

        self.tree.heading("name", text="Name", anchor="w",
                          command=lambda: self._sort_by("name"))
        self.tree.heading("alias", text="Alias", anchor="w",
                          command=lambda: self._sort_by("alias"))
        self.tree.heading("directory", text="Working Directory", anchor="w",
                          command=lambda: self._sort_by("cwd"))
        self.tree.heading("mode", text="Mode", anchor="center")
        self.tree.heading("model", text="Model", anchor="w")
        self.tree.heading("session_id", text="Session ID", anchor="w")

        self.tree.column("name", width=220, minwidth=120)
        self.tree.column("alias", width=120, minwidth=70)
        self.tree.column("directory", width=300, minwidth=160)
        self.tree.column("mode", width=70, minwidth=60, anchor="center")
        self.tree.column("model", width=110, minwidth=80)
        self.tree.column("session_id", width=240, minwidth=120)

        scrollbar = ttk.Scrollbar(tree_frame, orient="vertical", command=self._on_scroll)
        self.tree.configure(yscrollcommand=self._sync_scroll)

        self.tree.pack(side="left", fill="both", expand=True)
        scrollbar.pack(side="right", fill="y")

        self.tree.bind("<Double-1>", lambda e: self.launch_session())
        self.tree.bind("<<TreeviewSelect>>", self._on_select)

        # Alternating row tags
        self.tree.tag_configure("odd", background=C["bg"])
        self.tree.tag_configure("even", background=C["bg_light"])

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
            # Compact label for the column
            model_short = model_label.replace("Default (Claude Code chooses)", "Default")

            if filter_text:
                if (filter_text not in name.lower() and
                    filter_text not in alias.lower() and
                    filter_text not in cwd.lower() and
                    filter_text not in sid.lower() and
                    filter_text not in model_short.lower()):
                    continue

            tag = "odd" if count % 2 == 0 else "even"
            self.tree.insert("", "end", values=(name, alias, cwd, mode, model_short, sid), tags=(tag,))
            count += 1

        self.status.set_segment("count", f"{count} sessions")
        matches = f" ({count} matches)" if filter_text else ""
        self.status.set_main(f"Ready{matches}")

    def _sort_by(self, key):
        if self.sort_col == key:
            self.sort_reverse = not self.sort_reverse
        else:
            self.sort_col = key
            self.sort_reverse = False
        self.sessions.sort(key=lambda s: s.get(key, "").lower(), reverse=self.sort_reverse)
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
        values = self.tree.item(sel[0], "values")
        name = values[0]
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

    def scan_sessions(self):
        self.status.flash("Scanning Claude storage...", C["accent_orange"])
        self.root.update()
        discovered = discover_sessions()
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
