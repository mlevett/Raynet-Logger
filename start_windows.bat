@echo off
REM Copyright (C) 2026 Mathew Levett
REM SPDX-License-Identifier: AGPL-3.0-or-later
setlocal
cd /d "%~dp0"
if not exist .venv (
  py -3 -m venv .venv
)
call .venv\Scripts\activate.bat
python -m pip install -r requirements.txt
start "" http://127.0.0.1:8080
python -m uvicorn app:app --host 0.0.0.0 --port 8080
