"""Printing: spool behavior."""
import os, shutil
from pypdf import PdfReader
from reportlab.pdfgen import canvas
from reportlab.lib.pagesizes import A4
import pypdfium2 as pdfium
from tools.jobs import null_progress
from tools.panels._colour import _colour_histogram, _hist_stats
from tests.support import FX, _TMP, _app, _open, _page_labels, _spin
from tests.printing_support import _print_dialog, _lp_options, _stub_queue


def test_unticking_two_sided_says_one_sided():
    """Saying nothing is not the same as saying no.

    With the box unticked the job carried no sides option at all, so the
    queue's own default decided — and a queue defaulted to duplex, which is the
    usual office setting, printed both sides anyway. Unticking the box did
    nothing at all.

    Both spellings, as when it is on: the IPP attribute for driverless queues
    and the PPD keyword for driver ones. GTK's print dialog and Qt's CUPS
    plugin both send one-sided explicitly."""
    off = _lp_options(duplex=False)
    assert "sides=one-sided" in off, off
    assert "Duplex=None" in off, off

    long_edge = _lp_options(duplex=True, edge="long")
    assert "sides=two-sided-long-edge" in long_edge, long_edge
    assert "Duplex=DuplexNoTumble" in long_edge, long_edge

    short_edge = _lp_options(duplex=True, edge="short")
    assert "sides=two-sided-short-edge" in short_edge, short_edge
    assert "Duplex=DuplexTumble" in short_edge, short_edge


def _scaling_sent(**kw):
    """Just the options that decide whether the printer may resize the page."""
    return [o for o in _lp_options(duplex=False, **kw)
            if "scaling" in o or "fit" in o]


def test_fit_to_page_actually_asks_for_a_fit():
    """The bug this test exists for: "An Seite anpassen" did nothing.

    The job carried `fit-to-page` and, from an unrelated branch further down,
    `print-scaling=none` as well. CUPS settles that pair by not scaling —
    measured against pdftopdf, an A6 page on A4 media came back at 1.000
    instead of the 1.835 the fit called for — so the page printed at its own
    size and the option looked broken because it was.

    Nothing that cancels a fit may be sent alongside one.
    """
    sent = _scaling_sent(scale_idx=0)
    assert "print-scaling=none" not in sent, \
        f"the fit is cancelled by a no-scaling option: {sent}"
    assert any(o in sent for o in ("print-scaling=fit", "fit-to-page")), sent
    return f"fit asks for a fit: {sent}"


def test_shrink_never_asks_for_something_that_enlarges():
    """"Auf bedruckbaren Bereich verkleinern" promises it never enlarges.

    `fit-to-page` scales up as readily as down (measured: an A6 page came back
    at 1.835), so it is the wrong request for this mode however convenient it
    looks. `print-scaling=auto-fit` is the one that shrinks an oversized page
    and leaves a small one alone (measured: A3 -> 0.647, A6 -> 1.000).
    """
    sent = _scaling_sent(scale_idx=2)
    assert "fit-to-page" not in sent, f"shrink asked for an enlarging fit: {sent}"
    assert "print-scaling=auto-fit" in sent, sent
    return f"shrink asks only for a shrink: {sent}"


def test_a_fixed_size_forbids_the_printer_from_rescaling():
    """Feste Groesse means the size in the box, at every percentage.

    At anything but 100 % the content is scaled here and centred on the sheet,
    and the job used to carry no scaling option at all after that — leaving
    CUPS's own default (print-scaling=auto) to decide whether to size it
    again. Saying none is what makes the percentage mean what it says.
    """
    for pct in (100, 70, 150):
        sent = _scaling_sent(scale_idx=1, scale_pct=pct)
        assert sent == ["print-scaling=none"], f"{pct}%: {sent}"
    return "100 %, 70 % and 150 % all say print-scaling=none"


