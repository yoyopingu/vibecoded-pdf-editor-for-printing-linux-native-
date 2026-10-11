"""Document export formats and workers, independent of viewer widgets.

Only real PDF conversion paths belong here. An Office input extension does not
imply a PDF exporter: Calc, for example, cannot reconstruct a PDF's tables.
LibreOffice's explicit Writer/Impress/Draw PDF importers choose the right model.
"""
import os
import shutil
import tempfile
import uuid
import zipfile
from datetime import datetime, timezone
from html import escape
from dataclasses import dataclass
from pathlib import Path

from PIL import Image
from pypdf import PdfReader, PdfWriter

from tools.i18n import tr


@dataclass(frozen=True)
class ExportFormat:
    key: str
    label: str
    extensions: tuple
    backend: str = ''
    per_page: bool = False
    input_filter: str = ''
    output_filter: str = ''
    image_mode: str = 'RGB'

    def dialog_filter(self):
        return f'{tr(self.label)} ({" ".join("*." + e for e in self.extensions)})'


FORMATS = (
    ExportFormat('pdf', 'PDF', ('pdf',)),
    ExportFormat('png', 'PNG', ('png',), 'image', True, output_filter='PNG'),
    ExportFormat('jpeg', 'JPEG', ('jpg', 'jpeg'), 'image', True, output_filter='JPEG'),
    ExportFormat('tiff', 'TIFF (mehrseitig)', ('tiff', 'tif'), 'image', output_filter='TIFF'),
    ExportFormat('webp', 'WebP', ('webp',), 'image', True, output_filter='WEBP'),
    ExportFormat('bmp', 'BMP', ('bmp',), 'image', True, output_filter='BMP'),
    ExportFormat('gif', 'GIF', ('gif',), 'image', True, output_filter='GIF'),
    ExportFormat('jp2', 'JPEG 2000', ('jp2', 'j2k'), 'image', True, output_filter='JPEG2000'),
    ExportFormat('avif', 'AVIF', ('avif',), 'image', True, output_filter='AVIF'),
    ExportFormat('qoi', 'QOI', ('qoi',), 'image', True, output_filter='QOI'),
    ExportFormat('ppm', 'PPM', ('ppm',), 'image', True, output_filter='PPM'),
    ExportFormat('pgm', 'PGM (Graustufen)', ('pgm',), 'image', True, output_filter='PPM', image_mode='L'),
    ExportFormat('pbm', 'PBM (Schwarzweiß)', ('pbm',), 'image', True, output_filter='PPM', image_mode='1'),
    ExportFormat('tga', 'TGA', ('tga',), 'image', True, output_filter='TGA'),
    ExportFormat('pcx', 'PCX', ('pcx',), 'image', True, output_filter='PCX'),
    ExportFormat('svg', 'SVG', ('svg',), 'cairo', True),
    ExportFormat('eps', 'EPS', ('eps',), 'cairo', True),
    ExportFormat('ps', 'PostScript', ('ps',), 'cairo'),
    ExportFormat('txt', 'Text (UTF-8)', ('txt',), 'text'),
    ExportFormat('html', 'HTML', ('html', 'htm'), 'html'),
    ExportFormat('xml', 'XML (Text und Positionen)', ('xml',), 'xml'),
    ExportFormat('odt', 'OpenDocument Text', ('odt',), 'office', input_filter='writer_pdf_import', output_filter='writer8'),
    ExportFormat('docx', 'Word DOCX', ('docx',), 'office', input_filter='writer_pdf_import', output_filter='Office Open XML Text'),
    ExportFormat('doc', 'Word DOC', ('doc',), 'office', input_filter='writer_pdf_import', output_filter='MS Word 97'),
    ExportFormat('rtf', 'Rich Text (Textfluss)', ('rtf',), 'office', input_filter='writer_pdf_import', output_filter='Rich Text Format'),
    ExportFormat('fodt', 'OpenDocument Text (XML)', ('fodt',), 'office', input_filter='writer_pdf_import', output_filter='OpenDocument Text Flat XML'),
    ExportFormat('ott', 'OpenDocument Textvorlage', ('ott',), 'office', input_filter='writer_pdf_import', output_filter='writer8_template'),
    ExportFormat('epub', 'EPUB (festes Layout)', ('epub',), 'epub'),
    ExportFormat('odp', 'OpenDocument Präsentation', ('odp',), 'office', input_filter='impress_pdf_import', output_filter='impress8'),
    ExportFormat('pptx', 'PowerPoint PPTX', ('pptx',), 'office', input_filter='impress_pdf_import', output_filter='Impress MS PowerPoint 2007 XML'),
    ExportFormat('ppt', 'PowerPoint PPT', ('ppt',), 'office', input_filter='impress_pdf_import', output_filter='MS PowerPoint 97'),
    ExportFormat('fodp', 'OpenDocument Präsentation (XML)', ('fodp',), 'office', input_filter='impress_pdf_import', output_filter='OpenDocument Presentation Flat XML'),
    ExportFormat('otp', 'OpenDocument Präsentationsvorlage', ('otp',), 'office', input_filter='impress_pdf_import', output_filter='impress8_template'),
    ExportFormat('odg', 'OpenDocument Zeichnung', ('odg',), 'office', input_filter='draw_pdf_import', output_filter='draw8'),
    ExportFormat('fodg', 'OpenDocument Zeichnung (XML)', ('fodg',), 'office', input_filter='draw_pdf_import', output_filter='OpenDocument Drawing Flat XML'),
    ExportFormat('otg', 'OpenDocument Zeichnungsvorlage', ('otg',), 'office', input_filter='draw_pdf_import', output_filter='draw8_template'),
)


