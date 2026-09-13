# evernote-to-onenote

Migrate an Evernote account into OneNote with **original creation dates, images and file attachments intact** — using the Microsoft Graph API rather than automating the OneNote user interface.

One Evernote notebook becomes one OneNote section. One note becomes one page.

## Why not just automate the OneNote app?

The obvious approach is to script the OneNote desktop app: click "Add page", type the title, paste the body. It looks impressive and it mostly works. It is also the wrong tool, and it loses things you cannot get back:

| | Driving the OneNote UI | This project (Graph API) |
|---|---|---|
| Original creation date | impossible — every page is stamped today | preserved exactly |
| Images and attachments | lost, or manual drag-and-drop | uploaded with the page |
| Creating a section | type the name and hope the UI keeps up | one REST call, returns its id |
| A run that fails halfway | restart and get duplicates | resumable ledger, no duplicates |
| Verifying the result | open pages and look | automated, note by note |
| 600 notes | hours, babysat | about an hour, unattended |

OneNote's page-creation API accepts `<meta name="created" content="...">`, which is what makes the dates survive. Nothing in the OneNote app exposes that.

This project was in fact built the other way first, driving the OneNote app with AppleScript, before being rewritten. [docs/why-not-gui-automation.md](docs/why-not-gui-automation.md) is the post-mortem.

## Requirements

- Python 3.9+
- An Evernote account, and a Microsoft account with OneNote
- `pip3 install -r requirements.txt`
- For the export step: `pip3 install --user evernote-backup`

## Setup

Work in an empty directory — this is where your notes and credentials will live, and none of it belongs in a git repo.

```bash
git clone https://github.com/Gouliath1/evernote-to-onenote.git
mkdir ~/my-migration && cd ~/my-migration
pip3 install -r ~/evernote-to-onenote/requirements.txt
```

Register an Azure app so the tool can talk to OneNote as you — it takes about three minutes and is free. Follow [docs/azure-app-setup.md](docs/azure-app-setup.md), then:

```bash
echo 'YOUR-APPLICATION-CLIENT-ID' > .client_id
export ONENOTE_NOTEBOOK="Evernote Import"
```

All commands below assume `~/evernote-to-onenote/tools` is on your path or typed in full, and that you run them from your working directory.

## 1. Export from Evernote

```bash
~/evernote-to-onenote/tools/export.sh
```

Signs in through Evernote's own browser page, syncs your account to a local database, and writes one `.enex` file per notebook into `enex/`.

Run this in a real terminal — the sign-in step prompts for input. Notes in the Evernote trash are skipped.

If you already have `.enex` files from Evernote's own **File → Export Notes**, skip this step and drop them into `enex/`. They will work, but without Evernote's note GUIDs the importer falls back to content hashing to recognise notes it has already uploaded. See [docs/limitations.md](docs/limitations.md).

## 2. Convert

```bash
python3 ~/evernote-to-onenote/tools/enex2staging.py
```

Reads `enex/` and writes `staging/<section>/<note-id>/` — one directory per note holding the OneNote-ready XHTML plus each attachment as a loose file.

This step does the real work: Evernote's ENML is reduced to the small subset of HTML OneNote accepts, `<en-media>` references are resolved to their attachments, checkboxes become text markers, and oversized images are downscaled to fit inside a Graph request. Nothing touches the network, so you can inspect or re-run it freely.

To rename a notebook or merge several into one section, create `section_overrides.json`:

```json
{
  "Book_ The AI Advantage": "Book The AI Advantage",
  "Inbox_part1": "Inbox",
  "Inbox_part2": "Inbox"
}
```

Keys are `.enex` filenames without the extension. OneNote forbids `? * \ / : < > | & # " % ~` in section names; the converter strips them and turns `&` into "and", so check the printed section list before importing.

## 3. Import

Try one notebook first:

```bash
python3 ~/evernote-to-onenote/tools/onenote_push.py --create-notebook --section Blockchain
```

Open it in OneNote. Check the title, the formatting, the images, and — the point of all this — the creation date. Then run the rest:

```bash
python3 ~/evernote-to-onenote/tools/onenote_push.py --all
```

Every uploaded page is recorded in `ledger.jsonl`, so this is safe to interrupt and re-run: it uploads only what is missing. If a page was created but its attachments failed, the ledger remembers that too, and the retry deletes the half-built page before replacing it — you never end up with duplicates.

Useful flags:

```
--dry-run          plan the run offline, no network
--section NAME     just one section
--limit N          at most N notes per section
--notebook NAME    target a different notebook
```

## 4. Verify

```bash
python3 ~/evernote-to-onenote/tools/reconcile.py          # counts and titles
python3 ~/evernote-to-onenote/tools/reconcile.py --deep   # also check every attachment
```

Reads the `.enex` files again — not the staging tree — and compares them against what is actually in OneNote, note by note. Anything missing is listed in `reconcile_problems.txt`.

```bash
python3 ~/evernote-to-onenote/tools/onenote_push.py --verify
python3 ~/evernote-to-onenote/tools/onenote_push.py --verify --delete-orphans
```

Compares by page id rather than by counting, which matters because OneNote's page listing is eventually consistent: a page created seconds ago can list with an empty title, and a deleted page can linger for minutes.

## What gets preserved

- Page title, and the **original Evernote creation date**
- Headings, bold, italic, lists, tables, links, block quotes, preformatted text
- Embedded images, inline, in their original position
- File attachments (PDF, Office documents, and so on) as OneNote attachments
- A header line on each page carrying the created and updated timestamps, tags, and the source URL for web clips

## What does not survive

- **Evernote checkboxes** become `☐` / `☑` text characters. OneNote's native checkbox is not expressible in the API's input HTML.
- **Encrypted note content** is skipped — decrypt it in Evernote first.
- **Attachments over ~3 MB** cannot be uploaded: Graph caps a request at 4 MB. They are written to `oversized/` and linked by name from the page, for you to drag in by hand.
- **Very large images** are downscaled to 1800px so they fit. The originals stay in your `.enex` files.
- **Note tags** are recorded in the header line, not as OneNote tags.

See [docs/limitations.md](docs/limitations.md) for the full list, including the OneNote API quirks worth knowing before you debug something that is not your fault.

## Layout

```
tools/
  export.sh            Evernote -> enex/            (wraps evernote-backup)
  enex2staging.py      enex/ -> staging/
  enml.py              the ENML -> OneNote HTML sanitiser
  onenote_push.py      staging/ -> OneNote, resumable
  reconcile.py         Evernote vs OneNote, note by note
  cleanup_sections.py  remove sections no longer backed by a notebook
  msauth.py            device-code OAuth
  config.py            paths and settings
```

## Credits

The export step wraps [evernote-backup](https://github.com/vzhd1701/evernote-backup) by vzhd1701, which does the hard part of talking to Evernote.

## Licence

MIT — see [LICENSE](LICENSE).
