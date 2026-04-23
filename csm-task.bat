@echo off
REM csm-task: delegate a task to another registered session via `claude -p`
REM Usage:  csm-task <alias> "<prompt>" [--with-context N] [--from <alias>] [--tools "..."] [--continue]
python "%~dp0csm_task.py" %*
