@echo off
where py >nul 2>nul
if %errorlevel%==0 (
    py -m pip install requests
) else (
    python -m pip install requests
)
pause
