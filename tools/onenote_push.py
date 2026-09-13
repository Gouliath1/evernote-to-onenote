#!/usr/bin/env python3
"""Upload the staged notes into OneNote through the Microsoft Graph API.

Every uploaded page is recorded in ledger.jsonl, so a re-run only pushes what
is still missing and an interrupted run can simply be restarted.

    python3 tools/onenote_push.py --section Blockchain --limit 1   # trial
    python3 tools/onenote_push.py --all
    python3 tools/onenote_push.py --verify
"""
import argparse
import json
import mimetypes
import random
import re
import sys
import time
import urllib.error
import urllib.request
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import msauth  # noqa: E402

import config  # noqa: E402

ROOT = config.HOME
STAGING = config.STAGING
LEDGER = config.LEDGER
GRAPH = "https://graph.microsoft.com/v1.0"
NOTEBOOK = config.NOTEBOOK

# Graph rejects anything over 4 MB, and allows at most 6 parts per request
# (the Presentation/Commands part plus five data parts).
MAX_REQUEST_BYTES = 3_300_000
MAX_DATA_PARTS = 5
# Small gap between pages; cheaper than repeatedly hitting the throttle.
PACE = 0.4
CRLF = "\r\n"


# --------------------------------------------------------------------------
# HTTP


class GraphError(Exception):
    def __init__(self, status, body):
        super().__init__(f"{status}: {body[:400]}")
        self.status = status
        self.body = body


class PartialPush(Exception):
    """The page was created but not all of its attachments landed."""

    def __init__(self, page_id, cause):
        super().__init__(str(cause))
        self.page_id = page_id
        self.cause = cause


def request(method, path, tok, body=None, content_type=None, retries=10):
    url = path if path.startswith("http") else GRAPH + path
    for attempt in range(retries):
        headers = {"Authorization": f"Bearer {tok}", "Accept": "application/json"}
        if content_type:
            headers["Content-Type"] = content_type
        req = urllib.request.Request(url, data=body, method=method, headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=300) as r:
                raw = r.read()
                return r.status, (json.loads(raw) if raw else {})
        except urllib.error.HTTPError as e:
            text = e.read().decode("utf-8", "replace")
            if e.code in (429, 503, 504) and attempt < retries - 1:
                wait = float(e.headers.get("Retry-After") or 0)
                if not wait:
                    # OneNote's per-user throttle window outlasts a short
                    # exponential backoff, so 429 waits start much higher.
                    wait = max(20.0, 5 * 2 ** attempt) if e.code == 429 else 2 ** attempt
                wait += random.uniform(0, 2)
                print(f"    throttled ({e.code}), waiting {wait:.0f}s", flush=True)
                time.sleep(wait)
                continue
            if e.code == 401:
                raise GraphError(401, text)
            raise GraphError(e.code, text)
        except urllib.error.URLError as e:
            if attempt < retries - 1:
                time.sleep(2 ** attempt)
                continue
            raise GraphError(0, str(e))
    raise GraphError(0, "retries exhausted")


def multipart(parts):
    """Build a multipart/form-data body. parts = [(name, content_type, bytes)]."""
    boundary = f"Part{uuid.uuid4().hex}"
    buf = bytearray()
    for name, ctype, payload in parts:
        buf += f"--{boundary}{CRLF}".encode()
        buf += f'Content-Disposition: form-data; name="{name}"{CRLF}'.encode()
        buf += f"Content-Type: {ctype}{CRLF}{CRLF}".encode()
        buf += payload
        buf += CRLF.encode()
    buf += f"--{boundary}--{CRLF}".encode()
    return bytes(buf), f"multipart/form-data; boundary={boundary}"


# --------------------------------------------------------------------------
# Ledger


def load_ledger():
    done = {}
    if LEDGER.exists():
        for line in LEDGER.read_text().splitlines():
            if line.strip():
                rec = json.loads(line)
                done[rec["note_id"]] = rec
    return done


def record(rec):
    with LEDGER.open("a") as f:
        f.write(json.dumps(rec) + "\n")


# --------------------------------------------------------------------------
# Sections


