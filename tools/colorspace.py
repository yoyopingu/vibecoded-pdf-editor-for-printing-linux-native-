"""
Which colour spaces a PDF page actually uses.

Read from the file's structure rather than from its pixels: the declared spaces
in /Resources, the spaces of any images, and the colour operators in the content
streams. Pixels answer a different question — how far from grey this page looks —
and that is tools/panels/_colour.py.

There were three copies of this, in the viewer's colour label, the Farbprofil
tool and the greyscale scan, and they did not agree:

* Only one recursed into Form XObjects. Imposition, N-Up and merge all turn a
  page into a form, after which the page's own content stream is just "/Fm0 Do"
  and nothing is found in it at all.
* Only one scanned the content stream unconditionally. The Farbprofil tool
  stopped as soon as /Resources yielded anything, so a page with an RGB image
  and a CMYK vector fill was reported as RGB — the wrong answer, in the tool
  whose job is that answer.
* Only one had a depth and cycle guard, so a form that refers to itself was a
  hang rather than a result.

This is the careful version of the three. Callers classify the result for
themselves: the viewer wants a label, the greyscale scan wants to know whether a
page is grey already, the Farbprofil tool wants the names.
"""

import logging
import re

from tools.render.document_cache import _stat_key

# Colour operators in a content stream: `1 0 0 rg`, `0 0 0 1 k`, `0.5 g`, and
# their stroke-side capitals.
#
# Found with bytes.find, not with a regex, and by looking for the operator
# rather than for operands-then-operator. Both matter, and both were measured
# against one 53 MB page:
#
#   `[\d.]+\s+` four times over then [kK]     15,300 ms   (backtracks; the page
#                                                          contains no CMYK)
#   (?<![A-Za-z0-9])[kK](?![A-Za-z0-9])        2,900 ms
#   data.find(b"k") in a loop                      7 ms
#
# The operand count then runs against 64 bytes instead of 53 million, so the
# whole scan is bounded by how fast the file can be read.
_RE_OPERANDS = re.compile(rb'(?:[-+]?[0-9.]+[ \t\r\n]+){1,5}\Z')
_OPERANDS_WINDOW = 64          # more than enough for four numbers and spacing

# A PDF operator is delimited: `rg` in `/Xrg` or `rgb` is not the operator.
_WORD = frozenset(b"ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789")

_FAMILIES = (
    ((b"rg", b"RG"), 3, "/DeviceRGB"),
    ((b"k",  b"K"),  4, "/DeviceCMYK"),
    ((b"g",  b"G"),  1, "/DeviceGray"),
)


def _delimited(data, pos, length):
    before = data[pos - 1] if pos else None
    after = data[pos + length] if pos + length < len(data) else None
    return before not in _WORD and after not in _WORD


def _uses(data, tokens, operands):
    """Does `data` apply one of `tokens` to at least `operands` numbers?"""
    for token in tokens:
        pos = data.find(token)
        while pos != -1:
            if _delimited(data, pos, len(token)):
                head = data[max(0, pos - _OPERANDS_WINDOW):pos]
                m = _RE_OPERANDS.search(head)
                if m and len(m.group(0).split()) >= operands:
                    return True
            pos = data.find(token, pos + 1)
    return False


def colour_operators(data):
    """The colour spaces named by the operators in one content stream.

    `data` is bytes: these are all ASCII operators, and decoding 53 MB to str
    first costs 67 ms and buys nothing.
    """
    return {name for tokens, operands, name in _FAMILIES
            if _uses(data, tokens, operands)}


_MAX_FORM_DEPTH = 8

# (file revision, page index) -> frozenset of names. Keyed on the revision the
# document cache uses, so a file rewritten in place — which the page manager does
# constantly — is never answered from a stale entry.
_cache: "dict" = {}
_CACHE_MAX = 512     # pages; a frozenset of a few short strings each


def _revision(path):
    return _stat_key(path) or (path, None, None)


def _content_bytes(obj, pikepdf):
    """The content of a page or of a Form XObject.

    A page keeps its stream (or streams) in /Contents; a form *is* the stream,
    so it is read directly. Missing that second case is what makes a nested page
    look empty.
    """
    if "/Contents" in obj:
        contents = obj.get("/Contents")
        data = b""
        if isinstance(contents, pikepdf.Array):
            for c in contents:
                try:
                    data += bytes(c.read_bytes())
                except Exception:
                    logging.debug("colorspace: unreadable content stream", exc_info=True)
        elif contents is not None:
            try:
                data = bytes(contents.read_bytes())
            except Exception:
                logging.debug("colorspace: unreadable content stream", exc_info=True)
        return data
    try:
        return bytes(obj.read_bytes())
    except Exception:
        return b""


