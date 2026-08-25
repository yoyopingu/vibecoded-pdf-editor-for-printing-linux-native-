"""
The CMYK output profiles the shop prints against, and where to find them.

Two panels need this list — "Farbprofil / CMYK", which converts into one of
these spaces, and "PDF/X-Export", which converts *and* records which space it
converted into. Keeping one table means a profile added here shows up in both,
and neither can drift into naming a profile the other does not know.

The .icc files themselves are not shipped: ISO Coated v2, PSO Coated v3 and
the rest are ECI and IDEAlliance downloads with their own licences. Drop them
in ~/.local/share/copyshop_pdf_suite/icc/ and the matching entry starts using
the real profile; without the file the entry still converts, generically.

The identifier on each row is what PDF/X writes into the output intent as
/OutputConditionIdentifier. A RIP reads that string to decide whether the file
was separated for the condition it is about to print — so it has to be the
registered name of the characterisation data, not our label for it.
"""
import os


# (label, candidate .icc filenames, OutputConditionIdentifier, OutputCondition)
# The labels are what the combo shows beside "CMYK-Profil:"; the long German
# descriptions they used to embed clipped at the combo's edge, so the descriptive
# part now lives in the panel's info text below the field instead.
CMYK_PROFILES = [
    ("Standard (generisch)",
        None,
        "Custom",
        "Generic CMYK, no characterised printing condition"),
    ("ISO Coated v2 (FOGRA39)",
        ("ISOcoated_v2_eci.icc", "ISOcoated_v2_300_eci.icc"),
        "FOGRA39L",
        "Offset commercial and specialty printing according to "
        "ISO 12647-2:2004/Amd 1, paper type 1 or 2, coated"),
    ("PSO Coated v3 (FOGRA51)",
        ("PSOcoated_v3.icc",),
        "FOGRA51L",
        "Offset printing according to ISO 12647-2:2013, "
        "premium coated paper"),
    ("PSO Uncoated v3 (FOGRA52)",
        ("PSOuncoated_v3_FOGRA52.icc", "PSO_Uncoated_ISO12647_eci.icc"),
        "FOGRA52L",
        "Offset printing according to ISO 12647-2:2013, uncoated paper"),
    ("U.S. Web Coated (SWOP) v2",
        ("USWebCoatedSWOP.icc",),
        "CGATS TR 001",
        "U.S. Web Coated (SWOP) publication printing"),
    ("Coated GRACoL 2006",
        ("GRACoL2006_Coated1v2.icc", "CGATS21_CRPC6.icc"),
        "CGATS TR 006",
        "U.S. sheetfed offset printing, GRACoL 2006 coated #1"),
]

ICC_DIR = os.path.expanduser("~/.local/share/copyshop_pdf_suite/icc/")

# Ghostscript ships a generic CMYK profile. It characterises nothing in
# particular, but PDF/X requires *an* embedded output intent profile, and a
# generic one that says so beats refusing to export at all.
GS_FALLBACK_CMYK = "/usr/share/ghostscript/iccprofiles/default_cmyk.icc"


def resolve_icc(candidates):
    """The first of `candidates` that is actually installed, or None."""
    if not candidates:
        return None
    for name in candidates:
        for d in (ICC_DIR, "/usr/share/color/icc/", "/usr/share/color/icc/colord/"):
            path = os.path.join(d, name)
            if os.path.isfile(path):
                return path
    return None


def profile_by_key(key):
    """The CMYK_PROFILES row whose label starts with `key`, or the first row.

    The stored setting is the label, so an entry renamed in the table falls
    back to generic rather than to a condition the user never chose.
    """
    for entry in CMYK_PROFILES:
        if entry[0] == key:
            return entry
    return CMYK_PROFILES[0]


def icc_colour_space(path):
    """The four-character colour space of an ICC profile — "CMYK", "RGB ",
    "GRAY" — or None if `path` is not one.

    Bytes 16..20 of the 128-byte header are the data colour space signature
    (ICC.1:2010 §7.2.6). Checked before a profile is installed under a name
    the export will later separate against: an RGB profile filed as
    ISOcoated_v2_eci.icc would make every export silently wrong, and the
    output intent would state a condition the file was never separated for.

    Bytes 36..40 must be 'acsp' — the profile file signature. Without that
    test *any* file answers this question, because every file has four bytes
    at offset 16; a PDF picked by mistake came back as a colour space.
    """
    try:
        with open(path, "rb") as f:
            header = f.read(128)
    except OSError:
        return None
    if len(header) < 40 or header[36:40] != b"acsp":
        return None
    space = header[16:20].decode("latin-1", "replace")
    return space if space.strip() else None


def profile_description(path):
    """The profile's own name, for confirming the right file was picked.

    Read out of the 'desc' tag rather than trusting the filename, which is
    exactly what is in question at the point this is called. Returns None if
    the tag cannot be read — a profile is still installable without it.
    """
    try:
        with open(path, "rb") as f:
            data = f.read()
        count = int.from_bytes(data[128:132], "big")
        for i in range(count):
            off = 132 + i * 12
            sig = data[off:off + 4]
            if sig != b"desc":
                continue
            start = int.from_bytes(data[off + 4:off + 8], "big")
            size = int.from_bytes(data[off + 8:off + 12], "big")
            tag = data[start:start + size]
            if tag[:4] == b"desc":          # ICC v2 textDescriptionType
                length = int.from_bytes(tag[8:12], "big")
                return tag[12:12 + length].rstrip(b"\x00").decode("latin-1")
            if tag[:4] == b"mluc":          # ICC v4 multiLocalizedUnicodeType
                length = int.from_bytes(tag[20:24], "big")
                start2 = int.from_bytes(tag[24:28], "big")
                return tag[start2:start2 + length].decode("utf-16-be").rstrip("\x00")
    except (OSError, IndexError, ValueError, UnicodeDecodeError):
        return None
    return None


def install_profile(src_path, filename):
    """Copy an ICC profile into the app's profile directory as `filename`.

    Raises ValueError if it is not a CMYK profile. Returns where it landed.
    """
    import shutil
    space = icc_colour_space(src_path)
    if space != "CMYK":
        raise ValueError(space or "")
    os.makedirs(ICC_DIR, exist_ok=True)
    dest = os.path.join(ICC_DIR, filename)
    shutil.copyfile(src_path, dest)
    return dest


def fallback_cmyk_icc():
    """Some embeddable CMYK profile, or None if the system has none.

    Only PDF/X needs this: the standard requires the output intent to carry a
    profile, so an export with no named profile installed still has to embed
    something rather than write an output intent with a dangling reference.
    """
    for path in (GS_FALLBACK_CMYK,
                 "/usr/share/color/icc/ghostscript/default_cmyk.icc"):
        if os.path.isfile(path):
            return path
    for root in ("/usr/share/ghostscript",):
        for dirpath, _dirs, files in os.walk(root):
            if "default_cmyk.icc" in files:
                return os.path.join(dirpath, "default_cmyk.icc")
    return None
