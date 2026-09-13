"""ENML (Evernote note markup) -> OneNote-API input XHTML.

The OneNote API accepts a small subset of HTML and silently drops the rest,
so this reduces each note to that subset rather than passing the original
web-clip markup through. Inline CSS in particular is mostly noise here: a
single clipped Reddit page carries hundreds of kilobytes of layout styling
(including base64 `url(...)` sprites) that OneNote would discard anyway.

See: https://learn.microsoft.com/en-us/graph/onenote-create-page
"""
import re
from lxml import etree, html as lhtml

# Tags kept as-is. Anything else is unwrapped (children survive) unless it is
# in DROP_TAGS, which is dropped along with its subtree.
KEEP_TAGS = {
    "a", "b", "strong", "i", "em", "u", "s", "strike", "del", "ins", "sub", "sup",
    "br", "hr", "p", "div", "span", "h1", "h2", "h3", "h4", "h5", "h6",
    "ul", "ol", "li", "table", "thead", "tbody", "tfoot", "tr", "td", "th",
    "pre", "code", "blockquote", "img", "center", "caption", "dl", "dt", "dd",
}
DROP_TAGS = {
    "script", "style", "link", "meta", "noscript", "iframe", "form", "input",
    "button", "select", "textarea", "svg", "canvas", "audio", "video", "object",
    "embed", "applet", "map", "area", "en-crypt", "head", "base", "title",
}
ATTRS = {
    "a": {"href"},
    "img": {"src", "alt", "width", "height"},
    "td": {"colspan", "rowspan"},
    "th": {"colspan", "rowspan"},
}
# OneNote honours a handful of inline properties; the rest is dead weight.
STYLE_PROPS = {"font-weight", "font-style", "text-decoration", "color", "background-color"}

VOID = {"br", "hr", "img"}


def _clean_style(value):
    # Clip markup often repeats the same property several times; last one wins,
    # which is what a browser would have applied.
    seen = {}
    for decl in value.split(";"):
        if ":" not in decl:
            continue
        prop, _, val = decl.partition(":")
        prop = prop.strip().lower()
        val = val.strip()
        if prop in STYLE_PROPS and "url(" not in val.lower() and val:
            seen[prop] = val
    return ";".join(f"{k}:{v}" for k, v in seen.items())


def _unwrap(el):
    """Replace el with its children, preserving text."""
    parent = el.getparent()
    if parent is None:
        return
    idx = parent.index(el)
    prev = el.getprevious()

    def push_text(text):
        if not text:
            return
        if len(el) and el[-1] is not None:
            pass
        target = prev if prev is not None else parent
        if target is parent:
            parent.text = (parent.text or "") + text
        else:
            target.tail = (target.tail or "") + text

    push_text(el.text)
    for i, child in enumerate(list(el)):
        parent.insert(idx + i, child)
        prev = child
    push_text(el.tail)
    parent.remove(el)