def export_formats():
    # Pillow builds differ (notably AVIF). Do not advertise an encoder which
    # this installation cannot use. External dependencies get a named error.
    Image.init()
    return tuple(f for f in FORMATS if f.backend != 'image' or f.output_filter in Image.SAVE)


def resolve_export(path, selected_filter=''):
    """Use a typed extension first; repair a stale .pdf suffix after changing
    the format dropdown. Never write image/Office bytes into a .pdf filename."""
    formats = export_formats()
    selected = next((f for f in formats if f.dialog_filter() == selected_filter), None)
    suffix = Path(path).suffix.lower().lstrip('.')
    typed = next((f for f in formats if suffix in f.extensions), None)
    if selected is not None and typed is not None and typed.key == 'pdf' and selected.key != 'pdf':
        path = str(Path(path).with_suffix('.' + selected.extensions[0]))
        return path, selected
    if typed is not None:
        return path, typed
    if suffix:
        raise ValueError(tr('Nicht unterstütztes Ausgabeformat: {p0}').format(p0=suffix))
    selected = selected or formats[0]
    return path + '.' + selected.extensions[0], selected


def output_paths(path, fmt, count):
    if fmt.per_page and count > 1:
        base, ext = os.path.splitext(path)
        digits = max(3, len(str(count)))
        return [f'{base}_{i:0{digits}d}{ext}' for i in range(1, count + 1)]
    return [path]


def _program(name):
    path = shutil.which(name)
    if name == 'soffice':
        path = path or shutil.which('libreoffice')
    if not path:
        raise RuntimeError(tr('Benötigtes Programm nicht gefunden: {p0}').format(p0=name))
    return path


def _run(report, command, timeout=300):
    result = report.run(command, timeout=timeout)
    if result.returncode:
        detail = result.stderr
        if isinstance(detail, bytes):
            detail = detail.decode('utf-8', errors='replace')
        raise RuntimeError(tr('Konvertierung fehlgeschlagen:\n{p0}').format(p0=str(detail)[-1000:]))
    report.check()


def _snapshot(sources, path, report):
    readers = {}
    writer = PdfWriter()
    try:
        for source, index, rotation in sources:
            report.check()
            if source not in readers:
                readers[source] = PdfReader(source, strict=False)
            reader = readers[source]
            # A repeated source page must remain an independent copy: rotating
            # its second instance must not rotate the first as well. append
            # also retains AcroForm, unlike a bare add_page.
            writer.reset_translation(reader)
            writer.append(reader, pages=[index], import_outline=False)
            page = writer.pages[-1]
            if rotation:
                page.rotate(rotation)
            page.mediabox = page.cropbox
        writer.write(path)
    finally:
        writer.close()
        for reader in readers.values():
            reader.close()


def _images(src, targets, fmt, dpi, stage, report, count):
    program = _program('pdftoppm')

    def frames():
        for i in range(count):
            report.check()
            report(tr('Seite {i} / {total}…').format(i=i + 1, total=count))
            prefix = os.path.join(stage, 'render')
            _run(report, [program, '-f', str(i + 1), '-l', str(i + 1),
                          '-singlefile', '-cropbox', '-r', str(dpi), '-png', src, prefix])
            with Image.open(prefix + '.png') as raw:
                with raw.convert(fmt.image_mode) as image:
                    yield image

    images = frames()
    try:
        if fmt.key == 'tiff':
            first = next(images)
            first.save(targets[0], 'TIFF', save_all=True, append_images=images,
                       compression='tiff_deflate', dpi=(dpi, dpi))
        else:
            for image, target in zip(images, targets):
                kwargs = {'dpi': (dpi, dpi)}
                if fmt.key in ('jpeg', 'webp', 'avif'):
                    kwargs['quality'] = 95
                image.save(target, fmt.output_filter, **kwargs)
    finally:
        images.close()


