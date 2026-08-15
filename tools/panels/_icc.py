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
CMYK_PROFILES = [
    ("Standard (generisch) — universell, ohne ICC-Datei",
        None,
        "Custom",
        "Generic CMYK, no characterised printing condition"),
    ("ISO Coated v2 (FOGRA39) — gestrichenes Papier, EU-Offset-Standard",
        ("ISOcoated_v2_eci.icc", "ISOcoated_v2_300_eci.icc"),
        "FOGRA39L",
        "Offset commercial and specialty printing according to "
        "ISO 12647-2:2004/Amd 1, paper type 1 or 2, coated"),
    ("PSO Coated v3 (FOGRA51) — modernes gestrichenes Papier, Premium-Offset",
        ("PSOcoated_v3.icc",),
        "FOGRA51L",
        "Offset printing according to ISO 12647-2:2013, "
        "premium coated paper"),
    ("PSO Uncoated v3 (FOGRA52) — ungestrichenes/Naturpapier, Bücher & Briefbögen",
        ("PSOuncoated_v3_FOGRA52.icc", "PSO_Uncoated_ISO12647_eci.icc"),
        "FOGRA52L",
        "Offset printing according to ISO 12647-2:2013, uncoated paper"),
    ("U.S. Web Coated (SWOP) v2 — US-Rollenoffset, Magazine (gestrichen)",
        ("USWebCoatedSWOP.icc",),
        "CGATS TR 001",
        "U.S. Web Coated (SWOP) publication printing"),
    ("Coated GRACoL 2006 — US-Bogenoffset, hochwertiges gestrichenes Papier",
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