def _scan(obj, found, pikepdf, depth=0, seen=None):
    """Collect colour-space names from `obj` and anything it draws."""
    if depth > _MAX_FORM_DEPTH:
        return
    seen = set() if seen is None else seen

    def _name_of(cs):
        return str(cs[0]) if isinstance(cs, pikepdf.Array) else str(cs)

    res = obj.get("/Resources")
    if res is not None:
        cs_dict = res.get("/ColorSpace")
        if isinstance(cs_dict, pikepdf.Dictionary):
            for v in cs_dict.values():
                try:
                    found.add(_name_of(v))
                except Exception:
                    logging.debug("colorspace: bad /ColorSpace entry", exc_info=True)
        xobj = res.get("/XObject")
        if isinstance(xobj, pikepdf.Dictionary):
            for v in xobj.values():
                try:
                    subtype = v.get("/Subtype")
                    if subtype == pikepdf.Name("/Image"):
                        cs = v.get("/ColorSpace")
                        if cs is not None:
                            found.add(_name_of(cs))
                    elif subtype == pikepdf.Name("/Form"):
                        try:
                            key = v.objgen
                        except Exception:
                            key = id(v)
                        if key not in seen:      # a form may refer to itself
                            seen.add(key)
                            _scan(v, found, pikepdf, depth + 1, seen)
                except Exception:
                    logging.debug("colorspace: bad /XObject entry", exc_info=True)

    # Always, not only when the resources gave nothing: vector fills live here
    # and nowhere else.
    try:
        found |= colour_operators(_content_bytes(obj, pikepdf))
    except Exception:
        logging.debug("colorspace: content scan failed", exc_info=True)


def _page_names(page, pikepdf):
    found = set()
    _scan(page, found, pikepdf)
    return frozenset(found)


def _remember(key, names):
    """Cache `names`, dropping the oldest entries once the cap is reached.

    Not _cache.clear(): a document with more pages than the cap would wipe
    everything it had just learned each time it filled up, so scanning a
    300-page file left only its last few pages cached and the viewer went back
    to reading page 1 from disk. Insertion order is good enough to decide what
    to drop — these are read in page order.
    """
    _cache[key] = names
    while len(_cache) > _CACHE_MAX:
        _cache.pop(next(iter(_cache)))
    return names


def page_colorspaces(pdf_path, page_index):
    """The colour-space names one page uses, as a frozenset. Empty if it could
    not be read — callers must treat that as "unknown", never as "grey"."""
    key = (_revision(pdf_path), page_index)
    hit = _cache.get(key)
    if hit is not None:
        return hit
    try:
        import pikepdf
        with pikepdf.open(pdf_path) as pdf:
            return _remember(key, _page_names(pdf.pages[page_index], pikepdf))
    except Exception:
        logging.debug("colorspace: %s page %s unreadable", pdf_path, page_index,
                      exc_info=True)
        return frozenset()


def cached_page_colorspaces(pdf_path, page_index):
    """:func:`page_colorspaces` if it has already been read, else None.

    For the GUI thread, which must not be the one to find out: reading means
    walking every content stream on the page, which is half a second on a large
    one and unbounded in principle.
    """
    return _cache.get((_revision(pdf_path), page_index))


def document_colorspaces(pdf_path):
    """Every colour-space name used anywhere in the document.

    One open for the whole file. Asking page_colorspaces in a loop meant a
    pikepdf.open per page — 500 opens of the same file for a 500-page document,
    where the answer for every page is behind the one handle already in hand.
    Each page is put in the cache on the way past, so the viewer's label and the
    greyscale scan get theirs for nothing afterwards.
    """
    revision = _revision(pdf_path)
    found = set()
    try:
        import pikepdf
        with pikepdf.open(pdf_path) as pdf:
            for i, page in enumerate(pdf.pages):
                key = (revision, i)
                names = _cache.get(key)
                if names is None:
                    names = _remember(key, _page_names(page, pikepdf))
                found |= names
    except Exception:
        logging.debug("colorspace: %s unreadable", pdf_path, exc_info=True)
        return frozenset()
    return frozenset(found)


RGB_NAMES  = frozenset({"/DeviceRGB", "/CalRGB", "/ICCBased"})
GRAY_NAMES = frozenset({"/DeviceGray", "/CalGray"})


def has_rgb(names):
    return bool(set(names) & RGB_NAMES)


def has_cmyk(names):
    return "/DeviceCMYK" in names


def has_gray(names):
    return bool(set(names) & GRAY_NAMES)


def is_grey_only(names):
    """Grey and nothing else. False for an empty set: nothing found means the
    page could not be read, and treating that as grey would skip converting it."""
    return bool(names) and has_gray(names) and not (has_rgb(names) or has_cmyk(names))


def describe(names):
    """The one-line answer the viewer puts under the page."""
    if has_rgb(names) and has_cmyk(names):
        return "RGB + CMYK"
    if has_cmyk(names):
        return "CMYK"
    if has_rgb(names):
        return "RGB"
    if has_gray(names):
        return "Grayscale"
    return "—"