def find_notebook(tok, create=False, name=None):
    name = name or NOTEBOOK
    _, data = request("GET", "/me/onenote/notebooks", tok)
    books = data.get("value", [])
    for nb in books:
        if nb["displayName"] == name:
            return nb
    if create:
        body = json.dumps({"displayName": name}).encode()
        _, nb = request("POST", "/me/onenote/notebooks", tok, body, "application/json")
        print(f"created notebook {name!r}")
        return nb
    sys.exit(f"Notebook {name!r} not found. Have: "
             f"{[b['displayName'] for b in books]}\n"
             f"Run with --create-notebook to make it.")


def ensure_section(tok, notebook_id, name, cache):
    """Return the id of the section called `name`, creating it if needed."""
    if name in cache:
        return cache[name]
    _, data = request("GET", f"/me/onenote/notebooks/{notebook_id}/sections", tok)
    for s in data.get("value", []):
        cache[s["displayName"]] = s["id"]
    if name in cache:
        return cache[name]
    body = json.dumps({"displayName": name}).encode()
    _, created = request("POST", f"/me/onenote/notebooks/{notebook_id}/sections",
                         tok, body, "application/json")
    print(f"  created section {name!r}")
    cache[name] = created["id"]
    return created["id"]


# --------------------------------------------------------------------------
# Pages


def plan_parts(html_bytes, parts):
    """Split attachments into an initial POST batch plus PATCH batches."""
    initial, deferred = [], []
    used = html_bytes
    for p in parts:
        if len(initial) < MAX_DATA_PARTS and used + p["bytes"] < MAX_REQUEST_BYTES:
            initial.append(p)
            used += p["bytes"]
        else:
            deferred.append(p)

    batches = []
    cur, cur_bytes = [], 0
    for p in deferred:
        if cur and (len(cur) >= MAX_DATA_PARTS or cur_bytes + p["bytes"] >= MAX_REQUEST_BYTES):
            batches.append(cur)
            cur, cur_bytes = [], 0
        cur.append(p)
        cur_bytes += p["bytes"]
    if cur:
        batches.append(cur)
    return initial, batches


def media_tag(p):
    if p["kind"] == "img":
        return f'<img src="name:{p["name"]}" />'
    return (f'<object data-attachment="{p["filename"]}" '
            f'data="name:{p["name"]}" type="{p["mime"]}" />')


def defer_in_html(html, part):
    """Swap a media tag for a div the PATCH step can append into.

    The div needs content: OneNote discards empty ones while building the page,
    and the later PATCH then fails with "target cannot be located".
    """
    slot = f'<div data-id="slot_{part["name"]}">&#160;</div>'
    pattern = (rf'<img[^>]*src="name:{part["name"]}"[^>]*/>' if part["kind"] == "img"
               else rf'<object[^>]*data="name:{part["name"]}"[^>]*/>')
    html, n = re.subn(pattern, slot, html, count=1)
    if n == 0:
        # The tag was appended to the page rather than sitting inline.
        html = html.replace("</body>", slot + "\n</body>")
    return html


def patch_when_ready(tok, page_id, payload, ctype, attempts=6):
    """PATCH a page, tolerating it not being addressable yet.

    Creating a page returns its id before OneNote can serve it, so an
    immediate PATCH can come back 404 for a page that does exist and will
    appear a moment later.
    """
    for attempt in range(attempts):
        try:
            return request("PATCH", f"/me/onenote/pages/{page_id}/content",
                           tok, payload, ctype)
        except GraphError as e:
            if e.status == 404 and attempt < attempts - 1:
                wait = 2 * (attempt + 1)
                print(f"    page not ready yet, waiting {wait}s", flush=True)
                time.sleep(wait)
                continue
            raise


