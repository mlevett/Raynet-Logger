# Copyright (C) 2026 Mathew Levett
# SPDX-License-Identifier: AGPL-3.0-or-later

from app import create_backup, init_db
init_db()
path = create_backup()
print(path)
