@echo off
echo Registering AlpacaDailySummary scheduled task...
echo.

schtasks /delete /tn "AlpacaDailySummary" /f >nul 2>&1

schtasks /create ^
  /tn "AlpacaDailySummary" ^
  /tr "C:\Users\Nathaniel\Documents\Trading\run_alpaca_summary.bat" ^
  /sc daily ^
  /st 10:00 ^
  /f

if %errorlevel%==0 (
    echo.
    echo SUCCESS: Task registered. Runs daily at 10:00 AM.
) else (
    echo.
    echo FAILED. Error code: %errorlevel%
)

echo.
pause
