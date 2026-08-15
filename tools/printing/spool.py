"""
Sending a document to paper.

Ghostscript and lp where they exist, Qt where they do not. Kept apart from the
dialog because none of it is about widgets: it takes a path, a page model and
the settings that were chosen, and it is the part that has to be right when the
copy shop is billing for the output.

_gs_blacked_out is here for the same reason. Ghostscript reports success while
turning a transparency group solid black, so what comes back is compared against
what went in before any of it reaches a printer.
"""
import logging
from PyQt6.QtGui import QPageLayout
from tools.app_state import AppState
from tools.i18n import tr
from tools.render.document_cache import PDFIUM_LOCK as _pdfium_lock

# Paper in points. Here rather than on the dialog: it is what a sheet
# measures, which the spooler needs as much as the widget that offers it.
_PAPER_PTS = {
    "A4":        (595.28, 841.89),  "A3":     (841.89, 1190.55),
    "A5":        (419.53, 595.28),  "Letter": (612.0,  792.0),
    "Legal":     (612.0,  1008.0),  "B4":     (708.66, 1000.63),
    "B5":        (498.90, 708.66),  "Executive": (521.86, 756.0),
    "Folio":     (612.0,  936.0),
}


def _gs_blacked_out(before, after, budget=60):
    """Page indexes that went from paper-white to near-black during Ghostscript's
    colour conversion for printing.

    Deliberately coarser than the page-by-page check the Grayscale and CMYK tools
    use: this step also scales, fits and re-centres, so content legitimately
    moves and a per-pixel comparison would flag healthy pages. Comparing mean
    brightness is immune to that and still catches the case that matters — a page
    that was mostly white coming back mostly black. Large documents are sampled,
    because this runs before every print and must not add noticeable delay.

    Returns [] when nothing is wrong and None if it could not be checked."""
    import pypdfium2 as pdfium
    try:
        with _pdfium_lock:
            a = pdfium.PdfDocument(before); b = pdfium.PdfDocument(after)
            try:
                n = min(len(a), len(b))
                if not n:
                    return None
                step = max(1, n // budget)
                bad = []
                for i in range(0, n, step):
                    pa = a[i].render(scale=0.12).to_pil().convert("L")
                    pb = b[i].render(scale=0.12).to_pil().convert("L")
                    ma = sum(pa.get_flattened_data()) / (pa.size[0] * pa.size[1] or 1)
                    mb = sum(pb.get_flattened_data()) / (pb.size[0] * pb.size[1] or 1)
                    if ma > 200 and mb < 80:
                        bad.append(i)
                return bad
            finally:
                a.close(); b.close()
    except Exception:
        logging.exception("print: could not verify the colour conversion")
        return None


def printer_options(printer_name, timeout=10):
    """What one queue offers, as {keyword: (label, choices, default)}.

    Parsed from `lpoptions -p NAME -l`, which is what CUPS documents as the way
    to list a printer's own options, and reports one per line:

        InputSlot/Media Source: Auto *Tray1 Tray2 Manual

    The starred choice is the queue's default. Keywords are PPD names on a
    driver queue (InputSlot, MediaType, OutputBin) and IPP names on a driverless
    one (media-source, media-type), so callers ask for both spellings rather
    than assuming which kind of queue this is.
    """
    import subprocess
    options = {}
    if not printer_name or printer_name in ("lp", "none"):
        return options
    try:
        r = subprocess.run(["lpoptions", "-p", printer_name, "-l"],
                           capture_output=True, text=True, errors="replace",
                           timeout=timeout)
    except Exception:
        logging.debug("printer_options: lpoptions failed for %s", printer_name,
                      exc_info=True)
        return options
    for line in r.stdout.splitlines():
        head, sep, rest = line.partition(":")
        if not sep:
            continue
        keyword, _, label = head.strip().partition("/")
        choices, default = [], None
        for choice in rest.split():
            if choice.startswith("*"):
                choice = choice[1:]
                default = choice
            choices.append(choice)
        if keyword and choices:
            options[keyword] = (label or keyword, choices, default)
    if not options:
        # Worth a line in the log: if the queue answers nothing, the dialog
        # falls back to Qt's guess and the user sees defaults they did not set.
        logging.info("printer_options: %s reported no options (rc=%s)",
                     printer_name, r.returncode)
    return options


# The paper tray, in the two spellings a queue may use for it. GTK's print
# dialog and Qt's both key on InputSlot; driverless queues report media-source.
PAPER_SOURCE_KEYWORDS = ("InputSlot", "media-source")

# Page size, likewise: PPD queues name it PageSize with values like "A4" or
# "A4.Borderless"; driverless queues name it media with PWG names like
# "iso_a4_210x297mm". Both reduce to the keys the dialog's combo carries.
PAGE_SIZE_KEYWORDS = ("PageSize", "media")
_PWG_TO_KEY = {
    "iso_a3": "A3", "iso_a4": "A4", "iso_a5": "A5", "iso_b5": "B5",
    "jis_b4": "B4", "jis_b5": "B5", "iso_b4": "B4",
    "na_letter": "Letter", "na_legal": "Legal", "na_executive": "Executive",
    "na_foolscap": "Folio", "na_folio": "Folio",
}


def normalise_page_size(value):
    """A queue's page-size value as the key the paper combo uses, or None.

    `A4` and `A4.Borderless` are both A4 — the suffix is a PPD variant, not a
    different sheet. `iso_a4_210x297mm` is the same size said the PWG way.
    """
    if not value:
        return None
    base = value.split(".")[0]
    for known in ("A0", "A1", "A2", "A3", "A4", "A5", "A6", "B4", "B5",
                  "Letter", "Legal", "Executive", "Folio"):
        if base.lower() == known.lower():
            return known
    parts = value.lower().split("_")
    if len(parts) >= 2:
        return _PWG_TO_KEY.get("_".join(parts[:2]))
    return None


def queue_defaults(printer_name):
    """What the queue itself is set to, as {"paper", "duplex", "duplex_edge"}.

    Read from CUPS rather than from Qt. QPrinterInfo.defaultPageSize() and
    defaultDuplexMode() are what this used to ask, and on a CUPS queue they are
    not reliable — the note on the duplex control records defaultDuplexMode()
    reporting DuplexNone for printers that plainly duplex. The starred choice in
    `lpoptions -l` is the queue's effective default, and it is exactly what the
    desktop's own printer settings write when the user sets one there.

    Any key the queue does not answer for is left out rather than guessed.
    """
    options = printer_options(printer_name)
    found = {}

    for keyword in PAGE_SIZE_KEYWORDS:
        if keyword in options:
            key = normalise_page_size(options[keyword][2])
            if key:
                found["paper"] = key
                break

    if "Duplex" in options:                     # PPD: None / DuplexNoTumble / DuplexTumble
        default = (options["Duplex"][2] or "").lower()
        if default:
            found["duplex"] = default != "none"
            found["duplex_edge"] = "short" if "tumble" == default[6:] else "long"
    elif "sides" in options:                    # IPP: one-sided / two-sided-*
        default = (options["sides"][2] or "").lower()
        if default:
            found["duplex"] = default != "one-sided"
            found["duplex_edge"] = "short" if default.endswith("short-edge") else "long"
    logging.info("queue_defaults: %s -> %s", printer_name, found or "nothing")
    return found


def paper_sources(printer_name):
    """(keyword, choices, default) for the paper tray, or None if the queue
    does not offer a choice of one."""
    options = printer_options(printer_name)
    for keyword in PAPER_SOURCE_KEYWORDS:
        if keyword in options:
            _label, choices, default = options[keyword]
            if len(choices) > 1:
                return keyword, choices, default
    return None


def recenter_on_paper(src_path, dest_path, paper_w_pt, paper_h_pt, factor=1.0):
    """Enlarge every page's media box to the full physical sheet size and
    centre the existing content on it, optionally scaled by `factor`.

    `factor` is what the percentage box beside "Originalgrösse" asks for: 1.0
    is the page at its own size, 0.5 half of it, 2.0 twice. It is applied here
    rather than by Ghostscript because GS can fit a page to the media or leave
    it alone, and neither of those is "make it 70 % of what it is". Scaling the
    content and re-centring it on a full sheet is the same operation this
    already did for the printable-area fit, with one more number in it.

    Ghostscript fits the content to the *printable area* (media set to
    paper − hardware margins).  This step places that printable-area page,
    unscaled, in the exact centre of a full-size sheet so the printer driver
    receives correctly-sized media and does not rescale — the on-screen
    preview and the physical print then agree.  The GS output is already
    normalised (upright, /Rotate cleared, media box at the origin), so a
    plain translate + media-box resize is safe here.
    """
    from pypdf import PdfReader, PdfWriter, Transformation
    from pypdf.generic import RectangleObject
    reader = PdfReader(src_path, strict=False)
    writer = PdfWriter()
    for page in reader.pages:
        box = page.mediabox
        x0 = float(box.left);   y0 = float(box.bottom)
        w0 = float(box.width);  h0 = float(box.height)
        # Scale about the page's own origin first, then centre what that
        # produced — centring a page and then scaling it moves it off centre.
        w1, h1 = w0 * factor, h0 * factor
        tx = (paper_w_pt - w1) / 2.0 - x0 * factor
        ty = (paper_h_pt - h1) / 2.0 - y0 * factor
        t = Transformation()
        if abs(factor - 1.0) > 1e-9:
            t = t.scale(factor, factor)
        page.add_transformation(t.translate(tx, ty))
        full = RectangleObject([0, 0, paper_w_pt, paper_h_pt])
        page.mediabox = full
        page.cropbox  = full
        writer.add_page(page)
    if not writer.pages:
        raise RuntimeError(tr("Re-centre: keine Seiten."))
    with open(dest_path, "wb") as f:
        writer.write(f)


def write_subset_pdf(pdf_path, model, pages, dest_path):
    """Write a subset of pages (with rotations) to dest_path."""
    from pypdf import PdfReader, PdfWriter
    readers = {}
    def _rdr(p):
        if p not in readers:
            readers[p] = PdfReader(p, strict=False)
        return readers[p]

    writer = PdfWriter()
    skipped = []
    try:
        for pos in pages:
            uid            = model.order[pos]
            src_path, orig = model.page_source(uid, pdf_path)
            rot            = model.get_rotation(uid)
            try:
                writer.add_page(_rdr(src_path).pages[orig])
                if rot:
                    writer.pages[-1].rotate(rot)   # rotate writer's copy, not reader's
            except Exception:
                skipped.append(pos + 1)
        if not writer.pages:
            raise RuntimeError(tr("Keine Seiten konnten gelesen werden (Datei beschädigt?)."))
        with open(dest_path, "wb") as f:
            writer.write(f)
    finally:
        for r in readers.values():
            try: r.stream.close()
            except Exception: pass   # inside a finally: anything raised here would
                                     # replace the error that brought us in
    return skipped


def prerender_for_qt(pdf_path, model, pages, color_mode, scale_idx, orient_idx,
                     paper_key, qt_dpi, hw_margin_mm, report, scale_pct=100):
    """Rasterise PDF pages via pypdfium2 in background.

    No QPrinter usage here — Qt objects must stay on the GUI thread.
    Dimensions are computed directly from paper points × DPI / 72.

    Returns (rendered_list, skipped_list).
    rendered_list items: (pil_image, page_orient, target_w_px, target_h_px)
    """
    import pypdfium2 as pdfium

    pw_pt, ph_pt = _PAPER_PTS.get(paper_key, (595.28, 841.89))
    full_bleed   = hw_margin_mm < 0.5
    margin_px    = 0 if full_bleed else max(0, int(hw_margin_mm / 25.4 * qt_dpi))

    def _target_dims(landscape):
        w_pt, h_pt = (ph_pt, pw_pt) if landscape else (pw_pt, ph_pt)
        w_px = max(1, int(w_pt * qt_dpi / 72) - 2 * margin_px)
        h_px = max(1, int(h_pt * qt_dpi / 72) - 2 * margin_px)
        return w_px, h_px

    rendered    = []
    skipped     = []
    pdfium_docs: dict = {}

    try:
        for i, pos in enumerate(pages):
            report(tr("Rendere Seite {i} / {total}…").format(i=i + 1, total=len(pages)))
            uid = model.order[pos]
            src_path, orig = model.page_source(uid, pdf_path)
            rot = model.get_rotation(uid)
            try:
                with _pdfium_lock:
                    if src_path not in pdfium_docs:
                        pdfium_docs[src_path] = pdfium.PdfDocument(src_path)
                    pdfpage = pdfium_docs[src_path][orig]
                    pdfw    = pdfpage.get_width()
                    pdfh    = pdfpage.get_height()

                    # Determine per-page orientation
                    if orient_idx == 0:
                        page_is_ls = (pdfw > pdfh) != bool(rot % 180)
                    else:
                        page_is_ls = (orient_idx == 2)
                    page_orient = (QPageLayout.Orientation.Landscape
                                   if page_is_ls
                                   else QPageLayout.Orientation.Portrait)

                    target_w, target_h = _target_dims(page_is_ls)

                    scale_fit = min(target_w / max(pdfw, 1),
                                    target_h / max(pdfh, 1))
                    scale_100 = qt_dpi / 72.0
                    if scale_idx == 0:
                        scale = scale_fit
                    elif scale_idx == 1:
                        # Original size, times whatever the percentage box says.
                        scale = scale_100 * (scale_pct / 100.0)
                    else:
                        scale = min(scale_100, scale_fit)

                    bm  = pdfpage.render(scale=max(0.5, scale))
                    pil = bm.to_pil().convert("RGB")

                # No convert("L") here either: the QPrinter colour mode
                # below carries the request, and throwing the colour away in
                # the raster made this path just as irreversible as the
                # Ghostscript one.
                if rot:
                    pil = pil.rotate(-rot, expand=True)

                rendered.append((pil, page_orient, target_w, target_h))

            except Exception:
                logging.exception("Qt render: page %d", pos + 1)
                skipped.append(pos + 1)
    finally:
        for doc in pdfium_docs.values():
            try: doc.close()
            except Exception: pass   # closing what we can; a failure here cannot change the job's outcome

    if not rendered:
        raise RuntimeError(tr("Keine Seiten konnten gerendert werden."))
    return rendered, skipped


def print_via_gs(pdf_path, model, pages, copies, color_mode, collate, duplex,
                  duplex_edge, colorconv, printer_name, scale_idx,
                  paper_key, orient_idx, hw_margin_mm, report,
                  paper_source=None, scale_pct=100):
    """Full-quality print via Ghostscript + CUPS/lp.

    GS normalises, embeds fonts, applies colour conversion AND pre-scales the
    output to exactly the target paper dimensions.  lp then receives a
    correctly-sized PDF and is told print-scaling=none — no double-scaling.

    For "Fit" and "Shrink" the content is fitted to the *printable area*
    (paper minus the printer's unprintable hardware margin), matching the
    on-screen preview exactly, then re-centred on a full-size sheet so the
    printer never clips content or rescales.
    """
    import subprocess, shutil, tempfile, os

    pw_pt, ph_pt = _PAPER_PTS.get(paper_key, (595.28, 841.89))

    # Determine target paper orientation
    if orient_idx == 2:          # explicit landscape
        pw_pt, ph_pt = ph_pt, pw_pt
    elif orient_idx == 0:        # auto — detect from first selected page
        try:
            uid = model.order[pages[0]]
            src_path, orig = model.page_source(uid, pdf_path)
            import pypdfium2 as pdfium
            with _pdfium_lock:
                doc = pdfium.PdfDocument(src_path)
                try:
                    pg = doc[orig]; pdfw = pg.get_width(); pdfh = pg.get_height()
                finally:
                    doc.close()
            rot0 = model.get_rotation(model.order[pages[0]])
            if (pdfw > pdfh) != bool(rot0 % 180):   # page is landscape
                if pw_pt < ph_pt:
                    pw_pt, ph_pt = ph_pt, pw_pt
        except Exception:
            logging.debug("could not read the first page's orientation; "
                          "using the paper as given", exc_info=True)
    # orient_idx == 1 → portrait: keep pw_pt < ph_pt as-is

    sub_fd, sub_tmp = tempfile.mkstemp(suffix="_sub.pdf")
    os.close(sub_fd)
    norm_tmp = None
    recenter_tmp = None
    scaled_tmps = []
    printable_unrecentered = False
    try:
        report(tr("Seiten zusammenstellen… ({count})").format(count=len(pages)))
        skipped = write_subset_pdf(pdf_path, model, pages, sub_tmp)
        print_src = sub_tmp
        cups_fit  = (scale_idx == 0)   # used by both the GS + lp branches

        if shutil.which("gs"):
            norm_fd, norm_tmp = tempfile.mkstemp(suffix="_norm.pdf")
            os.close(norm_fd)
            report(tr("Ghostscript: Normalisierung und Skalierung…"))

            # ── Scaling strategy ──────────────────────────────────────────
            # "Fit" (scale_idx 0) is delegated to CUPS/pdftopdf, which scales
            # each page to the driver's REAL imageable area — the printer's
            # actual margins, exactly like Acrobat's "Fit to Printable Area"
            # (and it adapts per printer). GS just normalises at natural size.
            # "Shrink" (scale_idx 2) pre-fits to our printable-area estimate
            # and re-centres it; "Originalgrösse" (scale_idx 1) prints at the
            # percentage asked for on full media — 100 % being 1:1, and
            # anything else applied by recenter_on_paper afterwards, because
            # Ghostscript can fit a page or leave it alone and neither of those
            # is "make it 70 % of what it is".
            margin_pt = (0.0 if hw_margin_mm < 0.5
                         else hw_margin_mm * 72.0 / 25.4)
            fit_to_printable = (scale_idx == 2) and margin_pt > 0.0
            if fit_to_printable:
                media_w = max(1.0, pw_pt - 2.0 * margin_pt)
                media_h = max(1.0, ph_pt - 2.0 * margin_pt)
            else:
                media_w, media_h = pw_pt, ph_pt

            gs_cmd = [
                "gs", "-dBATCH", "-dNOPAUSE", "-dQUIET",
                "-sDEVICE=pdfwrite",
                "-dCompatibilityLevel=1.5",
                "-dPDFSETTINGS=/printer",
                "-dEmbedAllFonts=true",
                "-dSubsetFonts=true",
                "-dCompressFonts=true",
                "-dDetectDuplicateImages=true",
            ]
            if not cups_fit:
                # Fix output media to the fit target (printable area or sheet).
                gs_cmd += [
                    f"-dDEVICEWIDTHPOINTS={media_w}",
                    f"-dDEVICEHEIGHTPOINTS={media_h}",
                    "-dFIXEDMEDIA",
                ]

            # Scaling policy
            if cups_fit:
                pass                # CUPS fits to the imageable area (see lp -o)
            elif scale_idx == 2:    # Shrink only — fit if ANY page exceeds target
                try:
                    import pypdfium2 as pdfium
                    needs_fit = False
                    with _pdfium_lock:
                        doc = pdfium.PdfDocument(sub_tmp)
                        try:
                            for pi in range(len(doc)):
                                pg = doc[pi]
                                if pg.get_width() > media_w + 1 or pg.get_height() > media_h + 1:
                                    needs_fit = True
                                    break
                        finally:
                            doc.close()
                    if needs_fit:
                        gs_cmd.append("-dPDFFitPage")
                    # else: all pages fit — FIXEDMEDIA centres at natural size
                except Exception:
                    gs_cmd.append("-dPDFFitPage")   # safe fallback
            # scale_idx == 1 (100 %): FIXEDMEDIA without -dPDFFitPage → 1:1

            # Colour handling.
            # "Graustufen" deliberately does NOT convert the PDF here. It
            # used to, and that destroyed the colour in the spooled file:
            # the job was monochrome for good, so a queue that was later
            # re-routed, or settings picked on another machine, could never
            # bring the colour back. Mono is requested as a CUPS job option
            # instead (see print-color-mode below) and the printer does the
            # conversion, exactly as Evince, Chrome and Acrobat do it. The
            # explicit "Farbkonvertierung" choices below stay, because there
            # the user is asking for the data itself to be converted.
            if colorconv == 1:
                gs_cmd += ["-sColorConversionStrategy=CMYK",
                            "-dProcessColorModel=/DeviceCMYK"]
            elif colorconv == 2:
                gs_cmd += ["-sColorConversionStrategy=sRGB",
                            "-dProcessColorModel=/DeviceRGB"]
            else:
                gs_cmd += ["-sColorConversionStrategy=LeaveColorUnchanged"]

            gs_cmd += [f"-sOutputFile={norm_tmp}", sub_tmp]
            r = subprocess.run(gs_cmd, capture_output=True, text=True, errors="replace", timeout=240)
            # Only when a colour conversion was actually asked for, and only
            # once Ghostscript reported success — otherwise there is nothing
            # meaningful to compare against.
            converted = colorconv in (1, 2)
            blackout = (_gs_blacked_out(sub_tmp, norm_tmp)
                        if (converted and r.returncode == 0
                            and os.path.getsize(norm_tmp) > 100)
                        else None)
            if blackout:
                # Print unconverted rather than print black paper. Same
                # fallback Ghostscript failing outright already takes.
                logging.error("print: colour conversion blacked out page(s) %s "
                              "— printing the unconverted file", blackout)
                AppState.get().status_message.emit(tr(
                    'Farbumwandlung hat Seite(n) {p0} geschwaerzt — es wird '
                    'ohne Umwandlung gedruckt.').format(
                        p0=", ".join(str(i + 1) for i in blackout)))
            elif r.returncode == 0 and os.path.getsize(norm_tmp) > 100:
                print_src = norm_tmp
                # Re-centre the printable-area page on a full-size sheet so
                # CUPS receives correctly-sized media and never rescales.
                if fit_to_printable:
                    try:
                        rec_fd, recenter_tmp = tempfile.mkstemp(suffix="_ctr.pdf")
                        os.close(rec_fd)
                        recenter_on_paper(norm_tmp, recenter_tmp,
                                                pw_pt, ph_pt)
                        if os.path.getsize(recenter_tmp) > 100:
                            print_src = recenter_tmp
                        else:
                            printable_unrecentered = True
                    except Exception:
                        logging.warning("Re-centre step failed; letting CUPS "
                                        "fit to printable area", exc_info=True)
                        printable_unrecentered = True
            else:
                logging.warning("GS normalization failed (rc=%d): %s",
                                r.returncode, r.stderr[:300])

        # Spool via lp/CUPS
        report(tr("Sende an Drucker…"))
        cmd = ["lp"]
        if printer_name and printer_name not in ("lp", "none"):
            cmd += ["-d", printer_name]
        cmd += ["-n", str(copies)]

        # Scaling: "Fit" → let CUPS fit to the driver's imageable area;
        # everything else was already sized exactly by GS (+ re-centre).
        if cups_fit:
            cmd += ["-o", "fit-to-page"]
        # Originalgrösse at anything but 100 %: scale the content and centre it
        # on the full sheet. Same step the printable-area fit uses, with the
        # percentage as its factor.
        if (scale_idx == 1 and abs(scale_pct - 100) > 0
                and print_src in (norm_tmp, sub_tmp)):
            try:
                pct_fd, pct_tmp = tempfile.mkstemp(suffix="_pct.pdf")
                os.close(pct_fd)
                recenter_on_paper(print_src, pct_tmp, pw_pt, ph_pt,
                                  factor=scale_pct / 100.0)
                if os.path.getsize(pct_tmp) > 100:
                    print_src = pct_tmp
                    recenter_tmp = recenter_tmp or pct_tmp
                    scaled_tmps.append(pct_tmp)
            except Exception:
                logging.warning("print: could not apply %d%% scaling; printing "
                                "at original size", scale_pct, exc_info=True)

        elif print_src in (norm_tmp, recenter_tmp) and not printable_unrecentered:
            cmd += ["-o", "print-scaling=none"]
        elif printable_unrecentered:
            # Re-centre failed: page is printable-area sized — let CUPS fit it
            # to the imageable area (single scaling, centred, no clipping).
            cmd += ["-o", "print-scaling=fit"]
        else:
            # GS unavailable — let CUPS handle scaling
            if scale_idx == 0:
                cmd += ["-o", "fit-to-page"]
            elif scale_idx == 1:
                cmd += ["-o", "print-scaling=none"]
            else:
                cmd += ["-o", "fit-to-page"]

        if orient_idx == 1:
            cmd += ["-o", "orientation-requested=3"]
        elif orient_idx == 2:
            cmd += ["-o", "orientation-requested=4"]

        if duplex:
            # Emit BOTH the IPP attribute AND the PPD driver keyword.
            # Driverless/IPP queues (e.g. the Xerox) honour `sides`; the
            # EPSON/Brother-style PPD filters read the `Duplex` keyword and
            # otherwise fall back to the queue's stored default — which is
            # what made portrait backs come out tumbled (short edge) even
            # though we asked for long edge. Both values are synonyms, and
            # CUPS ignores whichever the driver doesn't understand, so
            # sending both is safe and forces the correct binding edge.
            if duplex_edge == "short":
                cmd += ["-o", "sides=two-sided-short-edge",
                        "-o", "Duplex=DuplexTumble"]
            else:
                cmd += ["-o", "sides=two-sided-long-edge",
                        "-o", "Duplex=DuplexNoTumble"]
        else:
            # Say one-sided, rather than saying nothing. Sending nothing leaves
            # the queue's own default in force, and a queue defaulted to duplex
            # — which is the usual office setting, and what `lpoptions -d
            # printer -o sides=two-sided-long-edge` leaves behind — then printed
            # both sides however the box was left. Unticking it did nothing.
            #
            # Both spellings again, for the same reason as above. GTK's print
            # dialog and Qt's CUPS plugin both send one-sided explicitly.
            cmd += ["-o", "sides=one-sided", "-o", "Duplex=None"]
        # Force the colour mode explicitly so this job overrides the
        # printer's system-wide default (e.g. a queue whose default is
        # monochrome must still print in colour when the user picks "Farbe").
        # print-color-mode is the driver-independent IPP attribute;
        # ColorModel=Gray is kept as a fallback for older PPD-only drivers.
        if color_mode == "mono":
            cmd += ["-o", "print-color-mode=monochrome",
                    "-o", "ColorModel=Gray"]
        elif color_mode == "color":
            cmd += ["-o", "print-color-mode=color"]
        # "auto": send no colour option at all, so the queue's own default
        # applies and the decision can still be made downstream.

        # Collation via standard IPP multiple-document-handling attribute
        if copies > 1:
            mdc = ("separate-documents-collated-copies" if collate
                   else "separate-documents-uncollated-copies")
            cmd += ["-o", f"multiple-document-handling={mdc}"]

        cmd += ["-o", f"media={paper_key}"]
        # The tray, under the keyword this queue actually uses — InputSlot on a
        # driver queue, media-source on a driverless one. paper_source carries
        # both, so nothing here has to guess which kind it is talking to.
        if paper_source:
            keyword, choice = paper_source
            cmd += ["-o", f"{keyword}={choice}"]
        cmd.append(print_src)

        result = subprocess.run(cmd, capture_output=True, text=True, errors="replace", timeout=60)
        if result.returncode != 0:
            raise RuntimeError(
                result.stderr.strip() or result.stdout.strip()
                or f"lp: exit code {result.returncode}")
        return skipped

    finally:
        for f in [sub_tmp, norm_tmp, recenter_tmp] + scaled_tmps:
            if f:
                try: os.unlink(f)
                except OSError: pass   # temp file already gone
