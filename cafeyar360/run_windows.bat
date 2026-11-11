@echo off
cd /d "%~dp0"
if not exist .venv (py -3 -m venv .venv)
call .venv\Scripts\activate.bat
python -m pip install -r requirements-lock.txt
if not defined DEMO_MODE set DEMO_MODE=1
python -m uvicorn app:app --host 0.0.0.0 --port 8000