def push_note(tok, section_id, note_dir):
    meta = json.loads((note_dir / "meta.json").read_text())
    html = (note_dir / "page.html").read_text(encoding="utf-8")
    parts = meta["parts"]

    initial, batches = plan_parts(len(html.encode()), parts)
    for p in (x for b in batches for x in b):
        html = defer_in_html(html, p)

    body_parts = [("Presentation", "text/html; charset=utf-8", html.encode("utf-8"))]
    for p in initial:
        body_parts.append((p["name"], p["mime"], (note_dir / "parts" / p["file"]).read_bytes()))

    if len(body_parts) == 1:
        payload, ctype = html.encode("utf-8"), "text/html; charset=utf-8"
    else:
        payload, ctype = multipart(body_parts)

    _, page = request("POST", f"/me/onenote/sections/{section_id}/pages",
                      tok, payload, ctype)
    page_id = page["id"]

    for batch in batches:
        commands = [{"target": f'#slot_{p["name"]}', "action": "append",
                     "content": media_tag(p)} for p in batch]
        bp = [("Commands", "application/json", json.dumps(commands).encode())]
        for p in batch:
            bp.append((p["name"], p["mime"], (note_dir / "parts" / p["file"]).read_bytes()))
        payload, ctype = multipart(bp)
        try:
            patch_when_ready(tok, page_id, payload, ctype)
        except GraphError as e:
            # The page exists but is incomplete. Surface its id so the caller
            # can record it and delete it before retrying, instead of leaving
            # a half-filled duplicate behind.
            raise PartialPush(page_id, e) from e

    return page_id, meta, len(initial), sum(len(b) for b in batches)


# --------------------------------------------------------------------------


def note_dirs(section=None):
    manifest = json.loads((STAGING / "manifest.json").read_text())
    for entry in manifest:
        if section and entry["section"] != section:
            continue
        dirs = [STAGING / entry["section"] / n["note_id"] for n in entry["notes"]]
        yield entry["section"], dirs


def cmd_push(args):
    if args.dry_run:
        return cmd_plan(args)
    tok = msauth.token()
    notebook = find_notebook(tok, args.create_notebook, args.notebook)
    print(f"notebook: {notebook['displayName']}")
    cache = {}
    done = load_ledger()
    pushed = failed = 0

    for section, dirs in note_dirs(args.section):
        todo = [d for d in dirs if done.get(d.name, {}).get("status") != "ok"]
        if args.limit:
            todo = todo[: args.limit]
        if not todo:
            print(f"\n{section}: nothing to do ({len(dirs)} already pushed)")
            continue
        print(f"\n{section}: {len(todo)} of {len(dirs)} to push")
        if args.dry_run:
            continue
        section_id = ensure_section(tok, notebook["id"], section, cache)

        for i, d in enumerate(todo, 1):
            meta = json.loads((d / "meta.json").read_text())
            label = meta["title"][:58]

            # A previous attempt may have left a half-built page behind; drop it
            # so this run replaces it rather than adding a duplicate.
            stale = done.get(d.name, {}).get("page_id")
            if stale:
                try:
                    request("DELETE", f"/me/onenote/pages/{stale}", tok)
                    print(f"  [{i}/{len(todo)}] removed incomplete earlier page")
                except GraphError:
                    pass

            try:
                page_id, meta, n_init, n_patch = push_note(tok, section_id, d)
            except (GraphError, PartialPush) as exc:
                if isinstance(exc, GraphError) and exc.status == 401:
                    tok = msauth.token()
                    try:
                        page_id, meta, n_init, n_patch = push_note(tok, section_id, d)
                    except (GraphError, PartialPush) as retry_exc:
                        exc = retry_exc
                    else:
                        _ok(section, d, page_id, meta, i, len(todo), label, n_init, n_patch)
                        pushed += 1
                        continue
                failed += 1
                if isinstance(exc, PartialPush):
                    err, page_id = exc.cause, exc.page_id
                else:
                    err, page_id = exc, None
                print(f"  [{i}/{len(todo)}] FAIL {label} -> {err.status}")
                print(f"          {err.body[:300]}")
                record({"note_id": d.name, "section": section, "status": "error",
                        "page_id": page_id, "error": f"{err.status}: {err.body[:300]}",
                        "ts": time.time()})
                continue
            _ok(section, d, page_id, meta, i, len(todo), label, n_init, n_patch)
            pushed += 1
            time.sleep(PACE)

    print(f"\npushed {pushed}, failed {failed}")
    if failed:
        print("re-run the same command to retry the failures")


def _ok(section, d, page_id, meta, i, total, label, n_init, n_patch):
    extra = ""
    if n_init or n_patch:
        extra = f"  (+{n_init} inline, {n_patch} patched)" if n_patch else f"  (+{n_init} files)"
    print(f"  [{i}/{total}] {label}{extra}")
    record({"note_id": d.name, "section": section, "page_id": page_id,
            "title": meta["title"], "status": "ok", "ts": time.time()})


