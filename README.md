# Claude Session Manager (CSM)

[![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)](LICENSE)
[![Python 3.8+](https://img.shields.io/badge/python-3.8+-blue.svg)](https://www.python.org/downloads/)
[![Platform: Windows](https://img.shields.io/badge/platform-Windows-lightgrey.svg)](#requirements)
[![Status: stable](https://img.shields.io/badge/status-stable-brightgreen.svg)](#)

> A desktop GUI for managing, launching, and **automatically backing up** your [Claude Code](https://claude.com/claude-code) sessions.
> If you've ever lost a Claude Code conversation because the `.jsonl` file got cleaned up — this tool exists for you.

<p align="center"><i>Pure Python, no dependencies, ~270 MB of conversations safely backed up on first launch.</i></p>

---

## Table of contents

- [Features](#features)
- [Why this exists](#why-this-exists)
- [Quick start](#quick-start)
- [Requirements](#requirements)
- [Install](#install)
- [Usage](#usage)
- [Keyboard shortcuts](#keyboard-shortcuts)
- [How backups work](#how-backups-work)
- [Recovering a deleted session](#recovering-a-deleted-session)
- [Troubleshooting](#troubleshooting)
- [File layout](#file-layout)
- [Contributing](#contributing)
- [License](#license)

---

## Features

- **Browse all your Claude sessions** in a Notepad++-style dark UI
- **Launch sessions in Windows Terminal tabs** with the session name as the tab title
- **Automatic backups** of every conversation `.jsonl` file on app startup
- **Versioned snapshots** — keeps the last 10 backups per session, auto-prunes older ones
- **One-click restore** of any historical backup, with a `.pre-restore-*` safety copy
- **Scan & import** existing sessions — reads the real working directory from inside each `.jsonl`
- **CLI launcher** (`csm_cli.py`) for keyboard-driven workflows
- **Zero dependencies** — pure Python standard library (`tkinter`, `json`, `subprocess`, `pathlib`)
- **Single-file storage** — `sessions.json` is plain JSON, easy to edit, version, or sync

## Why this exists

Claude Code stores each conversation as a `.jsonl` file in `~/.claude/projects/<encoded-project>/<session-id>.jsonl`. Those files can be deleted, lost, or rotated out of existence — and **there is no built-in backup**. Lose the file, lose the conversation. CSM solves that, while also giving you a fast way to launch any session by name.

## Quick start

```bash
git clone https://github.com/mthamil107/claude-session-manager.git
cd claude-session-manager
copy sessions.example.json sessions.json    # Windows
# cp sessions.example.json sessions.json    # macOS/Linux
pythonw csm.pyw
```

Click **Scan** in the toolbar to discover and import your existing Claude sessions, then **Launch** any of them.

## Requirements

- **Windows 10 / 11** (launch path uses Windows Terminal + PowerShell — see [Contributing](#contributing) for cross-platform help)
- **Python 3.8+** with Tk (default on Windows installs from python.org)
- **[Claude Code CLI](https://docs.claude.com/en/docs/claude-code)** installed and on `PATH`
- *(Optional)* **[Windows Terminal](https://aka.ms/terminal)** — for tabbed sessions with proper titles

## Install

```bash
git clone https://github.com/mthamil107/claude-session-manager.git
cd claude-session-manager
copy sessions.example.json sessions.json
```

Then either:

- Double-click `launch-csm.bat`
- Run `pythonw csm.pyw` from any terminal
- *(Optional)* create a desktop shortcut:

```powershell
$WshShell = New-Object -ComObject WScript.Shell
$Shortcut = $WshShell.CreateShortcut("$Home\Desktop\Claude Session Manager.lnk")
$Shortcut.TargetPath = 'pythonw'
$Shortcut.Arguments = '"C:\path\to\claude-session-manager\csm.pyw"'
$Shortcut.WorkingDirectory = 'C:\path\to\claude-session-manager'
$Shortcut.Save()
```

## Usage

### Add sessions

1. Click **Scan** — CSM finds every `.jsonl` in `~/.claude/projects/`
2. Pick which to import — names auto-extracted from the first user message in each conversation
3. Imported sessions appear in the main list

Or click **New** to enter a session ID manually.

### Launch

- Select a row → **Launch** (or double-click) — opens a new tab in Windows Terminal with the session name as the title
- **Launch All** — opens every saved session at once (great for restoring an end-of-day workspace)

### Backup

- **Auto-backup** runs silently on every CSM startup — only copies files that changed since the last backup (size + mtime fingerprint), so it's fast
- **Backup** button — manual snapshot anytime
- Backups live in `./session_backups/<project>/<session-id>/<YYYY-MM-DD_HHMMSS>.jsonl`
- Up to **10 snapshots per session** are kept; older ones are auto-pruned

### Restore

- Select a session → **Restore**
- Pick a snapshot from the list (newest first, with size + timestamp)
- The current `.jsonl` (if any) is preserved as `.pre-restore-<timestamp>` before being overwritten
- The companion folder Claude Code expects (`<id>/` next to `<id>.jsonl`) is created automatically

### CLI quick launch

```bash
python csm_cli.py <alias>
python csm_cli.py list          # list all aliases
python csm_cli.py api           # resume the session aliased "api"
```

### Cross-session task delegation

Use **Run Task** to fire off a one-shot job in another project — without leaving your current session. Two ways to call it:

**From CSM (UI):** Select the *target* session → toolbar **Run Task** button → type the prompt → Run. A new Windows Terminal tab opens, runs `claude -p "<prompt>" -C <target-cwd>`, and exits when done.

**From inside another Claude session (CLI):** the `csm-task` helper is on `PATH` after install. Your active Claude can call it via its Bash tool:

```bash
csm-task <target-alias> "<prompt>" [--with-context N] [--from <source-alias>]
                                   [--tools "Read,Edit,Bash"] [--continue]
                                   [--print]
```

Examples:

```bash
# Fire-and-forget: add an endpoint in the API project
csm-task api "Add a /healthz endpoint that returns 200"

# Bring 20 turns of context from the current shell's most-recent .jsonl
csm-task api "Apply the same auth refactor we did here" --with-context 20

# Pull context from a specific session, append to target's existing conversation
csm-task api "Continue the migration we discussed" \
    --with-context 30 --from frontend --continue

# Just print the command without launching
csm-task api "test prompt" --print
```

Built on `claude -p` (non-interactive mode) — see [Claude Code CLI docs](https://docs.claude.com/en/docs/claude-code) for available flags.

## Keyboard shortcuts

| Shortcut | Action |
|----------|--------|
| `Enter` | Launch selected session |
| `Ctrl+L` | Launch all sessions |
| `Ctrl+N` | New session |
| `F2` | Edit selected session |
| `Del` | Remove session |
| `Ctrl+F` | Focus search |
| `Esc` | Clear search |
| `Ctrl+O` | Open session's working folder |
| `F5` | Reload sessions list |
| `1` / `2` | Switch tabs |

## How backups work

On every startup CSM walks `~/.claude/projects/*/`, inspecting every `.jsonl` file:

1. Compute a fingerprint: `<size>:<mtime>`
2. Compare it against the last fingerprint in `session_backups/index.json`
3. If unchanged → skip
4. If new or changed → copy to `session_backups/<project>/<session-id>/<YYYY-MM-DD_HHMMSS>.jsonl`
5. If more than 10 snapshots exist for a session → delete the oldest

Only changed files are copied, so backups stay quick and disk usage stays small. If `session_backups/` lives on a different drive from `~/.claude/` you also get cross-drive redundancy for free.

## Recovering a deleted session

If a Claude `.jsonl` was deleted and you have a CSM backup:

1. Open CSM, select the session in the list
2. Click **Restore** — pick the snapshot you want
3. CSM copies the `.jsonl` back to `~/.claude/projects/<project>/<session-id>.jsonl` and creates the companion `<session-id>/` folder
4. From a terminal in the original working directory:
   ```bash
   claude --resume <session-id>
   ```

> **Heads up:** Claude Code requires *both* `<id>.jsonl` and a folder named `<id>/` to exist in the same project directory. CSM creates the folder for you on restore — manual restores need to do this themselves.

If you have **no CSM backup yet** and the file was just deleted, your last hopes are:

1. Windows **Recycle Bin**
2. **Volume Shadow Copies** (`vssadmin list shadows /for=C:`)
3. **OneDrive version history** if `~/.claude` happens to be synced
4. File-recovery tools like Recuva — only useful if disk sectors haven't been overwritten

## Troubleshooting

**Launching a session does nothing / a black window flashes and disappears.**
The cwd in `sessions.json` probably doesn't exist anymore. Edit the session (`F2`) and fix the **Working Directory** field, or click **Scan** to re-import with the correct path read from the `.jsonl`.

**`claude --resume <id>` says "No conversation found" even after restore.**
Claude Code requires the companion folder. Make sure both of these exist:
- `~/.claude/projects/<project>/<session-id>.jsonl`
- `~/.claude/projects/<project>/<session-id>/`

CSM does this automatically on **Restore**. If you copied a file in by hand, create the folder yourself.

**Tab title shows "Claude Code" instead of the session name.**
In Windows Terminal: Settings → your PowerShell profile → Advanced → uncheck **Suppress title changes from the application**.

**`pythonw` not found.**
Use `python` instead, or install Python from [python.org](https://www.python.org/downloads/) (the Microsoft Store build sometimes lacks `pythonw`).

**`tkinter` not installed.**
Tk ships with the Windows Python installer by default. On Linux: `sudo apt install python3-tk`. On macOS: it's bundled with the python.org build.

## File layout

```
claude-session-manager/
├── csm.pyw                  # main GUI app (run with pythonw)
├── csm_cli.py               # command-line launcher
├── launch-csm.bat           # Windows double-click launcher
├── csm-cli.bat              # CLI launcher
├── sessions.example.json    # template
├── sessions.json            # YOUR sessions  (gitignored)
├── session_backups/         # YOUR backups   (gitignored)
├── README.md
├── LICENSE
└── .gitignore
```

`sessions.json` and `session_backups/` are gitignored — your data stays local.

## Contributing

PRs welcome. Particularly valuable:

- **macOS / Linux launch support** — currently launches via `wt.exe` + PowerShell
- **Cloud backup destinations** — S3, Google Drive, Dropbox
- **Full-text search** across all conversation contents
- **Diff viewer** between backup snapshots
- **Theme support** — currently dark only

Open an issue first if you want to discuss an approach.

## License

MIT — see [LICENSE](LICENSE).