def _convert(src, targets, fmt, dpi, stage, report, count):
    if fmt.backend == 'image':
        _images(src, targets, fmt, dpi, stage, report, count)
    elif fmt.backend == 'cairo':
        program = _program('pdftocairo')
        for i, target in enumerate(targets):
            command = [program, '-' + fmt.key]
            if fmt.key in ('ps', 'svg'):
                command += ['-origpagesizes']
            if fmt.per_page:
                command += ['-f', str(i + 1), '-l', str(i + 1)]
            _run(report, command + [src, target])
    elif fmt.backend == 'text':
        _run(report, [_program('pdftotext'), '-layout', '-enc', 'UTF-8', src, targets[0]])
    elif fmt.backend in ('html', 'xml'):
        command = [_program('pdftohtml'), '-enc', 'UTF-8']
        if fmt.backend == 'html':
            command += ['-s', '-noframes', '-dataurls']
        else:
            # Positioned text XML is a single file, not a half-export with
            # untracked image sidecars in the user's directory.
            command += ['-xml', '-i']
        _run(report, command + [src, targets[0]])
    elif fmt.backend == 'epub':
        _epub(src, targets[0], stage, report)
    elif fmt.backend == 'office':
        profile = Path(stage, 'lo-profile').as_uri()
        input_filter = fmt.input_filter
        office_source = src
        if fmt.key == 'rtf':
            # RTF cannot represent the transformed text boxes produced by the
            # PDF importer: a rotated page loses/clips its text on reopening.
            # Writer's HTML importer instead produces ordinary flowing text.
            office_source = os.path.join(stage, 'document.html')
            _run(report, [_program('pdftohtml'), '-s', '-noframes', '-dataurls',
                          '-enc', 'UTF-8', src, office_source])
            input_filter = 'HTML (StarWriter)'
        _run(report, [_program('soffice'), '-env:UserInstallation=' + profile,
                      '--headless', '--infilter=' + input_filter,
                      '--convert-to', fmt.extensions[0] + ':' + fmt.output_filter,
                      '--outdir', stage, office_source], timeout=600)
        generated = os.path.join(stage, 'document.' + fmt.extensions[0])
        if not os.path.isfile(generated):
            raise RuntimeError(tr('Konvertierung fehlgeschlagen:\n{p0}').format(p0=fmt.label))
        os.replace(generated, targets[0])
    else:
        raise ValueError(fmt.key)