def cmd_plan(args):
    """Show what would be uploaded, without touching the network."""
    done = load_ledger()
    total = requests = 0
    heavy = []
    for section, dirs in note_dirs(args.section):
        todo = [d for d in dirs if done.get(d.name, {}).get("status") != "ok"]
        if args.limit:
            todo = todo[: args.limit]
        for d in todo:
            meta = json.loads((d / "meta.json").read_text())
            html = (d / "page.html").read_text(encoding="utf-8")
            initial, batches = plan_parts(len(html.encode()), meta["parts"])
            total += 1
            requests += 1 + len(batches)
            if batches:
                heavy.append((len(meta["parts"]), len(batches), section, meta["title"][:50]))
        print(f"{section:32} {len(todo):4d} notes")
    print(f"\n{total} notes -> {requests} Graph requests")
    if heavy:
        print(f"\n{len(heavy)} notes need PATCH follow-ups:")
        for n, b, s, t in sorted(heavy, reverse=True)[:12]:
            print(f"  {n:3d} files, {b} patches   {s} / {t}")


def cmd_verify(args):
    tok = msauth.token()
    notebook = find_notebook(tok, name=args.notebook)
    _, data = request("GET", f"/me/onenote/notebooks/{notebook['id']}/sections", tok)
    live = {s["displayName"]: s for s in data.get("value", [])}
    manifest = json.loads((STAGING / "manifest.json").read_text())
    done = load_ledger()

    # Compare by page id, not by counting: OneNote populates a page's `title`
    # metadata asynchronously, so a fresh page can list with an empty title,
    # and pages deleted earlier can linger in the listing for a while.
    print(f"{'section':32} {'staged':>7} {'pushed':>7} {'live':>6} {'missing':>8} {'extra':>6}")
    bad = 0
    orphans = []
    for entry in manifest:
        name = entry["section"]
        staged = len(entry["notes"])
        expected = {}
        for n in entry["notes"]:
            rec = done.get(n["note_id"], {})
            if rec.get("status") == "ok" and rec.get("page_id"):
                expected[rec["page_id"]] = rec
        live_ids = set()
        if name in live:
            url = f"/me/onenote/sections/{live[name]['id']}/pages?$top=100&$select=id"
            while url:
                _, page = request("GET", url, tok)
                live_ids |= {p["id"] for p in page.get("value", [])}
                url = page.get("@odata.nextLink")
        missing = [p for p in expected if p not in live_ids]
        extra = [p for p in live_ids if p not in expected]
        orphans += [(name, p) for p in extra]
        flag = ""
        if len(expected) != staged or missing or extra:
            flag = "   <--"
            bad += 1
        print(f"{name:32} {staged:7} {len(expected):7} {len(live_ids):6} "
              f"{len(missing):8} {len(extra):6}{flag}")

    print("\nall sections match" if not bad else f"\n{bad} sections need attention")
    if orphans and args.delete_orphans:
        print(f"\ndeleting {len(orphans)} pages not in the ledger")
        for name, pid in orphans:
            try:
                request("DELETE", f"/me/onenote/pages/{pid}", tok)
                print(f"  deleted an orphan in {name}")
            except GraphError as e:
                print(f"  could not delete orphan in {name}: {e.status}")
    elif orphans:
        print(f"{len(orphans)} live pages are not in the ledger "
              f"(leftovers from a failed attempt); re-run with --delete-orphans "
              f"to remove them")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--section", help="only this section, e.g. Blockchain")
    ap.add_argument("--limit", type=int, help="at most N notes per section")
    ap.add_argument("--all", action="store_true", help="push everything")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--verify", action="store_true", help="compare staged vs OneNote")
    ap.add_argument("--create-notebook", action="store_true",
                    help="create the target notebook if it does not exist")
    ap.add_argument("--notebook", default=NOTEBOOK, help="target notebook name")
    ap.add_argument("--delete-orphans", action="store_true",
                    help="with --verify: delete live pages absent from the ledger")
    args = ap.parse_args()

    if args.verify:
        cmd_verify(args)
    elif args.section or args.all or args.dry_run or args.create_notebook:
        cmd_push(args)
    else:
        ap.error("pass --section NAME, --all, or --verify")



if __name__ == "__main__":
    main()
