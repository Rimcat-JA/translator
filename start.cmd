@echo off
setlocal
cd /d "%~dp0"
where uv >nul 2>nul
if errorlevel 1 (
  echo uv is required for source builds. Use the Windows distribution or install uv and Node.js 24.
  pause
  exit /b 1
)
uv run --locked --no-dev translator start %*
if errorlevel 1 pause