def _epub(src, target, stage, report):
    # PDF-imported drawing objects are not supported by Writer's EPUB exporter.
    # EPUB 3 fixed layout is the faithful alternative: standalone Cairo SVGs
    # retain page geometry, vectors and embedded images without rasterising a
    # second time or pretending the PDF has a recoverable paragraph structure.
    program = _program('pdftocairo')
    reader = PdfReader(src)
    manifest = []
    spine = []
    navigation = []
    with zipfile.ZipFile(target, 'w') as book:
        book.writestr('mimetype', 'application/epub+zip', compress_type=zipfile.ZIP_STORED)
        book.writestr('META-INF/container.xml',
                      '<?xml version="1.0"?><container version="1.0" '
                      'xmlns="urn:oasis:names:tc:opendocument:xmlns:container">'
                      '<rootfiles><rootfile full-path="OEBPS/content.opf" '
                      'media-type="application/oebps-package+xml"/></rootfiles></container>')
        for i, page in enumerate(reader.pages, 1):
            report.check()
            report(tr('Seite {i} / {total}…').format(i=i, total=len(reader.pages)))
            svg = os.path.join(stage, f'page-{i}.svg')
            _run(report, [program, '-svg', '-origpagesizes', '-f', str(i), '-l', str(i), src, svg])
            width, height = float(page.cropbox.width), float(page.cropbox.height)
            if page.rotation % 180:
                width, height = height, width
            label = escape(tr('Seite {p0}').format(p0=i))
            book.write(svg, f'OEBPS/page-{i}.svg', compress_type=zipfile.ZIP_DEFLATED)
            book.writestr(f'OEBPS/page-{i}.xhtml',
                          '<html xmlns="http://www.w3.org/1999/xhtml"><head>'
                          f'<title>{label}</title><meta name="viewport" content="width={width:g}, height={height:g}"/>'
                          '<style>html,body{margin:0;padding:0;}img{display:block;width:100%;height:100%;}</style>'
                          f'</head><body><img src="page-{i}.svg" alt="{label}"/></body></html>',
                          compress_type=zipfile.ZIP_DEFLATED)
            manifest += [f'<item id="p{i}" href="page-{i}.xhtml" media-type="application/xhtml+xml" properties="svg"/>',
                         f'<item id="s{i}" href="page-{i}.svg" media-type="image/svg+xml"/>']
            spine.append(f'<itemref idref="p{i}"/>')
            navigation.append(f'<li><a href="page-{i}.xhtml">{label}</a></li>')
        book.writestr('OEBPS/nav.xhtml',
                      '<html xmlns="http://www.w3.org/1999/xhtml" xmlns:epub="http://www.idpf.org/2007/ops">'
                      '<head><title>CopyShop</title></head><body><nav epub:type="toc"><ol>'
                      + ''.join(navigation) + '</ol></nav></body></html>')
        modified = datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')
        book.writestr('OEBPS/content.opf',
                      '<package xmlns="http://www.idpf.org/2007/opf" version="3.0" unique-identifier="book-id">'
                      '<metadata xmlns:dc="http://purl.org/dc/elements/1.1/">'
                      f'<dc:identifier id="book-id">urn:uuid:{uuid.uuid4()}</dc:identifier>'
                      '<dc:title>CopyShop</dc:title><dc:language>und</dc:language>'
                      f'<meta property="dcterms:modified">{modified}</meta>'
                      '<meta property="rendition:layout">pre-paginated</meta>'
                      '<meta property="rendition:spread">none</meta></metadata><manifest>'
                      '<item id="nav" href="nav.xhtml" media-type="application/xhtml+xml" properties="nav"/>'
                      + ''.join(manifest) + '</manifest><spine>' + ''.join(spine) + '</spine></package>')
    reader.close()


def _publish(staged, destinations, stage):
    # All outputs share a directory with staging. Keep the old inodes until
    # the whole batch has been replaced, so a later rename failure can roll
    # back earlier pages as well as preserving the page which failed.
    backups = {}
    published = []
    try:
        for i, destination in enumerate(destinations):
            if os.path.lexists(destination):
                backup = os.path.join(stage, f'backup-{i}')
                try:
                    os.link(destination, backup, follow_symlinks=False)
                except OSError:
                    # USB sticks commonly use FAT/exFAT, which cannot link.
                    shutil.copy2(destination, backup, follow_symlinks=False)
                backups[destination] = backup
        for file, destination in zip(staged, destinations):
            os.replace(file, destination)
            published.append(destination)
    except Exception:
        for destination in reversed(published):
            if destination in backups:
                os.replace(backups[destination], destination)
            else:
                os.unlink(destination)
        raise


def export_document(sources, path, fmt, report, dpi=300):
    """Stage everything before replacing any destination; failure/cancellation
    must leave existing files intact. Render one page at a time to bound memory.
    `sources` is captured on the GUI thread, so later page-manager edits cannot
    change a job already in flight. No viewer widgets are read from this worker.
    """
    if not sources:
        raise ValueError(tr('Keine PDF geladen.'))
    required = {'image': 'pdftoppm', 'cairo': 'pdftocairo', 'text': 'pdftotext',
                'html': 'pdftohtml', 'xml': 'pdftohtml', 'office': 'soffice', 'epub': 'pdftocairo'}
    _program(required[fmt.backend])
    destinations = output_paths(path, fmt, len(sources))
    with tempfile.TemporaryDirectory(prefix='.copyshop-export-', dir=os.path.dirname(os.path.abspath(path))) as stage:
        src = os.path.join(stage, 'document.pdf')
        report.check()
        _snapshot(sources, src, report)
        staged = [os.path.join(stage, f'output-{i}.{fmt.extensions[0]}') for i in range(len(destinations))]
        _convert(src, staged, fmt, dpi, stage, report, len(sources))
        for file in staged:
            if not os.path.isfile(file) or not os.path.getsize(file):
                raise RuntimeError(tr('Konvertierung fehlgeschlagen:\n{p0}').format(p0=fmt.label))
        report.check()
        _publish(staged, destinations, stage)
    return destinations
