@echo off
title RotaKey v6 - Installer
color 0A

echo.
echo  ==========================================
echo    RotaKey Proxy v6 - Windows Installer
echo  ==========================================
echo.

:: ── Python check ─────────────────────────────────────────────
python --version >nul 2>&1
if errorlevel 1 (
    echo  [ERROR] Python not found. Install from https://python.org
    echo          Make sure to check "Add Python to PATH" during install.
    pause
    exit /b 1
)

for /f "tokens=2" %%i in ('python --version 2^>^&1') do set PYVER=%%i
echo  [OK] Python %PYVER% found

:: ── Install pinned deps ───────────────────────────────────────
if not exist requirements.txt (
    echo  [ERROR] requirements.txt not found next to install.bat
    pause
    exit /b 1
)

echo.
echo  Installing pinned dependencies from requirements.txt...
echo.

pip install -r requirements.txt --quiet

if errorlevel 1 (
    echo.
    echo  [ERROR] pip install failed. Try running as Administrator.
    pause
    exit /b 1
)

echo  [OK] Dependencies installed (pinned versions)
echo.

:: ── .env setup ───────────────────────────────────────────────
if not exist .env (
    if exist .env.example (
        copy .env.example .env >nul
        echo  [OK] Created .env from .env.example -- edit it to add your API keys
    )
)

:: ── Validate config ──────────────────────────────────────────
echo  Validating configuration...
python proxy.py --validate
if errorlevel 1 (
    echo  [WARN] Config validation had warnings -- check rotakey.yaml or .env
) else (
    echo  [OK] Config valid
)

echo.
echo  ==========================================
echo    SECURITY NOTICE
echo  ==========================================
echo.
echo  rotakey.yaml stores settings as plain text.
echo  API keys should go in .env (auto-created above).
echo  Keep this folder private:
echo    - Do NOT put it in Dropbox / OneDrive / git
echo    - Do NOT share the folder with other users
echo    - The proxy only listens on localhost (127.0.0.1)
echo.
echo  Set ROTAKEY_TOKEN to require clients to authenticate:
echo    Add line: ROTAKEY_TOKEN=your-secret-here   to .env
echo.
echo  ==========================================
echo    Installation complete!
echo  ==========================================
echo.
echo  Next steps:
echo    1. Edit .env -- add your API keys:
echo         ROTAKEY_KEYS_OPENROUTER=sk-or-v1-...
echo    2. Test connectivity: python proxy.py --dry-run
echo    3. Start the proxy:   start.bat
echo.
echo  Or with Docker:
echo    docker compose up -d
echo.
echo  Client env settings:
echo    OPENAI_BASE_URL = http://localhost:8765/openrouter
echo    OPENAI_API_KEY  = rotakey   (or your ROTAKEY_TOKEN)
echo.
pause
