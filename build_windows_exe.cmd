@echo off
setlocal
cd /d "%~dp0"
echo Building a desktop GUI executable on Windows...
py -3 -m pip install pyinstaller
if errorlevel 1 goto failed
py -3 -m PyInstaller --noconfirm --clean --onedir --windowed --name DngConverterEx DngConverterEx.pyw
if errorlevel 1 goto failed
echo Done: dist\DngConverterEx\DngConverterEx.exe
echo Adobe DNG Converter must be installed separately on the destination PC.
pause
exit /b 0
:failed
echo Build failed.
pause
exit /b 1
