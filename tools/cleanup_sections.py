#!/usr/bin/env python3
"""Delete OneNote sections that no longer correspond to any exported notebook.

Only ever touches empty sections, so a section holding pages is reported and
left alone rather than removed.

    python3 tools/cleanup_sections.py [--dry-run]
"""
import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import msauth  # noqa: E402
from enex2staging import enex_files, section_name  # noqa: E402
from onenote_push import GraphError, find_notebook, request  # noqa: E402


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    tok = msauth.token()
    notebook = find_notebook(tok)
    wanted = {section_name(stem) for stem in enex_files()}

    _, data = request("GET", f"/me/onenote/notebooks/{notebook['id']}/sections", tok)
    sections = data.get("value", [])
    stale = [s for s in sections if s["displayName"] not in wanted]

    print(f"sections in notebook: {len(sections)}")
    print(f"sections the export needs: {len(wanted)}")
    if not stale:
        print("nothing stale")
        return

    for s in stale:
        try:
            _, pages = request(
                "GET", f"/me/onenote/sections/{s['id']}/pages?$top=5&$select=id", tok)
        except GraphError as e:
            print(f"  {s['displayName']!r}: could not list pages ({e.status}), skipping")
            continue
        n = len(pages.get("value", []))
        if n:
            print(f"  {s['displayName']!r}: holds {n} page(s), leaving alone")
            continue
        if args.dry_run:
            print(f"  {s['displayName']!r}: would delete")
            continue
        try:
            # One attempt only. OneNote's consumer API answers section deletes
            # with a permanent 503, and retrying that just burns the request
            # budget and triggers throttling for everything that follows.
            request("DELETE", f"/me/onenote/sections/{s['id']}", tok, retries=1)
            print(f"  deleted empty section {s['displayName']!r}")
        except GraphError as e:
            print(f"  {s['displayName']!r}: delete refused ({e.status}); "
                  f"remove it by hand in OneNote")


if __name__ == "__main__":
    main()
