#!/usr/bin/env python3
"""Convert the Evernote ENEX exports into a staging tree the pusher can upload.

One directory per note holds the OneNote-ready XHTML plus each attachment as a
loose file, so the upload step is pure I/O and every note can be inspected or
re-pushed on its own.

    python3 tools/enex2staging.py [--only nb_Blockchain] [--limit N]
"""
import argparse
import base64
import hashlib
import io
import json
import mimetypes
import re
import shutil
import sys
from datetime import datetime, timezone
from pathlib import Path

from lxml import etree

sys.path.insert(0, str(Path(__file__).resolve().parent))
import enml  # noqa: E402

import config  # noqa: E402

ROOT = config.HOME
STAGING = config.STAGING
OVERSIZED = config.OVERSIZED

# Graph caps a request at 4 MB; leave room for headers and the HTML itself.
MAX_PART_BYTES = 3_000_000
IMAGE_MAX_DIM = 1800

ENEX_DIR = config.ENEX_DIR

# Rename a notebook, or merge several into one section. Edit
# section_overrides.json rather than this file; see config.py.
SECTION_OF = config.section_overrides()

# Characters OneNote rejects in a section name.
BAD_SECTION_CHARS = r'[?*\\/:<>|#"%~]'


def section_name(stem):
    """Turn an Evernote notebook name into a legal OneNote section name."""
    name = SECTION_OF.get(stem, stem)
    name = name.replace("&", "and")
    name = re.sub(BAD_SECTION_CHARS, "", name)
    return re.sub(r"\s+", " ", name).strip() or "Untitled"


def enex_files(enex_dir=None):
    """{notebook name: path} for every .enex in the export directory.

    Evernote's own File > Export sometimes produces a doubled `.enex.enex`
    extension, and exporting the same notebook twice leaves both spellings
    side by side, so collapse them onto one entry.
    """
    found = {}
    for p in sorted(Path(enex_dir or ENEX_DIR).glob("*.enex*")):
        stem = re.sub(r"(\.enex)+$", "", p.name)
        found.setdefault(stem, p)
    return found


def iter_notes(path):
    ctx = etree.iterparse(str(path), events=("end",), tag="note",
                          recover=True, huge_tree=True)
    for _, el in ctx:
        yield el
        el.clear()
        while el.getprevious() is not None:
            del el.getparent()[0]


def parse_ts(raw):
    """Evernote timestamps look like 20171119T231851Z."""
    if not raw:
        return None
    try:
        return datetime.strptime(raw.strip(), "%Y%m%dT%H%M%SZ").replace(tzinfo=timezone.utc)
    except ValueError:
        return None


def shrink_image(data, mime):
    """Downscale an oversized image so it fits inside one Graph request."""
    try:
        from PIL import Image
    except ImportError:
        return None, mime
    try:
        im = Image.open(io.BytesIO(data))
        im.load()
    except Exception:
        return None, mime
    im.thumbnail((IMAGE_MAX_DIM, IMAGE_MAX_DIM), Image.LANCZOS)

    has_alpha = im.mode in ("RGBA", "LA", "P")
    if has_alpha and mime == "image/png":
        buf = io.BytesIO()
        im.save(buf, "PNG", optimize=True)
        if len(buf.getvalue()) <= MAX_PART_BYTES:
            return buf.getvalue(), "image/png"
        # An optimized PNG of a photographic screenshot can still blow the
        # budget; flatten onto white and go JPEG instead.
        flat = Image.new("RGB", im.size, (255, 255, 255))
        rgba = im.convert("RGBA")
        flat.paste(rgba, mask=rgba.split()[-1])
        im = flat

    buf = io.BytesIO()
    im.convert("RGB").save(buf, "JPEG", quality=82, optimize=True)
    return buf.getvalue(), "image/jpeg"


def ext_for(mime, filename):
    if filename and "." in filename:
        return "." + filename.rsplit(".", 1)[1].lower()
    return mimetypes.guess_extension(mime or "") or ".bin"


def safe_filename(name, mime, index):
    name = (name or "").strip()
    name = re.sub(r'[\\/:*?"<>|]', "_", name)
    if not name:
        name = f"attachment{index}{ext_for(mime, '')}"
    return name[:120]


