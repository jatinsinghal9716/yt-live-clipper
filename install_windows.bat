@echo off
setlocal
cd /d "%~dp0"
echo === YT Live Clipper - Windows installer (everything is free) ===
echo.

where python >nul 2>nul
if errorlevel 1 (
  echo [1/4] Installing Python...
  winget install -e --id Python.Python.3.12 --accept-package-agreements --accept-source-agreements
  echo.
  echo Python installed. CLOSE this window and double-click install_windows.bat again.
  pause
  exit /b
) else (
  echo [1/4] Python found.
)

where ffmpeg >nul 2>nul
if errorlevel 1 (
  echo [2/4] Installing FFmpeg...
  winget install -e --id Gyan.FFmpeg --accept-package-agreements --accept-source-agreements
  echo.
  echo FFmpeg installed. CLOSE this window and double-click install_windows.bat again.
  pause
  exit /b
) else (
  echo [2/4] FFmpeg found.
)

echo [3/4] Installing the clipper and its free tools (yt-dlp, streamlink, faster-whisper)...
if not exist .venv python -m venv .venv
call .venv\Scripts\activate.bat
python -m pip install -q --upgrade pip
pip install -q -e .
if errorlevel 1 (
  echo Install failed - see the messages above.
  pause
  exit /b 1
)

echo [4/4] Checking everything...
ytclip doctor
echo.
echo Done! Double-click  START_CLIPPING.bat  to clip a live stream.
echo (Optional test without YouTube:  .venv\Scripts\ytclip demo )
pause
