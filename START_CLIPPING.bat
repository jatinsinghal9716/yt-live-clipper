@echo off
cd /d "%~dp0"
if not exist .venv\Scripts\ytclip.exe (
  echo Run install_windows.bat first.
  pause
  exit /b 1
)
call .venv\Scripts\activate.bat
echo === YT Live Clipper ===
echo Paste the YouTube live link (e.g. https://www.youtube.com/@ChannelName/live)
set /p URL=Link:
echo.
echo Clipping... leave this window open. Press Ctrl+C to stop.
echo Clips appear in the clips_output folder (open clips\index.html to watch them).
echo.
ytclip live "%URL%" --wait
start "" "%~dp0clips_output"
pause