def test_every_scaling_mode_sends_exactly_one_instruction():
    """Structural, because the bug was two instructions that disagreed.

    Whatever the mode, and whether or not Ghostscript is installed, the job
    must carry one coherent answer to "may the printer resize this" — and the
    same answer either way, since Ghostscript's absence changes who does the
    work, not what the user asked for.
    """
    from tools.printing import spool
    real = spool.ghostscript_binary
    seen = {}
    try:
        for gs, tag in ((real, "gs"), (lambda: None, "no gs")):
            spool.ghostscript_binary = gs
            for idx in (0, 1, 2):
                sent = _scaling_sent(scale_idx=idx)
                assert sent, f"{tag}, mode {idx}: no scaling instruction at all"
                assert not ("print-scaling=none" in sent and len(sent) > 1), \
                    f"{tag}, mode {idx}: contradictory pair {sent}"
                assert len(sent) == len(set(sent)), f"{tag}, mode {idx}: {sent}"
                seen.setdefault(idx, sent)
                assert seen[idx] == sent, \
                    f"mode {idx} differs with and without Ghostscript: {seen[idx]} vs {sent}"
    finally:
        spool.ghostscript_binary = real
    return "one instruction per mode, the same with or without Ghostscript"


def test_orientation_options_match_the_prepared_physical_page_axes():
    """Automatic turns landscape content into the stock's axes inside the PDF.

    The old job-wide flag left filters to rotate mixed pages, and then a
    request was sent telling the driver the page's axes. The C70 acted on that
    request — it is a rotating value — and turned an already-prepared portrait
    page sideways on its portrait sheet, while the near-identical C90 ignored
    it. So an automatic job must not ask the driver to rotate a page that
    already matches the stock: it says nothing, and the stock's own
    orientation stands. Only an explicit orientation (not baked in) still names
    its way up, and a landscape stock under an automatic job overrides with the
    page's own upright orientation.
    """
    import os
    from reportlab.lib.pagesizes import A4
    from reportlab.pdfgen import canvas
    land = os.path.join(_TMP, "print_landscape.pdf")
    if not os.path.exists(land):
        c = canvas.Canvas(land, pagesize=(A4[1], A4[0]))
        c.drawString(40, 100, "LANDSCAPE"); c.showPage(); c.save()

    def orient_for(src, orient_idx, paper_key="A4"):
        opts = _lp_options(duplex=False, src=src, orient_idx=orient_idx,
                           paper_key=paper_key)
        return [o for o in opts if o.startswith("orientation-requested")]

    # A portrait stock (A4) already matches the prepared portrait page, so the
    # job requests no orientation at all — the fix for the C70.
    assert orient_for(land, 0) == [], \
        "automatic rotation must not ask the driver to rotate the prepared page"
    assert orient_for(FX["normal"], 0) == [], \
        "a portrait document on a portrait stock names no orientation"
    # Explicit orientation is not baked in, so the driver is told which way up.
    assert orient_for(land, 2) == ["orientation-requested=2"]
    assert orient_for(land, 1) == ["orientation-requested=1"]
    # A landscape stock under an automatic job is the one case where the page
    # (prepared portrait) does not match the media, so it is overridden with
    # the page's own upright orientation.
    assert orient_for(land, 0, paper_key="Custom.420x297mm") == \
        ["orientation-requested=1"], \
        "a landscape stock under auto is overridden with the page's orientation"
    return "auto stays silent on matching stock; explicit and mismatching name it"


def test_the_chosen_paper_tray_reaches_lp():
    """Under the keyword the queue itself uses — InputSlot on a driver queue,
    media-source on a driverless one — and nothing at all when the user leaves
    it on the printer's default."""
    assert "InputSlot=Tray2" in _lp_options(duplex=False,
                                            paper_source=("InputSlot", "Tray2"))
    assert "media-source=tray-2" in _lp_options(
        duplex=False, paper_source=("media-source", "tray-2"))
    default = _lp_options(duplex=False, paper_source=None)
    assert not [o for o in default if "InputSlot" in o or "media-source" in o], default


