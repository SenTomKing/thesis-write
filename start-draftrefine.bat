@echo off
setlocal

set "PROJECT_DIR=F:\Fucking\thesis-refine-hub"

if not exist "%PROJECT_DIR%\package.json" (
  echo [DraftRefine] Project directory not found: %PROJECT_DIR%
  pause
  exit /b 1
)

echo [DraftRefine] Starting frontend and backend...

start "DraftRefine Frontend" cmd /k "cd /d %PROJECT_DIR% && npm run dev -- --host 127.0.0.1 --port 5173"
start "DraftRefine Backend" cmd /k "cd /d %PROJECT_DIR% && python -m backend.app"

echo.
echo Frontend: http://127.0.0.1:5173
echo Backend health: http://127.0.0.1:8000/api/health
echo.
echo If a window closes immediately, check whether npm and python are in PATH.
pause
