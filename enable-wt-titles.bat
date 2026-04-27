@echo off
REM One-time helper: makes Windows Terminal honor CSM tab titles instead of
REM letting Claude Code overwrite them with "Claude Code".
python "%~dp0enable_wt_titles.py"
pause
