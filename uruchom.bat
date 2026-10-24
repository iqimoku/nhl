@echo off
REM Raport NHL na dzisiejsze mecze. Dwuklik albo Harmonogram zadan Windows.
cd /d "%~dp0"
python nhl_model.py %*
if errorlevel 1 pause
