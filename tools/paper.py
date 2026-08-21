"""
The paper sizes this application offers, in one place.

There were three lists. tools/panels/_shared.py had nine for the tools,
tools/printing/spool.py had its own for the spooler, and the print dialog had
a third to fall back on when a queue could not be asked. They disagreed: the
tools reached A0 but had never heard of SRA3, so a job could be printed on
SRA3 and not cropped to it. Adding a size meant finding all three and getting
the spelling right in each, which is why they drifted in the first place.

One list now, and it belongs to the operator rather than to the source: a shop
that runs a sheet nobody thought of adds it once, in Einstellungen, and every
dropdown in the application offers it. Built-in sizes can be hidden the same
way — a counter that never runs Legal should not have to read past it.

Sizes are stored and compared in points, which is the unit PDF itself uses;
millimetres are for the people reading them.
"""

import logging

from PyQt6.QtCore import QSettings

MM_TO_PT = 72.0 / 25.4

# The same store the rest of the application uses, reached directly rather
# than through tools.shell.settings. Going through the shell would make this
# low-level list depend on the window layer, and the graph says what that is:
# tools.shell.settings -> panels._prepress -> panels._shared -> tools.paper,
# a cycle. tools/printing/prefs.py keeps its own keys for the same reason.
_ORG, _APP = "CopyShop", "PDFSuite"

# Ordered as an operator thinks of them: the ISO series first, then the
# oversized stock that gets trimmed back, then the American sizes.
_BUILTIN = (
    ("A0",        2383.94, 3370.39),
    ("A1",        1683.78, 2383.94),
    ("A2",        1190.55, 1683.78),
    ("A3",         841.89, 1190.55),
    ("A4",         595.28,  841.89),
    ("A5",         419.53,  595.28),
    ("A6",         297.64,  419.53),
    ("SRA3",       907.09, 1275.59),
    ("SRA4",       637.80,  907.09),
    ("RA3",        864.57, 1218.90),
    ("RA4",        609.45,  864.57),
    ("B3",        1000.63, 1417.32),
    ("B4",         708.66, 1000.63),
    ("B5",         498.90,  708.66),
    ("Letter",     612.00,  792.00),
    ("Legal",      612.00, 1008.00),
    ("Tabloid",    792.00, 1224.00),
    ("Executive",  521.86,  756.00),
    ("Folio",      612.00,  936.00),
)

_CUSTOM_KEY = "paper/custom"     # "name:w_pt:h_pt" per entry
_HIDDEN_KEY = "paper/hidden"     # names of built-ins the shop does not run


def _settings():
    return QSettings(_ORG, _APP)


def _read_list(key):
    try:
        raw = _settings().value(key, [])
    except Exception:
        logging.debug("could not read %s", key, exc_info=True)
        return []
    if raw is None:
        return []
    if isinstance(raw, str):        # QSettings collapses a one-item list
        return [raw] if raw else []
    return [str(v) for v in raw]


def _write_list(key, values):
    try:
        _settings().setValue(key, list(values))
    except Exception:
        logging.debug("could not store %s", key, exc_info=True)


def builtin_names():
    """Every size that ships with the application, in order."""
    return [name for name, _w, _h in _BUILTIN]


def custom_sizes():
    """The shop's own sizes, as {name: (w_pt, h_pt)}."""
    out = {}
    for entry in _read_list(_CUSTOM_KEY):
        try:
            name, w, h = entry.rsplit(":", 2)
            out[name] = (float(w), float(h))
        except Exception:
            logging.debug("ignoring a malformed custom paper %r", entry)
    return out


def hidden_names():
    """Built-ins the operator has switched off."""
    return set(_read_list(_HIDDEN_KEY))


def sizes():
    """Every size to offer, as {name: (w_pt, h_pt)}, in display order.

    Hidden built-ins are left out; the shop's own sizes come after the ones
    that ship. This is what every dropdown in the application is built from.
    """
    hidden = hidden_names()
    out = {name: (w, h) for name, w, h in _BUILTIN if name not in hidden}
    out.update(custom_sizes())
    return out


def all_sizes():
    """Every size known, hidden ones included, as {name: (w_pt, h_pt)}.

    For resolving and for matching, never for offering. Hiding a size takes it
    off the dropdowns; it must not make a job that already names one, or a
    queue that reports one, suddenly unreadable.
    """
    out = {name: (w, h) for name, w, h in _BUILTIN}
    out.update(custom_sizes())
    return out


def size_pt(name):
    """(w_pt, h_pt) for a size, or None.

    Hidden sizes still resolve: a job saved with one, or a queue reporting
    one, should not stop working because the size was taken off a dropdown.
    """
    if not name:
        return None
    for n, w, h in _BUILTIN:
        if n == name:
            return (w, h)
    return custom_sizes().get(name)


def label(name):
    """"A4  (210x297mm)" — the name with the measurements after it.

    This exact spelling is what the tools' dropdowns have always shown, and
    what a saved choice and several tests name a size by. Prettier spacing is
    not worth a size that silently stops being findable.
    """
    size = size_pt(name)
    if not size:
        return name
    return (f"{name}  ({size[0] / MM_TO_PT:.0f}x"
            f"{size[1] / MM_TO_PT:.0f}mm)")


def add_custom(name, w_mm, h_mm):
    """Add or replace one of the shop's own sizes. Returns the name used."""
    name = (name or "").strip()
    if not name:
        raise ValueError("a paper size needs a name")
    if any(name == n for n, _w, _h in _BUILTIN):
        raise ValueError(f"{name} is already a built-in size")
    if not (1.0 <= w_mm <= 5000.0 and 1.0 <= h_mm <= 5000.0):
        raise ValueError("a sheet has to measure between 1 and 5000 mm")
    kept = [e for e in _read_list(_CUSTOM_KEY)
            if e.rsplit(":", 2)[0] != name]
    kept.append(f"{name}:{w_mm * MM_TO_PT:.4f}:{h_mm * MM_TO_PT:.4f}")
    _write_list(_CUSTOM_KEY, kept)
    return name


def remove_custom(name):
    _write_list(_CUSTOM_KEY,
                [e for e in _read_list(_CUSTOM_KEY)
                 if e.rsplit(":", 2)[0] != name])


def set_hidden(name, hidden):
    """Show or hide a built-in size."""
    current = hidden_names()
    if hidden:
        current.add(name)
    else:
        current.discard(name)
    _write_list(_HIDDEN_KEY, sorted(current))
