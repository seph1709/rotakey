@echo off
title RotaKey Proxy v6 - Running
color 0A

:: Load .env if it exists
if exist .env (
    for /f "usebackq tokens=1,* delims==" %%A in (".env") do (
        if not "%%A"=="" if not "%%A:~0,1%"=="#" (
            set "%%A=%%B"
        )
    )
)

:: Pass --dry-run or --validate through
if "%1"=="--dry-run"  goto passthrough
if "%1"=="--validate" goto passthrough
if "%1"=="--check"    goto passthrough
goto start

:passthrough
python proxy.py %*
exit /b %errorlevel%

:start
set PORT=%ROTAKEY_PORT%
if "%PORT%"=="" set PORT=8765

echo.
echo  ==========================================
echo    RotaKey Proxy v6 - Starting...
echo  ==========================================
echo.
echo  Default port : %PORT%
echo  Health check : http://localhost:%PORT%/health
echo  Status page  : http://localhost:%PORT%/status
echo  Metrics      : http://localhost:%PORT%/metrics
echo.
if defined ROTAKEY_TOKEN (
    echo  Auth         : ENABLED
) else (
    echo  Auth         : DISABLED -- add ROTAKEY_TOKEN to .env to enable
)
echo.
echo  Client env settings:
echo    OPENAI_BASE_URL = http://localhost:%PORT%/openrouter
echo    OPENAI_API_KEY  = rotakey
echo.
echo  Press Ctrl+C to stop.
echo  ==========================================
echo.

python proxy.py

if errorlevel 1 (
    echo.
    echo  [ERROR] Proxy failed to start.
    echo  Run: python proxy.py --validate   to check your config.
    pause
)
