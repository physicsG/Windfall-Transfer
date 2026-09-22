@echo off
rem Starts the Mac <-> Windows USB-C network bridge (asks for administrator rights).
cd /d "%~dp0"
python bridge.py %*
