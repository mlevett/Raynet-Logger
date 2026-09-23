#!/usr/bin/env sh
# Copyright (C) 2026 Mathew Levett
# SPDX-License-Identifier: AGPL-3.0-or-later
set -eu
cd "$(dirname "$0")"
if [ ! -d .venv ]; then python3 -m venv .venv; fi
. .venv/bin/activate
python -m pip install -r requirements.txt
python -m uvicorn app:app --host 0.0.0.0 --port 8080
