@echo off
REM Builds a single-file, no-console yt-dlp-gui.exe (needs Python 3.9+ from python.org)
py -m pip install --upgrade pyinstaller
py -m PyInstaller --noconsole --onefile --name yt-dlp-gui yt_dlp_gui.pyw
echo.
echo Done. Your app is in dist\yt-dlp-gui.exe
pause