def test_paper_sources_are_read_from_the_queue():
    """Parsed from `lpoptions -p NAME -l`, which is what CUPS documents as the
    way to ask. Both kinds of queue answer, in their own vocabulary."""
    import subprocess
    import tools.printing.spool as spool

    def with_output(text):
        real_run = subprocess.run

        class _R:
            stdout = text
        subprocess.run = lambda *a, **k: _R()
        try:
            return spool.printer_options("q"), spool.paper_sources("q")
        finally:
            subprocess.run = real_run

    opts, source = with_output(
        "PageSize/Media Size: *A4 Letter\n"
        "InputSlot/Media Source: Auto *Tray1 Tray2 Manual\n"
        "Duplex/2-Sided Printing: *None DuplexNoTumble DuplexTumble\n")
    assert source == ("InputSlot", ["Auto", "Tray1", "Tray2", "Manual"], "Tray1"), source
    assert "Duplex" in opts and opts["PageSize"][2] == "A4"

    _opts, source = with_output(
        "media-source/Media Source: *auto tray-1 tray-2\n")
    assert source == ("media-source", ["auto", "tray-1", "tray-2"], "auto"), source

    # A queue with one tray is not worth asking about.
    _opts, source = with_output("InputSlot/Media Source: *Auto\n")
    assert source is None, source




def test_queue_defaults_come_from_cups_not_qt():
    """The dialog opened on Letter and on two-sided for a queue set to A4 and
    one-sided in the desktop's printer settings.

    Both defaults were read from QPrinterInfo, and on a CUPS queue that is not
    reliable — the note on the duplex control already recorded
    defaultDuplexMode() reporting DuplexNone for printers that plainly duplex.
    The starred choice in `lpoptions -l` is what the queue is actually set to,
    and it is what the desktop writes when the user sets one there."""
    from tools.printing.spool import queue_defaults

    with _stub_queue("PageSize/Media Size: *A4 Letter Legal\n"
                     "Duplex/2-Sided Printing: *None DuplexNoTumble DuplexTumble\n"):
        assert queue_defaults("q") == {"paper": "A4", "duplex": False,
                                       "duplex_edge": "long"}

    with _stub_queue("PageSize/Media Size: A4 *Letter\n"
                     "Duplex/2-Sided Printing: None DuplexNoTumble *DuplexTumble\n"):
        assert queue_defaults("q") == {"paper": "Letter", "duplex": True,
                                       "duplex_edge": "short"}

    # A driverless queue says the same things in PWG/IPP vocabulary.
    with _stub_queue("media/Media Size: *iso_a4_210x297mm na_letter_8.5x11in\n"
                     "sides/2-Sided Printing: *one-sided two-sided-long-edge\n"):
        assert queue_defaults("q") == {"paper": "A4", "duplex": False,
                                       "duplex_edge": "long"}

    # A PPD variant is still that size, and a queue that says nothing is not
    # guessed at.
    with _stub_queue("PageSize/Media Size: *A4.Borderless A4\n"):
        assert queue_defaults("q") == {"paper": "A4"}
    with _stub_queue("ColorModel/Color Mode: *RGB Gray\n"):
        assert queue_defaults("q") == {"color_mode": "color"}


def test_bitmap_color_default_reads_the_cups_queue():
    """A Qt grayscale guess must not win over a CUPS colour default; a real
    monochrome default must still be respected by bitmap printing."""
    from tools.printing.spool import queue_defaults

    with _stub_queue("print-color-mode/Color Mode: monochrome *color\n"):
        assert queue_defaults("q")["color_mode"] == "color"
    with _stub_queue("print-color-mode/Color Mode: *monochrome color\n"):
        assert queue_defaults("q")["color_mode"] == "mono"
    with _stub_queue("ColorModel/Color Mode: *Gray RGB CMYK\n"):
        assert queue_defaults("q")["color_mode"] == "mono"
