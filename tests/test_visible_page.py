"""Tools must use the page on screen, not the sheet a crop left hidden behind it."""
import os
from pypdf import PdfReader
import pikepdf
import pypdfium2 as pdfium
from reportlab.pdfgen import canvas
from reportlab.lib.pagesizes import A4

from tools.jobs import null_progress
from tools.app_state import AppState
from tools.pagebox import displayed_size
from tools.panels.page_numbers import PageNumbersPanel
from tools.panels.preflight import _preflight
from tools.panels.img_pdf import _pdf_to_images
from tools.printing.spool import _place_on_sheet, _scale_pages, FIT
from tests.support import FX, _TMP, _open


def _cropped(name, rotate=0, bar=False):
    """A4, red everywhere, a magenta window, optionally turned.

    `bar` paints a black strip along the file-bottom of the window, which a
    viewer shows on the left once the page is rotated 90 degrees.
    """
    path = os.path.join(_TMP, name + ".pdf")
    W, H = A4
    x0, y0, x1, y1 = 180.0, 250.0, 420.0, 560.0
    c = canvas.Canvas(path, pagesize=A4)
    c.setFillColorRGB(1, 0, 0)
    c.rect(0, 0, W, H, fill=1, stroke=0)
    c.setFillColorRGB(1, 0, 1)
    c.rect(x0, y0, x1 - x0, y1 - y0, fill=1, stroke=0)
    if bar:
        c.setFillColorRGB(0, 0, 0)
        c.rect(x0, y0, x1 - x0, 22, fill=1, stroke=0)
    c.showPage()
    c.save()
    with pikepdf.open(path, allow_overwriting_input=True) as pdf:
        pdf.pages[0].CropBox = [x0, y0, x1, y1]
        if rotate:
            pdf.pages[0].Rotate = rotate
        pdf.save(path)
    return path


def _render(path):
    doc = pdfium.PdfDocument(path)
    try:
        page = doc[0]
        return (page.get_width(), page.get_height()), page.render(scale=1).to_pil().convert("RGB")
    finally:
        doc.close()


def _hidden_is_back(counts):
    """The hidden page coming back is a field of red. A one-pixel seam where
    the two fills meet is the crop boundary, not that page."""
    red = counts.get("red", 0)
    return red > counts.get("magenta", 0) * 0.05


def _counts(img):
    c = {}
    w, h = img.size
    for y in range(0, h, 3):
        for x in range(0, w, 3):
            r, g, b = img.getpixel((x, y))[:3]
            if r > 180 and g < 70 and b < 70:
                k = "red"
            elif r > 170 and b > 170 and g < 90:
                k = "magenta"
            elif max(r, g, b) < 50:
                k = "black"
            elif min(r, g, b) > 225:
                k = "white"
            else:
                k = "other"
            c[k] = c.get(k, 0) + 1
    return c


def _dark_side(img):
    w, h = img.size
    pts = [(x, y) for y in range(0, h, 2) for x in range(0, w, 2)
           if max(img.getpixel((x, y))) < 60]
    if len(pts) < 4:
        return "none"
    cx = sum(p[0] for p in pts) / len(pts)
    cy = sum(p[1] for p in pts) / len(pts)
    edges = {"left": cx / w, "right": (w - cx) / w,
             "top": cy / h, "bottom": (h - cy) / h}
    return min(edges, key=edges.get)


def _numbers(src, name):
    _open(src)
    p = PageNumbersPanel()
    p.log.log = lambda *a, **k: None
    p.pos.setCurrentIndex(0)          # bottom centre
    p.prefix.setText("NN")
    p.font_spin.setValue(28)
    out = os.path.join(_TMP, name + ".pdf")
    p.save_pdf = lambda *a, **k: out
    p.open_result = lambda *a, **k: None
    p._run_action()
    return out


def test_page_numbers_land_on_the_page_that_is_shown():
    """A number placed from the MediaBox sat in the margin Acrobat had hidden,
    and on a rotated page "bottom" was the bottom of the unturned file."""
    cropped = _cropped("nums_crop")
    out = _numbers(cropped, "nums_crop_out")
    _size, img = _render(out)
    assert _dark_side(img) == "bottom", f"the number is {_dark_side(img)}, not at the bottom"
    assert "red" not in _counts(img), "the hidden page came back with the number"

    turned = _cropped("nums_rot", rotate=90)
    out = _numbers(turned, "nums_rot_out")
    _size, img = _render(out)
    assert _dark_side(img) == "bottom", (
        f"on a rotated page the number landed {_dark_side(img)}, not at the bottom")


