"""Printing: paper behavior."""
import os, shutil
from pypdf import PdfReader
from reportlab.pdfgen import canvas
from reportlab.lib.pagesizes import A4
import pypdfium2 as pdfium
from tools.jobs import null_progress
from tools.panels._colour import _colour_histogram, _hist_stats
from tests.support import FX, _TMP, _app, _open, _page_labels, _spin
from tests.printing_support import _print_dialog, _lp_options, _stub_queue


def _paper_dialog(queue="PageSize/Media Size: *A4 Letter A3\n"):
    """A dialog with its paper list already populated, the way the other queue
    tests in this file do it: stub the queue and drive the change directly,
    rather than spinning the event loop until the background enumeration
    happens to land."""
    tab, dlg = _print_dialog(2, "paper_dlg.pdf")
    with _stub_queue(queue):
        dlg.printer_combo.blockSignals(True)
        dlg.printer_combo.clear()
        dlg.printer_combo.addItem("office", "office")
        dlg.printer_combo.blockSignals(False)
        dlg._on_printer_changed()
        _spin(40, 0.0)
    return tab, dlg


def test_paper_defaults_to_leaving_the_printer_alone():
    """A size was named on every job, and there was no way not to.

    On a press with SRA3 loaded that meant "media=A4" went out with the job
    and an A4-sized area came back printed on a 320x450 sheet. Naming a size
    can only agree with what is loaded or override it wrongly; the operator
    has already loaded the stock and chosen the tray. The colour control has
    had "Drucker-Standard" all along — the paper had no equivalent.

    The custom entry is checked here too: a driverless press reports no sizes
    to enumerate, so a list, however long, cannot name the sheet it is running.
    """
    from tools.printing import prefs
    prefs.forget()
    tab, dlg = _paper_dialog()
    try:
        assert dlg.paper_combo.count(), "the paper list never populated"
        assert dlg.paper_combo.itemData(0) == "", \
            "leaving the paper alone is not the first choice"
        assert dlg.selected_paper() == "", \
            f"a size is chosen by default: {dlg.selected_paper()!r}"

        idx = dlg.paper_combo.findData(dlg.PAPER_CUSTOM)
        assert idx >= 0, "there is no way to type a size"
        assert not dlg.paper_w_mm.isVisibleTo(dlg), \
            "the millimetre boxes are shown when they mean nothing"
        dlg.paper_combo.setCurrentIndex(idx); _app.processEvents()
        assert dlg.paper_w_mm.isVisibleTo(dlg), "picking Custom showed no boxes"
        dlg.paper_w_mm.setValue(320); dlg.paper_h_mm.setValue(450)
        assert dlg.selected_paper() == "Custom.320x450mm", dlg.selected_paper()
    finally:
        prefs.forget()
        dlg.close(); dlg.deleteLater(); tab.deleteLater(); _app.processEvents()
    return "opens on 'leave the paper to the printer', and a size can be typed"


def test_no_media_option_is_sent_when_the_paper_is_left_alone():
    """The whole point of the default: the job must carry nothing about paper,
    so the queue's own setting stands. An orientation worked out from a sheet
    nobody chose would override it just as effectively, so that goes too."""
    opts = _lp_options(paper_key="")
    flat = " ".join(opts)
    assert "media=" not in flat, f"paper was named anyway: {opts}"
    assert "orientation-requested" not in flat, \
        f"an orientation for an unknown sheet was sent: {opts}"

    # Naming one still works, and reaches lp verbatim.
    opts = _lp_options(paper_key="SRA3")
    assert any(o == "media=SRA3" for o in opts), f"SRA3 did not reach lp: {opts}"
    opts = _lp_options(paper_key="Custom.320x450mm")
    assert any(o == "media=Custom.320x450mm" for o in opts), \
        f"a typed size did not reach lp: {opts}"
    return "nothing sent by default, exactly what was asked for otherwise"


def test_a_size_this_app_does_not_know_is_never_guessed_at():
    """_PAPER_PTS answered A4 for anything missing from it, and SRA3 was
    missing from it — so choosing SRA3 sized the job as A4 and printed an A4
    area on the sheet. Not knowing has to read as not knowing."""
    from tools.printing.spool import paper_size_pt, custom_paper_key
    MM = 72 / 25.4                      # points per millimetre

    a4 = paper_size_pt("A4")
    assert paper_size_pt("SRA3") is not None, "SRA3 is still unknown"
    w, h = paper_size_pt("SRA3")
    assert abs(w / MM - 320) < 1 and abs(h / MM - 450) < 1, \
        f"SRA3 is {w / MM:.0f}x{h / MM:.0f}mm"
    assert paper_size_pt("SRA3") != a4, "SRA3 still resolves to A4"

    for unknown in ("Nonsense", "B7", "Custom.notanumber"):
        assert paper_size_pt(unknown) is None, \
            f"{unknown} was guessed at as {paper_size_pt(unknown)}"
    assert paper_size_pt("") is None, "the printer default is not a size"

    # Custom sizes, in the spelling CUPS documents.
    assert custom_paper_key(320, 450) == "Custom.320x450mm"
    w, h = paper_size_pt("Custom.320x450mm")
    assert abs(w / MM - 320) < 0.5 and abs(h / MM - 450) < 0.5
    return "SRA3 is a real size, and an unknown one stays unknown"