def note_identity(title, created_raw, content, seen, guid=None):
    """Stable id for a note, independent of which file it arrived in.

    Evernote's own note GUID is used when the export carries one (pass
    --add-guid to evernote-backup); it survives edits and re-exports. Without
    it, fall back to hashing the note's content, which is stable enough as
    long as the note itself does not change.

    Exports can genuinely contain the same note twice; those collide here by
    design, so a counter keeps them distinct while leaving every unique note's
    id unchanged from one export to the next.
    """
    if guid:
        base = f"g{hashlib.sha1(guid.encode()).hexdigest()[:15]}"
    else:
        base = hashlib.sha1(
            f"{title}|{created_raw}|{hashlib.md5((content or '').encode()).hexdigest()}".encode()
        ).hexdigest()[:16]
    if base not in seen:
        seen.add(base)
        return base
    n = 2
    while f"{base}-{n}" in seen:
        n += 1
    seen.add(f"{base}-{n}")
    return f"{base}-{n}"


def build_note(note_el, section, stem, index, report, seen_ids):
    title = (note_el.findtext("title") or "").strip() or "Untitled"
    created = parse_ts(note_el.findtext("created"))
    updated = parse_ts(note_el.findtext("updated"))
    tags = [t.text for t in note_el.findall("tag") if t.text]
    attrs = note_el.find("note-attributes")
    source_url = attrs.findtext("source-url") if attrs is not None else None
    content = note_el.findtext("content") or ""

    # Identify a note by what it *is*, never by which file it arrived in or
    # where it sat in that file. Re-exporting from Evernote changes both, and
    # an id that moved would make the upload ledger look empty and re-push
    # notes that are already in OneNote.
    note_id = note_identity(title, note_el.findtext("created"), content,
                            seen_ids, note_el.findtext("guid"))

    # Index every resource by the MD5 that <en-media> references.
    resources = {}
    order = []
    for i, r in enumerate(note_el.findall("resource")):
        raw = (r.findtext("data") or "").strip()
        if not raw:
            continue
        try:
            data = base64.b64decode(raw)
        except Exception:
            report.append(f"{note_id} {title!r}: undecodable resource #{i}")
            continue
        mime = (r.findtext("mime") or "application/octet-stream").strip()
        ra = r.find("resource-attributes")
        fname = ra.findtext("file-name") if ra is not None else None
        digest = hashlib.md5(data).hexdigest()
        if digest in resources:
            continue
        resources[digest] = {
            "data": data, "mime": mime, "filename": fname, "index": i,
        }
        order.append(digest)

    note_dir = STAGING / section / note_id
    if note_dir.exists():
        shutil.rmtree(note_dir)
    parts_dir = note_dir / "parts"
    parts_dir.mkdir(parents=True)

    parts = []          # metadata for the pusher
    resolved = {}       # md5 -> part tuple handed to the sanitizer
    skipped = []

    for n, digest in enumerate(order):
        res = resources[digest]
        data, mime = res["data"], res["mime"]
        is_image = mime.startswith("image/") and mime != "image/svg+xml"

        if len(data) > MAX_PART_BYTES and is_image:
            shrunk, new_mime = shrink_image(data, mime)
            if shrunk and len(shrunk) <= MAX_PART_BYTES:
                report.append(
                    f"{note_id} {title!r}: downscaled {len(data)/1e6:.1f}MB "
                    f"-> {len(shrunk)/1e6:.2f}MB image")
                data, mime = shrunk, new_mime

        filename = safe_filename(res["filename"], mime, res["index"])

        if len(data) > MAX_PART_BYTES:
            # Too large for any single Graph request; keep it on disk instead.
            OVERSIZED.mkdir(exist_ok=True)
            out = OVERSIZED / f"{note_id}_{filename}"
            out.write_bytes(data)
            skipped.append((filename, len(data), out))
            report.append(
                f"{note_id} {title!r}: SKIPPED {filename} "
                f"({len(data)/1e6:.1f}MB) -> {out.relative_to(ROOT)}")
            continue

        part = f"p{n}"
        ext = ext_for(mime, filename)
        blob = parts_dir / f"{part}{ext}"
        blob.write_bytes(data)
        kind = "img" if mime.startswith("image/") and mime != "image/svg+xml" else "object"
        parts.append({
            "name": part, "file": blob.name, "mime": mime,
            "filename": filename, "kind": kind, "bytes": len(data),
        })
        resolved[digest] = (kind, part, mime, filename)

    used = []

    def resolver(digest, _mime):
        hit = resolved.get((digest or "").lower())
        if hit:
            used.append(hit[1])
        return hit

    body, _ = enml.sanitize(content, resolver, note_id)

    # Resources the note body never referenced still belong on the page.
    for p in parts:
        if p["name"] in used:
            continue
        if p["kind"] == "img":
            body += f'\n<p><img src="name:{p["name"]}" /></p>'
        else:
            body += (f'\n<p><object data-attachment="{esc(p["filename"])}" '
                     f'data="name:{p["name"]}" type="{p["mime"]}" /></p>')

    header = meta_header(created, updated, tags, source_url)
    footer = ""
    if skipped:
        rows = "".join(
            f"<li>{esc(f)} ({b/1e6:.1f} MB) &#8212; {esc(str(p.relative_to(ROOT)))}</li>"
            for f, b, p in skipped)
        footer = ("<hr /><p><i>Attachments too large for the OneNote API, "
                  f"kept on disk:</i></p><ul>{rows}</ul>")

    page = (
        "<!DOCTYPE html>\n<html>\n<head>\n"
        f"  <title>{esc(title)}</title>\n"
        + (f'  <meta name="created" content="{created.isoformat()}" />\n' if created else "")
        + "</head>\n<body>\n" + header + body + footer + "\n</body>\n</html>\n"
    )
    (note_dir / "page.html").write_text(page, encoding="utf-8")
    (note_dir / "meta.json").write_text(json.dumps({
        "note_id": note_id,
        "section": section,
        "source": stem,
        "index": index,
        "title": title,
        "created": created.isoformat() if created else None,
        "updated": updated.isoformat() if updated else None,
        "tags": tags,
        "source_url": source_url,
        "parts": parts,
        "skipped": [{"filename": f, "bytes": b, "path": str(p.relative_to(ROOT))}
                    for f, b, p in skipped],
        "html_bytes": len(page.encode()),
    }, indent=2), encoding="utf-8")
    return note_id, len(page.encode()), parts