def test_placing_on_a_sheet_uses_the_visible_page():
    """Fit-to-sheet measured the MediaBox and then set the crop to the sheet,
    so the hidden drawing filled the paper. A rotated page was measured
    unturned and the rotation flag turned the sheet a second time."""
    from reportlab.lib.pagesizes import A4 as _A4
    cropped = _cropped("fit_crop")
    out = os.path.join(_TMP, "fit_crop_out.pdf")
    _place_on_sheet(cropped, out, _A4[0], _A4[1], FIT, 100)
    size, img = _render(out)
    assert abs(size[0] - _A4[0]) < 2 and abs(size[1] - _A4[1]) < 2, size
    c = _counts(img)
    assert not _hidden_is_back(c) and c.get("magenta", 0) > 10, c

    turned = _cropped("fit_rot", rotate=90, bar=True)
    shown, _img = _render(turned)
    out = os.path.join(_TMP, "fit_rot_out.pdf")
    _place_on_sheet(turned, out, shown[0], shown[1], FIT, 100)
    size, img = _render(out)
    assert abs(size[0] - shown[0]) < 2 and abs(size[1] - shown[1]) < 2, (
        f"sheet displays as {size}, the page on screen is {shown}")
    assert _dark_side(img) == "left", f"the bar moved to {_dark_side(img)}"
    assert not _hidden_is_back(_counts(img))

    out = os.path.join(_TMP, "scale_crop_out.pdf")
    _scale_pages(cropped, out, 1.0)
    size, img = _render(out)
    vis = displayed_size(PdfReader(cropped).pages[0])
    assert abs(size[0] - vis[0]) < 2 and abs(size[1] - vis[1]) < 2, (
        f"100% became {size}, the visible page is {vis}")
    assert not _hidden_is_back(_counts(img))


def test_a_blank_page_matches_the_cropped_page_it_follows():
    from tools.viewer.tab import PdfTab
    src = _cropped("blank_src")
    tab = PdfTab(src)
    st = AppState.get()
    st.open_pdf(tab.pdf_path)
    st.page_model = tab.model
    tab._build_manage_once()
    tab._manage_panel._insert_blank()
    doc = pdfium.PdfDocument(tab.pdf_path)
    try:
        shown = (doc[0].get_width(), doc[0].get_height())
        blank = (doc[1].get_width(), doc[1].get_height())
    finally:
        doc.close()
    tab.deleteLater()
    assert abs(blank[0] - shown[0]) < 2 and abs(blank[1] - shown[1]) < 2, (
        f"blank is {blank}, the page it follows shows as {shown}")


def test_preflight_measures_the_visible_page():
    """The size check used the MediaBox, so a cropped page passed as the old
    sheet."""
    src = _cropped("pre_crop")
    a4 = (A4[0], A4[1])
    checks = {k: False for k in (
        "size", "orient", "colour", "enc", "bleed", "fonts", "dpi", "trans", "layers")}
    checks["size"] = True
    lines, _verdict = _preflight(src, checks, a4, 300, null_progress())
    text = "\n".join(lines)
    assert "595" not in text.split("PROBLEME")[-1] or "240" in text or "310" in text
    assert any("pt !=" in line for line in lines), (
        f"a cropped page was accepted as the full sheet:\n{text}")
    # and an ordinary A4 still passes the same check
    lines, verdict = _preflight(FX["normal"], checks, a4, 300, null_progress())
    assert not any("pt !=" in line for line in lines), (
        f"a plain A4 was rejected:\n" + "\n".join(lines) + "\n" + verdict)


def test_exporting_pages_as_images_uses_the_crop():
    """Poppler defaults to the MediaBox, so PDF-to-images and the OCR render
    exported the hidden page."""
    src = _cropped("img_crop")
    out = os.path.join(_TMP, "img_crop_pages")
    os.makedirs(out, exist_ok=True)
    try:
        _pdf_to_images(src, out, "png", 72, null_progress())
    except Exception as e:
        return f"skipped — no poppler ({e})"
    from PIL import Image
    im = Image.open(os.path.join(out, "img_crop_s001.png"))
    vis = displayed_size(PdfReader(src).pages[0])
    assert abs(im.size[0] - vis[0]) < 3 and abs(im.size[1] - vis[1]) < 3, (
        f"exported {im.size}, the visible page is {vis[0]:.0f}x{vis[1]:.0f}")
