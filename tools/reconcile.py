#!/usr/bin/env python3
"""Reconcile the original Evernote exports against what is now in OneNote.

Reads the ENEX files directly rather than trusting the staging tree, so the
comparison starts from the same source of truth the migration did.

    python3 tools/reconcile.py            # counts and titles
    python3 tools/reconcile.py --deep     # also check images/attachments per page
"""
import argparse
import json
import re
import sys
import unicodedata
from collections import defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import msauth  # noqa: E402
from enex2staging import enex_files, iter_notes, section_name  # noqa: E402
from onenote_push import find_notebook, request  # noqa: E402

import config  # noqa: E402

ROOT = config.HOME
STAGING = config.STAGING
LEDGER = config.LEDGER


def normalize(title):
    """Compare titles ignoring entity encoding, unicode form and whitespace."""
    t = unicodedata.normalize("NFC", title or "")
    t = re.sub(r"&#(\d+);", lambda m: chr(int(m.group(1))), t)
    t = (t.replace("&amp;", "&").replace("&lt;", "<")
         .replace("&gt;", ">").replace("&quot;", '"'))
    return re.sub(r"\s+", " ", t).strip().casefold()


def evernote_titles():
    """{section: [titles]} straight from the ENEX exports."""
    out = defaultdict(list)
    for stem, path in enex_files().items():
        section = section_name(stem)
        for note in iter_notes(path):
            out[section].append((note.findtext("title") or "").strip() or "Untitled")
    return out


def live_pages(tok, notebook_id):
    """{section: {page_id: title}} as OneNote currently reports it."""
    _, data = request("GET", f"/me/onenote/notebooks/{notebook_id}/sections", tok)
    out = {}
    for s in data.get("value", []):
        pages = {}
        url = f"/me/onenote/sections/{s['id']}/pages?$top=100&$select=id,title"
        while url:
            _, page = request("GET", url, tok)
            for p in page.get("value", []):
                pages[p["id"]] = p.get("title") or ""
            url = page.get("@odata.nextLink")
        out[s["displayName"]] = pages
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--deep", action="store_true",
                    help="fetch each page's HTML and check attachment counts")
    args = ap.parse_args()

    tok = msauth.token()
    notebook = find_notebook(tok)
    print(f"notebook: {notebook['displayName']}\n")

    source = evernote_titles()
    live = live_pages(tok, notebook["id"])
    ledger = {}
    for line in LEDGER.read_text().splitlines():
        if line.strip():
            rec = json.loads(line)
            if rec.get("status") == "ok" and rec.get("page_id"):
                ledger[rec["page_id"]] = rec

    print(f"{'section':30} {'evernote':>9} {'onenote':>8} {'matched':>8} {'missing':>8} {'extra':>6}")
    print("-" * 74)
    grand = defaultdict(int)
    problems = []

    for section in sorted(source):
        want = source[section]
        pages = live.get(section, {})

        # Match on title, allowing genuine duplicates in the source.
        pool = defaultdict(int)
        for pid in pages:
            title = ledger[pid]["title"] if pid in ledger else pages[pid]
            pool[normalize(title)] += 1

        missing = []
        for title in want:
            key = normalize(title)
            if pool.get(key, 0) > 0:
                pool[key] -= 1
            else:
                missing.append(title)
        extra = sum(v for v in pool.values() if v > 0)
        matched = len(want) - len(missing)

        grand["evernote"] += len(want)
        grand["onenote"] += len(pages)
        grand["matched"] += matched
        grand["missing"] += len(missing)
        grand["extra"] += extra

        flag = "" if not missing and not extra else "  <--"
        print(f"{section:30} {len(want):9} {len(pages):8} {matched:8} "
              f"{len(missing):8} {extra:6}{flag}")
        for t in missing:
            problems.append(f"MISSING  {section} / {t[:70]}")

    print("-" * 74)
    print(f"{'TOTAL':30} {grand['evernote']:9} {grand['onenote']:8} "
          f"{grand['matched']:8} {grand['missing']:8} {grand['extra']:6}")

    if args.deep:
        print("\nchecking attachments on every page...")
        bad = 0
        for m in sorted(STAGING.rglob("meta.json")):
            meta = json.loads(m.read_text())
            rec = next((r for r in ledger.values() if r["note_id"] == meta["note_id"]), None)
            if not rec:
                continue
            _, _ = request("GET", f"/me/onenote/pages/{rec['page_id']}", tok)
            html = fetch_html(tok, rec["page_id"])
            want_img = sum(1 for p in meta["parts"] if p["kind"] == "img")
            want_obj = sum(1 for p in meta["parts"] if p["kind"] == "object")
            got_img, got_obj = html.count("<img"), html.count("<object")
            if got_img < want_img or got_obj < want_obj:
                bad += 1
                problems.append(
                    f"ATTACH   {meta['section']} / {meta['title'][:50]}: "
                    f"images {got_img}/{want_img}, files {got_obj}/{want_obj}")
        print(f"pages with missing attachments: {bad}")

    if problems:
        out = ROOT / "reconcile_problems.txt"
        out.write_text("\n".join(problems), encoding="utf-8")
        print(f"\n{len(problems)} problems written to {out.name}:")
        for p in problems[:15]:
            print("  " + p)
    else:
        print("\nEverything in the Evernote exports is present in OneNote.")


def fetch_html(tok, page_id):
    import urllib.request
    req = urllib.request.Request(
        f"https://graph.microsoft.com/v1.0/me/onenote/pages/{page_id}/content",
        headers={"Authorization": f"Bearer {tok}", "Accept": "text/html"})
    with urllib.request.urlopen(req, timeout=300) as r:
        return r.read().decode("utf-8", "replace")


main()
