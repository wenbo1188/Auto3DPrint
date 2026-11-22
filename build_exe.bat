@echo off
REM Build Auto3DPrint.exe into the dist folder. Needs Python 3.10+ on Windows.
python -m pip install -r requirements.txt pyinstaller || exit /b 1
python -m PyInstaller --noconfirm --onefile --windowed --name Auto3DPrint run_gui.py || exit /b 1
echo.
echo Done: dist\Auto3DPrint.exe
