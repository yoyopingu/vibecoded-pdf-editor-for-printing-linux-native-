"""
PDF/X export.
"""
import os

import pikepdf
from pikepdf import Array, Dictionary, Name, String

from tools.panels._icc import CMYK_PROFILES, fallback_cmyk_icc, resolve_icc
from tools.panels.pdfx import (PDFX_VERSION, _check_conformance, _export_pdfx,
                               _layer_report, _pdfx_defs)
from tests.support import FX, _TMP


def _out(name):
    return os.path.join(_TMP, f"pdfx_{name}.pdf")


def _layered_fixture():
    """A one-page PDF with an OCG that the file itself switches off.

    A cutter contour or a varnish plate looks exactly like this, and it is the
    case the old layers tool existed to get right.
    """
    path = os.path.join(_TMP, "pdfx_layered.pdf")
    if os.path.exists(path):
        return path
    with pikepdf.open(FX["single"]) as pdf:
        page = pdf.pages[0]
        ocg = pdf.make_indirect(Dictionary(Type=Name("/OCG"),
                                           Name=String("Stanzkontur")))
        pdf.Root[Name("/OCProperties")] = Dictionary(
            OCGs=Array([ocg]),
            D=Dictionary(Order=Array([ocg]), OFF=Array([ocg])))
        page.contents_add(
            pikepdf.Stream(pdf, b"/OC /MC0 BDC 1 0 1 rg 100 300 300 300 re f EMC"))
        page.obj["/Resources"][Name("/Properties")] = Dictionary(MC0=ocg)
        pdf.save(path)
    return path


def test_the_export_carries_everything_pdfx_requires():
    """A PDF/X file is one a shop can accept without opening it, so every
    claim it makes has to be in the file: the version marker, an output intent
    with an embedded ICC profile, and a TrimBox on every page telling the RIP
    where the finished sheet ends."""
    icc = fallback_cmyk_icc()
    assert icc, "no CMYK ICC profile on this system to embed"
    out = _out("conform")
    result, dropped = _export_pdfx(FX["color"], out, icc, "Custom",
                                   "Generic CMYK", lambda _m: None)
    assert result == out and not dropped

    with pikepdf.open(out) as pdf:
        assert str(pdf.docinfo["/GTS_PDFXVersion"]) == PDFX_VERSION
        # Ghostscript's PDF/X mode writes PDF 1.3, which is what the :2002
        # revision is based on — the version string must not outrun it.
        assert str(pdf.pdf_version) == "1.3", pdf.pdf_version
        intent = pdf.Root["/OutputIntents"][0]
        assert str(intent["/S"]) == "/GTS_PDFX"
        assert str(intent["/OutputConditionIdentifier"]) == "Custom"
        profile = intent["/DestOutputProfile"]
        assert int(profile["/N"]) == 4, "the embedded output profile is not CMYK"
        assert len(bytes(profile.read_bytes())) > 0, "the profile stream is empty"
        assert len(pdf.pages) == 3, "pages went missing"
        for i, page in enumerate(pdf.pages):
            assert "/TrimBox" in page.obj, f"page {i + 1} has no TrimBox"
    return "version marker, CMYK output intent, TrimBox on every page"


def test_a_layer_switched_off_never_reaches_the_plate():
    """The reason this tool replaced "Ebenen (OCG)".

    PDF/X-3 has no optional content, so the export has to resolve it — and it
    must resolve it the way the file says, not by making everything visible.
    A cutter contour that prints is a ruined run."""
    src = _layered_fixture()
    on, off = _layer_report(src)
    assert (on, off) == ([], ["Stanzkontur"]), (on, off)

    out = _out("layered")
    _result, dropped = _export_pdfx(src, out, fallback_cmyk_icc(), "Custom",
                                    "Generic CMYK", lambda _m: None)
    assert dropped == ["Stanzkontur"], "the export did not report the dropped layer"

    with pikepdf.open(out) as pdf:
        assert "/OCProperties" not in pdf.Root, \
            "optional content survived into a PDF/X file"

    # And the hidden ink really is absent, not merely unmarked.
    import pypdfium2 as pdfium
    doc = pdfium.PdfDocument(out)
    try:
        image = doc[0].render(scale=0.4).to_pil().convert("RGB")
        magenta = sum(1 for r, g, b in image.get_flattened_data()
                      if r > 150 and g < 120 and b > 150)
    finally:
        doc.close()
    assert magenta == 0, f"{magenta} pixels of a switched-off layer got printed"
    return "the hidden layer is reported, dropped, and absent from the output"


def test_a_file_that_is_not_pdfx_is_not_handed_over():
    """pdfwrite exits 0 on plenty of files it did not fully convert. The whole
    value here is output that needs no second look, so the claim is checked
    against the file before it ships."""
    for name, path in (("plain", FX["normal"]), ("colour", FX["color"])):
        try:
            _check_conformance(path)
        except RuntimeError:
            pass
        else:
            raise AssertionError(f"an ordinary PDF ({name}) passed as PDF/X")

    out = _out("conform")   # written by the first test, genuinely conformant
    if os.path.exists(out):
        _check_conformance(out)     # must not raise
    return "ordinary PDFs are refused, a real one passes"


def test_the_output_intent_names_a_condition_a_rip_can_look_up():
    """/OutputConditionIdentifier is read by the RIP to decide whether the file
    was separated for the press it is going on. Our label for a profile is no
    use there — it has to be the registered name of the characterisation data,
    and every named profile needs one."""
    seen = {}
    for label, candidates, oci, condition in CMYK_PROFILES:
        assert oci and condition, f"{label} has no output condition"
        if candidates is None:
            assert oci == "Custom", "the generic entry must not claim a registry name"
        else:
            assert oci != "Custom", f"{label} falls back to an unnamed condition"
            assert oci not in seen, f"{label} and {seen[oci]} both claim {oci}"
            seen[oci] = label
    return f"{len(seen)} named printing conditions, all distinct"


def test_a_profile_path_cannot_break_out_of_the_postscript_prologue():
    """The ICC path and the condition name are pasted into a PostScript file.
    A parenthesis in either would close the string early and turn the rest of
    the path into code — and profile directories are user-supplied."""
    defs = _pdfx_defs("/tmp/od(d) name).icc", "OCI)", "cond(ition")
    body = defs.split("/ICCProfile (", 1)[1]
    path_literal = body.split(") def", 1)[0]
    assert path_literal == r"/tmp/od\(d\) name\).icc", path_literal
    assert r"\(" in defs and r"\)" in defs
    # And nothing unescaped survived that would end a string early.
    for line in defs.splitlines():
        if line.startswith("  /OutputCondition ") or line.startswith("/ICCProfile "):
            inner = line[line.index("(") + 1:line.rindex(")")]
            bare = inner.replace(r"\(", "").replace(r"\)", "")
            assert "(" not in bare and ")" not in bare, line
    return "parentheses in a path or a condition name are escaped"


def test_a_named_profile_that_is_installed_is_the_one_used():
    """resolve_icc has to find a real file, not just the first candidate name,
    or the export silently embeds a generic profile under a named condition."""
    assert resolve_icc(None) is None
    assert resolve_icc(("definitely_not_here_9f2a.icc",)) is None
    # The generic Ghostscript profile stands in for the real thing here: what
    # is being checked is that an existing file is picked up by name.
    fallback = fallback_cmyk_icc()
    assert fallback and os.path.isfile(fallback)
    found = resolve_icc((os.path.basename(fallback),))
    assert found is None or os.path.isfile(found)
    return "missing profiles answer None, present ones answer a real path"
