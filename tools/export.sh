#!/usr/bin/env bash
#
# Export an Evernote account to one .enex file per notebook.
#
# Wraps evernote-backup (https://github.com/vzhd1701/evernote-backup), which
# is the only maintained way to script this: Evernote's own API is deprecated
# and version 10 of the app dropped the ENScript command-line tool.
#
#   ./tools/export.sh            # sync, then export into ./enex
#   ./tools/export.sh --resync   # pull fresh changes before exporting
#
set -euo pipefail

HOME_DIR="${MIGRATION_HOME:-$PWD}"
DB="$HOME_DIR/en_backup.db"
OUT="$HOME_DIR/enex"

if ! python3 -c "import evernote_backup" 2>/dev/null; then
    echo "evernote-backup is not installed. Run:"
    echo "    pip3 install --user evernote-backup"
    exit 1
fi

if [ ! -f "$DB" ]; then
    echo "==> Logging in to Evernote"
    echo "    A browser window will open; sign in there. Your password is"
    echo "    never seen by this script."
    echo
    # Must run attached to a terminal: the OAuth step prompts for input.
    python3 -m evernote_backup init-db --database "$DB"
elif [ "${1:-}" = "--resync" ]; then
    echo "==> Refreshing credentials"
    python3 -m evernote_backup reauth --database "$DB" || true
fi

echo "==> Syncing account to $DB"
python3 -m evernote_backup sync --database "$DB"

echo "==> Exporting notebooks to $OUT"
mkdir -p "$OUT"
# --add-guid writes Evernote's own note id into each note. The importer uses
# it to recognise a note it has already uploaded, so re-exporting later never
# creates duplicates. --no-export-date keeps the files byte-stable between
# runs when nothing has changed.
python3 -m evernote_backup export \
    --database "$DB" \
    --add-guid \
    --no-export-date \
    --overwrite \
    "$OUT"

echo
echo "Exported $(ls -1 "$OUT"/*.enex 2>/dev/null | wc -l | tr -d ' ') notebook(s):"
ls -1 "$OUT"
echo
echo "Notes in trash are not exported. Add --include-trash above if you want them."
