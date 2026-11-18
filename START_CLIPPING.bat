@echo off
cd /d "%~dp0"
if not exist .venv\Scripts\ytclip.exe (
  echo Run install_windows.bat first.
  pause
  exit /b 1
)
call .venv\Scripts\activate.bat
echo === YT Live Clipper ===
echo The clipper page opens in your browser. Keep this window open while clipping.
echo Close this window to quit.
ytclip web
pause