def sanitize(enml, media_resolver, note_id):
    """Return (body_xhtml_string, used_part_names).

    media_resolver(hash, mime) -> part name to reference, or None to drop.
    """
    # Strip the XML/DOCTYPE preamble and unwrap <en-note>.
    inner = re.sub(r"^\s*<\?xml[^>]*\?>", "", enml).strip()
    inner = re.sub(r"<!DOCTYPE[^>]*>", "", inner, flags=re.I).strip()

    try:
        root = lhtml.fragment_fromstring(inner, create_parent="div")
    except Exception:
        root = lhtml.fromstring("<div>" + inner + "</div>")

    used = []

    # en-media -> img/object placeholders, resolved by the caller.
    for el in root.iter():
        pass  # tree is mutated below; iterate over snapshots instead

    for el in list(root.iter()):
        tag = el.tag
        if not isinstance(tag, str):  # comments, PIs
            _drop(el)
            continue
        tag = tag.lower()

        if tag == "en-media":
            part = media_resolver(el.get("hash"), el.get("type"))
            if part is None:
                _drop(el)
            else:
                used.append(part)
                _replace_with_media(el, part)
            continue

        if tag == "en-todo":
            mark = "☑ " if (el.get("checked") or "").lower() == "true" else "☐ "
            _replace_with_text(el, mark)
            continue

        if tag in DROP_TAGS:
            _drop(el)
            continue

        if tag not in KEEP_TAGS:
            _unwrap(el)
            continue

        allowed = ATTRS.get(tag, set())
        for name in list(el.attrib):
            low = name.lower()
            if low in allowed:
                continue
            if low == "style":
                cleaned = _clean_style(el.get(name) or "")
                del el.attrib[name]
                if cleaned:
                    el.set("style", cleaned)
                continue
            del el.attrib[name]

        if tag == "a":
            href = el.get("href") or ""
            if not href.lower().startswith(("http://", "https://", "mailto:")):
                el.attrib.pop("href", None)
            # OneNote styles links itself; the clipped colours are just noise.
            el.attrib.pop("style", None)
        if tag == "img":
            src = el.get("src") or ""
            # Inline data: images bloat the request; only URL or name: refs survive.
            if not src.lower().startswith(("http://", "https://", "name:")):
                _drop(el)
                continue

    _collapse_empties(root)
    return _serialize_children(root), used


def _drop(el):
    parent = el.getparent()
    if parent is None:
        return
    tail = el.tail
    if tail:
        prev = el.getprevious()
        if prev is not None:
            prev.tail = (prev.tail or "") + tail
        else:
            parent.text = (parent.text or "") + tail
    parent.remove(el)


def _replace_with_text(el, text):
    prev = el.getprevious()
    parent = el.getparent()
    combined = text + (el.tail or "")
    if prev is not None:
        prev.tail = (prev.tail or "") + combined
    else:
        parent.text = (parent.text or "") + combined
    parent.remove(el)


def _replace_with_media(el, part):
    kind, name, mime, filename = part
    if kind == "img":
        new = etree.Element("img")
        new.set("src", f"name:{name}")
    else:
        new = etree.Element("object")
        new.set("data-attachment", filename)
        new.set("data", f"name:{name}")
        new.set("type", mime)
    new.tail = el.tail
    el.getparent().replace(el, new)


EMPTYABLE = {"div", "span", "p", "font", "center", "a",
             "h1", "h2", "h3", "h4", "h5", "h6", "li", "b", "i", "strong", "em"}


def _collapse_empties(root):
    """Drop wrappers that ended up with no text and no children."""
    for _ in range(6):  # a few passes flatten deeply nested clip markup
        removed = False
        for el in list(root.iter()):
            if el is root or not isinstance(el.tag, str):
                continue
            if el.tag in EMPTYABLE:
                if len(el) == 0 and not (el.text or "").strip():
                    _drop(el)
                    removed = True
                elif el.tag in ("div", "span") and len(el) == 1 and not (el.text or "").strip() \
                        and not (el[0].tail or "").strip() and not el.attrib:
                    _unwrap(el)
                    removed = True
        if not removed:
            break


def _serialize_children(root):
    out = []
    if root.text and root.text.strip():
        out.append(_escape(root.text))
    for child in root:
        out.append(etree.tostring(child, method="xml", encoding="unicode"))
    body = "".join(out)
    # lxml emits <br></br> style output for XHTML voids in some paths; normalise.
    for tag in VOID:
        body = re.sub(rf"<{tag}([^>]*?)/?></{tag}>", rf"<{tag}\1/>", body)
    # Graph requires a matching close tag on every container, so an lxml
    # self-closed <a/> or <div/> has to be expanded.
    body = re.sub(
        r"<(?!(?:%s)\b)([a-zA-Z][a-zA-Z0-9]*)((?:\s[^<>]*?)?)/>" % "|".join(VOID),
        r"<\1\2></\1>", body)
    return body


def _escape(text):
    return (text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;"))