_READY_TWO_TRAYS = """
    media-col-ready (1setOf collection) =
    {media-size={x-dimension=21000 y-dimension=29700} media-source=tray-1 media-type=stationery},
    {media-size={x-dimension=29700 y-dimension=42000} media-source=tray-2 media-type=stationery}
"""


def _lp_with_ready(paper_key, source, ready=_READY_TWO_TRAYS):
    """The -o options a job carries, with the printer answering `ready` when
    asked what paper is loaded."""
    import subprocess
    from tools.printing import spool
    from tools.viewer.model import PageModel
    from pypdf import PdfReader

    src = FX["normal"]
    n = len(PdfReader(src).pages)
    _open(src)
    captured = []
    real = subprocess.run

    def spy(cmd, *a, **k):
        if cmd and cmd[0] == "ipptool":
            class R: returncode = 0; stdout = ready; stderr = ""
            return R()
        if cmd and cmd[0] == "lp":
            captured.append(cmd)
            class R: returncode = 0; stdout = "request id is t-1"; stderr = ""
            return R()
        return real(cmd, *a, **k)

    subprocess.run = spy
    try:
        spool.print_via_gs(src, PageModel(n), [0], 1, "auto", False, False,
                           "long", "press", 0, paper_key, 0,
                           null_progress(),
                           paper_source=("media-source", source) if source else None)
    finally:
        subprocess.run = real
    assert captured, "the job never reached lp"
    last = captured[-1]
    return [last[i + 1] for i, x in enumerate(last) if x == "-o"]


def test_a_chosen_tray_is_sent_with_the_size_that_is_in_it():
    """Leaving the size to the printer sends no media option, so CUPS fills in
    the queue's *default* size — which is not what the chosen tray holds. Pick
    the tray with A3 in it on a queue that defaults to A4 and the printer
    refuses the job, saying it has not got the right paper. It is right: it was
    asked for A4 out of a tray holding A3.

    "As set on the printer" has to mean the tray that was picked, so the job
    now carries the size actually loaded there.
    """
    opts = _lp_with_ready("", "tray-2")          # tray-2 holds A3
    assert "media=A3" in opts, f"the A3 in the chosen tray was not named: {opts}"
    assert "media-source=tray-2" in opts, opts

    opts = _lp_with_ready("", "tray-1")          # tray-1 holds A4
    assert "media=A4" in opts, f"the A4 in the chosen tray was not named: {opts}"
    return "the job names the paper that is in the tray it asks for"


def test_no_tray_and_no_size_still_sends_nothing_about_paper():
    """The default with no tray picked is unchanged: say nothing, and the
    queue's own setting stands. Only an explicitly chosen tray brings a size
    with it, because only then is there a tray whose contents can disagree."""
    opts = _lp_with_ready("", None)
    assert not [o for o in opts if o.startswith("media")], \
        f"paper was named for a job that chose neither: {opts}"
    return "nothing named when nothing was chosen"


def test_a_named_size_still_wins_over_what_is_in_the_tray():
    """Naming a size is an override and stays one — the operator may be
    deliberately running a smaller sheet out of a big tray."""
    opts = _lp_with_ready("SRA3", "tray-1")      # tray-1 holds A4
    assert "media=SRA3" in opts, f"the chosen size was overridden: {opts}"
    assert "media=A4" not in opts, opts
    return "an explicit size is not second-guessed"


def test_a_printer_that_cannot_say_what_is_loaded_changes_nothing():
    """Older queues, no network, no ipptool: the answer is unknown, and an
    unknown answer must leave the job exactly as it was rather than inventing
    a size for it."""
    opts = _lp_with_ready("", "tray-2", ready="")
    assert not [o for o in opts if o.startswith("media=")], \
        f"a size was invented from an empty answer: {opts}"
    assert "media-source=tray-2" in opts, "the tray choice was lost too"
    return "unknown stays unknown, and the tray is still asked for"
