"""Shared paths and settings.

Everything the migration reads or writes lives under one working directory,
which defaults to wherever you run the commands from. Set MIGRATION_HOME to
keep the data somewhere other than the current directory.
"""
import os
from pathlib import Path

HOME = Path(os.environ.get("MIGRATION_HOME", Path.cwd())).resolve()

ENEX_DIR = HOME / "enex"            # .enex files from the export step
STAGING = HOME / "staging"          # converted, upload-ready notes
OVERSIZED = HOME / "oversized"      # attachments too large for the OneNote API
LEDGER = HOME / "ledger.jsonl"      # what has been uploaded already
BACKUP_DB = HOME / "en_backup.db"   # evernote-backup's local database

# Name of the OneNote notebook to import into. Override per-run with
# --notebook, or set ONENOTE_NOTEBOOK once.
NOTEBOOK = os.environ.get("ONENOTE_NOTEBOOK", "Evernote Import")

# Azure app registration (see docs/azure-app-setup.md). Either set
# ONENOTE_CLIENT_ID or drop the id into a .client_id file.
CLIENT_ID_FILE = HOME / ".client_id"
TOKEN_CACHE = HOME / ".msal_token.json"

# Optional: rename notebooks, or merge several into one OneNote section.
# Put a JSON object in section_overrides.json next to your enex/ directory:
#
#     {
#       "Book_ The AI Advantage": "Book The AI Advantage",
#       "Inbox_part1": "Inbox",
#       "Inbox_part2": "Inbox"
#     }
#
# The key is the .enex filename without its extension; the value is the
# OneNote section name to use.
SECTION_OVERRIDES_FILE = HOME / "section_overrides.json"


def section_overrides():
    if not SECTION_OVERRIDES_FILE.exists():
        return {}
    import json
    return json.loads(SECTION_OVERRIDES_FILE.read_text(encoding="utf-8"))
