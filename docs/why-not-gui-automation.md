# Why not GUI automation

This project started as a script that drove the OneNote desktop app. That version worked, in the sense that notes appeared on screen. It was abandoned, and the reasons are worth writing down — partly to justify the rewrite, partly because the GUI approach is the one most people reach for first.

## What the GUI version did

AppleScript and the macOS accessibility API, driving OneNote for Mac directly:

1. Click **Add section**, select-all, type the notebook name, press Return.
2. For each note: convert its HTML to RTF with `textutil`, put that on the clipboard, click **Add page**, type the title, press Tab, press ⌘V.

It is genuinely impressive to watch. It is also a machine impersonating a human, and it inherits every weakness of being one.

## Why it failed

**The creation date is unreachable.** OneNote stamps every new page with today's date, and nothing in the app — no menu, no AppleScript property — lets you change it. For an archive of a decade of notes, this is the whole point of the migration, and the GUI cannot deliver it. The workaround was to write the real date into the body text as a line of prose. The API accepts `<meta name="created" content="...">` and sets the actual page date.

**The UI does not tell you when it is ready.** `create_section()` would click, then type the name — and sometimes the name landed, sometimes it arrived as "New Section 3" with the typed text lost, and once as a fragment of the intended name. Adding delays made it less frequent, not reliable. Every failed attempt left another wrongly-named section behind. One debugging session produced eleven of them, four of which quietly contained real notes filed under names like "New Section 6". The API creates a section with one call that either returns its id or an error.

**The accessibility tree is nearly empty.** OneNote for Mac exposes its toolbar buttons and the section list. The page title field and the body canvas expose nothing — probing for `AXTextField` and `AXTextArea` returns zero matches. So there is no way to confirm a title was typed into the right place, or typed at all. You are aiming keystrokes at a window and hoping.

**`perform action "AXPress"` creates the object but does not move keyboard focus.** Titles kept landing nowhere. Using System Events' plain `click` on the element does transfer focus; coordinate-based `click at {x, y}` fails with `osascript is not allowed assistive access (-25211)` even when accessibility permission is granted and the app has been restarted. Hours went into that distinction.

**Attachments cannot be carried through a clipboard.** Images can ride along inside RTF, but PDFs and Office documents cannot. They were extracted to a folder for the user to drag in by hand — several hundred manual operations.

**There is no such thing as resuming.** The script has no idea which notes already exist, because it cannot read the page list reliably. Interrupt it and the only safe options are to start over or to reconcile by hand.

**It needs the machine.** The app must stay focused, the screen awake, and nobody can touch the keyboard for the hours it takes.

## What changed with the API

Same goal, different surface. `POST /me/onenote/sections/{id}/pages` takes HTML, attachments as multipart data, and the creation date as a meta tag. It returns a page id.

That page id is the thing the GUI version could never have. It makes a ledger possible: record every uploaded note, and a re-run becomes a no-op for everything already done. It makes verification possible: fetch each page back and compare it to the source, note by note, instead of scrolling through OneNote looking for gaps. It makes failure recoverable: when a page is created but its attachments fail, the id is recorded too, so the retry deletes the half-built page before replacing it, and no duplicate appears.

| | GUI automation | Graph API |
|---|---|---|
| Original creation date | impossible | preserved |
| Images, attachments | images only, via clipboard | both, uploaded with the page |
| Section creation | type and hope | one call, returns an id |
| Confirming a title landed | not observable | in the response |
| Interrupted run | duplicates or manual repair | resume, no duplicates |
| Verification | open pages and look | automated, per note |
| 600 notes | hours, supervised | about an hour, unattended |

## The lesson

The GUI version was not a failure of effort — it was well-debugged, and each individual problem did get solved. It was a failure of surface. Automating a user interface means accepting every constraint the interface places on a human: no atomicity, no error values, no identifiers, no way to ask what is already there. Those constraints are invisible while you are demoing it and fatal when you are migrating an archive.

The API is less impressive to watch. Nothing moves on screen. It is simply correct.

If you are considering the GUI route because no API exists for your target application, the honest advice is: check again, and check whether the data you care about — timestamps especially — is even expressible through the interface you are planning to drive. If it is not, no amount of automation will recover it.
