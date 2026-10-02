@echo off
setlocal
cd /d "%~dp0"
where py >nul 2>nul
if errorlevel 1 goto python_fallback
py -3 -c "import sys, tkinter; assert sys.version_info >= (3, 10)" >nul 2>nul
if errorlevel 1 goto python_fallback
where pyw >nul 2>nul
if errorlevel 1 goto console_py
start "" pyw -3 DngConverterEx.pyw %*
exit /b
:console_py
py -3 app.py %*
if errorlevel 1 pause
exit /b
:python_fallback
where python >nul 2>nul
if errorlevel 1 goto no_python
python -c "import sys, tkinter; assert sys.version_info >= (3, 10)" >nul 2>nul
if errorlevel 1 goto no_python
where pythonw >nul 2>nul
if errorlevel 1 goto console_python
start "" pythonw DngConverterEx.pyw %*
exit /b
:console_python
python app.py %*
if errorlevel 1 pause
exit /b
:no_python
echo Python 3.10+ with Tkinter is required.
echo Install the official Python Windows installer, then run this file again.
echo https://www.python.org/downloads/windows/
pause
exit /b 2
