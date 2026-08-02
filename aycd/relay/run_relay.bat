@echo off
where py >nul 2>nul
if %errorlevel%==0 (
    py cyberyozh_purchase_relay.py
) else (
    python cyberyozh_purchase_relay.py
)
pause
