@echo off
REM Quick CLI launcher: csm <alias>
REM Example: csm domain, csm patrol, csm stock

if "%1"=="" (
    echo Usage: csm ^<alias^>
    echo.
    echo Launches Claude Session Manager GUI if no argument given.
    start "" pythonw "%~dp0csm.pyw"
    goto :eof
)

python "%~dp0csm_cli.py" %*
