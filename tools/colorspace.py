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
# their stroke-side capitals. The grey pattern has to reject the last number of
# an rg/k triple or quad, hence the leading guard.
_RE_RGB  = re.compile(r'[\d.]+\s+[\d.]+\s+[\d.]+\s+(?:rg|RG)\b')
_RE_CMYK = re.compile(r'[\d.]+\s+[\d.]+\s+[\d.]+\s+[\d.]+\s+[kK]\b')
_RE_GRAY = re.compile(r'(?:^|[^\d.])[\d.]+\s+[gG]\b')

_MAX_FORM_DEPTH = 8

# (file revision, page index) -> frozenset of names. Keyed on the revision the
# document cache uses, so a file rewritten in place — which the page manager does
# constantly — is never answered from a stale entry.
_cache: "dict" = {}
_CACHE_MAX = 256


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
        text = _content_bytes(obj, pikepdf).decode("latin-1", errors="replace")
        if _RE_RGB.search(text):
            found.add("/DeviceRGB")
        if _RE_CMYK.search(text):
            found.add("/DeviceCMYK")
        if _RE_GRAY.search(text):
            found.add("/DeviceGray")
    except Exception:
        logging.debug("colorspace: content scan failed", exc_info=True)


def page_colorspaces(pdf_path, page_index):
    """The colour-space names one page uses, as a frozenset. Empty if it could
    not be read — callers must treat that as "unknown", never as "grey"."""
    key = (_revision(pdf_path), page_index)
    hit = _cache.get(key)
    if hit is not None:
        return hit
    found = set()
    try:
        import pikepdf
        with pikepdf.open(pdf_path) as pdf:
            _scan(pdf.pages[page_index], found, pikepdf)
    except Exception:
        logging.debug("colorspace: %s page %s unreadable", pdf_path, page_index,
                      exc_info=True)
        return frozenset()
    result = frozenset(found)
    if len(_cache) >= _CACHE_MAX:
        _cache.clear()
    _cache[key] = result
    return result


def document_colorspaces(pdf_path):
    """Every colour-space name used anywhere in the document."""
    found = set()
    try:
        import pikepdf
        with pikepdf.open(pdf_path) as pdf:
            n = len(pdf.pages)
    except Exception:
        logging.debug("colorspace: %s unreadable", pdf_path, exc_info=True)
        return frozenset()
    for i in range(n):
        found |= page_colorspaces(pdf_path, i)
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
