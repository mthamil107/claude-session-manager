# Claude Session Manager (CSM)

A desktop GUI for managing, launching, and **automatically backing up** your [Claude Code](https://claude.com/claude-code) sessions on Windows.

If you've ever lost a Claude Code conversation because the `.jsonl` file got cleaned up, this tool exists for you.

## Features

- **Browse all your Claude sessions** in a Notepad++-style dark UI
- **Launch any session in a new Windows Terminal tab** with the session name as the tab title
- **Automatic backups** of every conversation `.jsonl` file on app startup
- **Versioned snapshots** — keeps the last 10 backups per session, prunes older ones
- **One-click restore** of any historical backup
- **Scan & import** existing sessions from `~/.claude/projects/` (reads the real working directory from inside each `.jsonl`)
- **CLI launcher** included (`csm_cli.py`) for keyboard-driven workflows
- Sessions live in a simple JSON file — easy to edit, version, or sync separately

## Why this exists

Claude Code stores each conversation as a `.jsonl` file in `~/.claude/projects/<encoded-project>/<session-id>.jsonl`. These files can be deleted, lost, or rotated out — and there's no built-in backup. Lose the file, lose the conversation. CSM solves that.

## Requirements

- Windows 10 / 11
- Python 3.8+ (uses only standard library — `tkinter`, `json`, `subprocess`, `pathlib`)
- [Claude Code CLI](https://docs.claude.com/en/docs/claude-code) installed and on `PATH`
- (Optional) [Windows Terminal](https://aka.ms/terminal) for the best launch experience

## Install

```bash
git clone https://github.com/mthamil107/claude-session-manager.git
cd claude-session-manager
copy sessions.example.json sessions.json
```

Then either:
- Double-click `launch-csm.bat`, or
- Run `pythonw csm.pyw` from a terminal

### Optional: desktop shortcut

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
1. Click **Scan** in the toolbar — CSM finds every `.jsonl` in `~/.claude/projects/`
2. Pick which ones to import — names are auto-extracted from the first user message in each conversation
3. Imported sessions appear in the main list

Or click **New** and enter a session ID manually.

### Launch
- Select a row → **Launch** (or double-click) — opens a new tab in Windows Terminal with the session name as title
- **Launch All** — opens every saved session at once (useful for restoring a workspace)

### Backup
- **Auto-backup** runs on every CSM startup — silent, only copies files that have changed since the last backup (by size + modification time)
- **Backup** button — manual snapshot anytime
- Backups live in `./session_backups/<project>/<session-id>/<timestamp>.jsonl`
- Up to 10 versions kept per session — older snapshots auto-pruned

### Restore
- Select a session → **Restore**
- Pick which snapshot to restore from (newest first, with size + timestamp)
- The current `.jsonl` (if any) is preserved as `.pre-restore-<timestamp>` before being overwritten
- Companion folder is created automatically (Claude Code requires both `<id>.jsonl` and `<id>/` to exist)

### Keyboard shortcuts
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

### CLI quick launch

```bash
python csm_cli.py <alias>
```

For example, `python csm_cli.py api` resumes the session you aliased as `api`.

## File layout

```
claude-session-manager/
├── csm.pyw              # main GUI app (run with pythonw)
├── csm_cli.py           # command-line launcher
├── launch-csm.bat       # Windows double-click launcher
├── csm-cli.bat          # CLI launcher
├── sessions.example.json
├── sessions.json        # YOUR sessions (gitignored)
├── session_backups/     # YOUR backups (gitignored)
└── ...
```

`sessions.json` and `session_backups/` are gitignored — your data stays local.

## How backups work

On every startup CSM walks `~/.claude/projects/*/` and looks at every `.jsonl` file. For each one it computes a fingerprint (`size:mtime`) and compares it against the last fingerprint stored in `session_backups/index.json`. If the file has changed, it's copied to `session_backups/<project>/<session-id>/<YYYY-MM-DD_HHMMSS>.jsonl`. Only changed files get copied, so backups stay fast and storage stays small.

When you have more than 10 snapshots for a single session, the oldest is deleted.

If `session_backups/` lives on a different drive from `~/.claude/`, you also get cross-drive redundancy.

## Contributing

PRs welcome. Particularly useful additions:
- macOS / Linux launch support (currently uses Windows Terminal + PowerShell)
- Cloud backup destinations (S3, Drive)
- Session search across all conversation contents

## License

MIT — see [LICENSE](LICENSE).