def meta_header(created, updated, tags, source_url):
    bits = []
    if created:
        bits.append(f"Created: {created.strftime('%Y-%m-%d %H:%M UTC')}")
    if updated:
        bits.append(f"Updated: {updated.strftime('%Y-%m-%d %H:%M UTC')}")
    if tags:
        bits.append("Tags: " + ", ".join(tags))
    line = " &#160;|&#160; ".join(esc(b) for b in bits)
    out = f'<p><i>{line}</i></p>' if line else ""
    if source_url and source_url.startswith(("http://", "https://")):
        out += f'<p><i>Source: <a href="{esc(source_url)}">{esc(source_url)}</a></i></p>'
    return out + "<hr />\n"


def esc(s):
    return (str(s).replace("&", "&amp;").replace("<", "&lt;")
            .replace(">", "&gt;").replace('"', "&quot;"))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--only", help="a single notebook name, e.g. Blockchain")
    ap.add_argument("--enex-dir", default=str(ENEX_DIR),
                    help="directory holding the exported .enex files")
    ap.add_argument("--limit", type=int, help="first N notes per export")
    ap.add_argument("--clean", action="store_true", help="wipe staging/ first")
    args = ap.parse_args()

    if args.clean and STAGING.exists():
        shutil.rmtree(STAGING)
    STAGING.mkdir(exist_ok=True)

    report = []
    manifest = {}
    seen_ids = set()
    total = 0
    for stem, path in enex_files(args.enex_dir).items():
        if args.only and stem != args.only:
            continue
        section = section_name(stem)
        entry = manifest.setdefault(section, {"section": section, "sources": [], "notes": []})
        entry["sources"].append(path.name)
        for i, note_el in enumerate(iter_notes(path)):
            if args.limit and i >= args.limit:
                break
            note_id, nbytes, parts = build_note(note_el, section, stem, i, report, seen_ids)
            entry["notes"].append({"note_id": note_id, "parts": len(parts), "html_bytes": nbytes})
            total += 1
        print(f"{section:32} {len(entry['notes']):4d} notes   ({path.name})")

    (STAGING / "manifest.json").write_text(
        json.dumps(list(manifest.values()), indent=2), encoding="utf-8")

    print(f"\n{total} notes staged into {STAGING.relative_to(ROOT)}/")
    print(f"{len(manifest)} sections")
    if report:
        (STAGING / "conversion_report.txt").write_text("\n".join(report), encoding="utf-8")
        print(f"\n{len(report)} notes needed special handling "
              f"(see staging/conversion_report.txt):")
        for line in report[:12]:
            print("  " + line)



if __name__ == "__main__":
    main()
