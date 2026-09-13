# Limitations and API quirks

Most of this was learned by hitting it. If something below looks like a bug in your migration, it probably is not yours.

## Hard limits of the OneNote API

| Limit | Value | What the importer does |
|---|---|---|
| Request size | 4 MB | Budgets 3.3 MB per request and splits the rest into follow-ups |
| Data parts per request | 6, including the HTML | At most 5 attachments per request |
| Images per page | 150 | Not reached in practice |
| Pages per section | a few thousand | Returns `507` if exceeded; split the notebook |

A note with more than five attachments is uploaded in stages: the page is created with the first five, then each remaining batch is added with a `PATCH`. A note with 47 images takes ten requests.

An attachment larger than about 3 MB cannot be uploaded at all, because no single request can carry it. Those files are written to `oversized/` and listed at the bottom of the page so you can drag them in manually. Images get downscaled to 1800px first, which rescues almost all of them — a 42 MB screenshot becomes 350 KB.

## Quirks worth knowing

**An empty `<div data-id="x"></div>` is silently dropped.** This matters if you build on this code: the staged HTML leaves placeholder divs where deferred attachments will go, and a later `PATCH` targets them by id. OneNote discards empty divs while building the page, so the `PATCH` then fails with "The PATCH target cannot be located." The fix is to give the div content — a non-breaking space is enough.

**A page is not addressable the instant it is created.** `POST /pages` returns an id, but a `PATCH` against it moments later can come back `404`. The importer waits and retries.

**The page listing is eventually consistent, in both directions.** A page created seconds ago can appear with an empty `title` for several minutes, while its content is perfectly correct. A deleted page can keep appearing in listings, and fetching it returns `404`. Occasionally a page reported as `404` turns out to still exist. Because of this, never verify a migration by counting pages — compare page ids, which is what `--verify` does.

**Deleting a section is not supported for personal notebooks.** `DELETE /sections/{id}` answers `503` every time, permanently. Delete sections by hand in the OneNote app. `cleanup_sections.py` attempts it once, reports the refusal, and moves on rather than retrying.

**Throttling is aggressive and the window is long.** Bulk deletes in particular will get you `429` for several minutes afterwards. The importer backs off starting at 20 seconds for `429`, honours `Retry-After`, and paces itself between pages. If you write your own tooling against this API, do not retry tightly — you will extend the lockout.

**CSS is mostly discarded.** OneNote accepts a small subset of HTML and keeps only a handful of inline style properties. The converter strips the rest before uploading, which is why a web clip shrinks from 87 KB to 16 KB with no visible loss.

**Input HTML must be well-formed XHTML.** Every container tag needs a closing tag — a self-closed `<a/>` will break the request. Named entities other than the XML built-ins are not defined either: write `&#160;`, not `&nbsp;`.

## Content that does not transfer

**Checkboxes.** Evernote's `<en-todo>` becomes a `☐` or `☑` character. OneNote's native checkbox is set with a `data-tag` attribute that the input HTML does not reliably honour for this purpose. Re-apply them in OneNote with ⌘1 if you need real ones.

**Encrypted note content.** `<en-crypt>` blocks are skipped. Decrypt them in Evernote before exporting.

**Tags.** Recorded in the header line of each page, not as OneNote tags. OneNote's tag model is per-paragraph and does not map cleanly onto Evernote's per-note tags.

**Evernote note links.** `evernote:///` links between notes are dropped, since the destinations no longer exist. Web links are preserved.

**Trashed notes.** Not exported. Add `--include-trash` to the export command if you want them.

## Note identity

The importer must recognise a note it has already uploaded, across re-exports, or a re-run would duplicate everything.

With `--add-guid` (what `export.sh` uses), identity comes from Evernote's own note GUID. That survives edits, re-exports, and notes being moved between notebooks.

Without it — exports made through Evernote's own File → Export — identity falls back to a hash of the note's title, creation time and content. That is stable as long as the note does not change, but editing a note in Evernote and re-exporting will make it look like a new note. Prefer `--add-guid`.

Genuinely duplicated notes, identical in title, date and content, are distinguished by a counter. Order within the export decides which is which, so a re-export could in principle swap two identical notes. They are identical, so it does not matter.
