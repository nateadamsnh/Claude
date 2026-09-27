@echo off
setlocal

set SCRIPT_DIR=%~dp0
set TARGET_DIR=C:\Users\Nathaniel\Documents\Trading
set CONFIG_DIR=%TARGET_DIR%\config
set TASK_NAME=AlpacaDailySummary

echo === Alpaca Daily Summary Setup ===
echo.

:: Create directories
echo Creating directories...
if not exist "%TARGET_DIR%" mkdir "%TARGET_DIR%"
if not exist "%CONFIG_DIR%" mkdir "%CONFIG_DIR%"

:: Copy files
echo Copying script and credentials...
copy /Y "%SCRIPT_DIR%alpaca_daily_summary.py" "%TARGET_DIR%\alpaca_daily_summary.py"
copy /Y "%SCRIPT_DIR%email_credentials.json"  "%CONFIG_DIR%\email_credentials.json"

:: Find Python
echo.
echo Finding Python...
where python >nul 2>&1
if %errorlevel% == 0 (
    set PYTHON=python
) else (
    if exist "C:\Users\Nathaniel\AppData\Local\Programs\Python\Python310\python.exe" (
        set PYTHON=C:\Users\Nathaniel\AppData\Local\Programs\Python\Python310\python.exe
    ) else (
        echo ERROR: Python not found. Install Python and try again.
        pause
        exit /b 1
    )
)
echo Using: %PYTHON%

:: Install requests
echo.
echo Installing requests...
%PYTHON% -m pip install requests --quiet

:: Register scheduled task (daily 10am)
echo.
echo Registering Windows Scheduled Task "%TASK_NAME%"...
schtasks /delete /tn "%TASK_NAME%" /f >nul 2>&1
schtasks /create /tn "%TASK_NAME%" ^
  /tr "\"%PYTHON%\" \"%TARGET_DIR%\alpaca_daily_summary.py\"" ^
  /sc daily /st 10:00 ^
  /rl highest /f
if %errorlevel% == 0 (
    echo Task registered successfully.
) else (
    echo WARNING: Task registration failed. You can register it manually.
)

:: Run the script now
echo.
echo =============================================
echo Running alpaca_daily_summary.py now...
echo =============================================
echo.
%PYTHON% "%TARGET_DIR%\alpaca_daily_summary.py"

echo.
if %errorlevel% == 0 (
    echo SUCCESS - Email sent!
) else (
    echo ERROR - Script exited with error. Check output above.
)

echo.
pause
