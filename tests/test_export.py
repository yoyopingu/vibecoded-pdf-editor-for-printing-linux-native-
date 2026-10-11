"""Save As converters produce real files and preserve the displayed page view."""
import os
import tempfile
import zipfile
from pathlib import Path
from unittest.mock import patch
from xml.etree import ElementTree

from PIL import Image
from pypdf import PdfReader, PdfWriter
from reportlab.pdfgen import canvas
from PyQt6.QtWidgets import QMessageBox

from tests.support import _TMP, _app, _open_single_view, _wait_for
from tools.export import export_formats, resolve_export, output_paths, export_document, _snapshot
from tools.jobs import Cancelled, null_progress


def _fixture(folder):
    path = os.path.join(folder, 'source.pdf')
    c = canvas.Canvas(path, pagesize=(90, 120))
    for i, colour in enumerate(((1, 0, 0), (0, 0, 1))):
        c.setFillColorRGB(*colour)
        c.rect(0, 0, 90, 120, fill=1, stroke=0)
        c.setFillColorRGB(0, 0, 0)
        c.drawString(10, 60, f'PAGE {i + 1}')
        c.showPage()
    c.save()
    return path


def test_format_resolution_and_numbered_names():
    formats = {f.key: f for f in export_formats()}
    assert {'pdf', 'odt', 'docx', 'pptx', 'odg', 'txt', 'html', 'svg', 'ps', 'png'} <= formats.keys()
    assert resolve_export('/tmp/out', formats['docx'].dialog_filter()) == ('/tmp/out.docx', formats['docx'])
    assert resolve_export('/tmp/out.pdf', formats['png'].dialog_filter()) == ('/tmp/out.png', formats['png'])
    assert resolve_export('/tmp/out.JPEG')[1].key == 'jpeg'
    assert resolve_export('/tmp/out.txt', formats['pdf'].dialog_filter())[1].key == 'txt'
    assert output_paths('/tmp/a.png', formats['png'], 2) == ['/tmp/a_001.png', '/tmp/a_002.png']
    assert output_paths('/tmp/a.tiff', formats['tiff'], 2) == ['/tmp/a.tiff']
    try:
        resolve_export('/tmp/out.nonsense')
    except ValueError:
        pass
    else:
        raise AssertionError('unknown extension silently accepted')


def test_snapshot_keeps_order_foreign_pages_and_independent_duplicate_rotations():
    with tempfile.TemporaryDirectory(dir=_TMP) as folder:
        source = _fixture(folder)
        other = os.path.join(folder, 'foreign.pdf')
        c = canvas.Canvas(other, pagesize=(30, 40))
        c.drawString(1, 15, 'FOREIGN'); c.showPage(); c.save()
        out = os.path.join(folder, 'view.pdf')
        _snapshot(((source, 1, 90), (source, 1, 0), (other, 0, 0), (source, 0, 180)), out, null_progress())
        reader = PdfReader(out)
        assert [p.extract_text().strip() for p in reader.pages] == ['PAGE 2', 'PAGE 2', 'FOREIGN', 'PAGE 1']
        assert [p.rotation for p in reader.pages] == [90, 0, 0, 180]
        assert tuple(reader.pages[2].mediabox)[2:] == (30, 40)
        assert PdfReader(source).pages[1].rotation == 0


def test_every_installed_image_encoder_and_multipage_tiff():
    with tempfile.TemporaryDirectory(dir=_TMP) as folder:
        source = _fixture(folder)
        sources = ((source, 1, 90), (source, 0, 0))
        for fmt in export_formats():
            if fmt.backend != 'image':
                continue
            out = os.path.join(folder, 'image.' + fmt.extensions[0])
            paths = export_document(sources, out, fmt, null_progress(), dpi=72)
            assert paths == output_paths(out, fmt, 2)
            with Image.open(paths[0]) as first:
                assert first.size == (120, 90), (fmt.key, first.size)
                if fmt.key == 'tiff':
                    assert first.n_frames == 2
                    first.seek(1)
                    assert first.size == (90, 120)
            if fmt.per_page:
                with Image.open(paths[1]) as second:
                    assert second.size == (90, 120), fmt.key
        assert not list(Path(folder).glob('.copyshop-export-*'))


def test_text_web_vector_and_postscript_converters():
    with tempfile.TemporaryDirectory(dir=_TMP) as folder:
        source = _fixture(folder)
        sources = ((source, 1, 90), (source, 0, 0))
        for fmt in export_formats():
            if fmt.backend not in ('text', 'html', 'xml', 'cairo'):
                continue
            out = os.path.join(folder, 'output.' + fmt.extensions[0])
            paths = export_document(sources, out, fmt, null_progress())
            assert all(os.path.getsize(p) > 0 for p in paths), fmt.key
            data = Path(paths[0]).read_text(errors='replace')
            if fmt.key in ('txt', 'html', 'xml'):
                assert data.index('PAGE 2') < data.index('PAGE 1'), fmt.key
            if fmt.key == 'svg':
                root = ElementTree.parse(paths[0]).getroot()
                assert root.tag.endswith('svg')
                assert root.attrib['width'] == '120pt' and root.attrib['height'] == '90pt'
            if fmt.key in ('ps', 'eps'):
                assert data.startswith('%!PS-Adobe')
            if fmt.key == 'html':
                assert not list(Path(folder).glob('*-*.png')), 'image sidecars escaped staging'


def test_failure_cancellation_and_missing_program_leave_existing_output_intact():
    fmt = next(f for f in export_formats() if f.key == 'png')
    with tempfile.TemporaryDirectory(dir=_TMP) as folder:
        source = _fixture(folder)
        out = os.path.join(folder, 'out.png')
        Path(out).write_bytes(b'original')
        with patch('tools.export.shutil.which', return_value=None):
            try:
                export_document(((source, 0, 0),), out, fmt, null_progress())
            except RuntimeError as e:
                assert 'pdftoppm' in str(e)
            else:
                raise AssertionError('missing binary accepted')
        assert Path(out).read_bytes() == b'original'
        with patch('tools.export._convert', side_effect=Cancelled()):
            try:
                export_document(((source, 0, 0),), out, fmt, null_progress())
            except Cancelled:
                pass
            else:
                raise AssertionError('cancellation ignored')
        assert Path(out).read_bytes() == b'original'
        assert not list(Path(folder).glob('.copyshop-export-*'))


def test_office_uses_correct_pdf_importer_isolated_profile_and_checks_output():
    with tempfile.TemporaryDirectory(dir=_TMP) as folder:
        source = _fixture(folder)
        report = null_progress()
        seen = []
        real_run = report.run
        def run(command, **kwargs):
            seen.append(command)
            if '--outdir' not in command:
                return real_run(command, **kwargs)
            dest = command[command.index('--outdir') + 1]
            ext = command[command.index('--convert-to') + 1].split(':')[0]
            Path(dest, 'document.' + ext).write_bytes(b'converted')
            from subprocess import CompletedProcess
            return CompletedProcess(command, 0, b'', b'')
        with patch('tools.export.shutil.which', side_effect=lambda name: '/usr/bin/soffice' if name == 'soffice' else '/usr/bin/' + name), patch.object(report, 'run', side_effect=run):
            for fmt in export_formats():
                if fmt.backend != 'office':
                    continue
                out = os.path.join(folder, 'output.' + fmt.extensions[0])
                export_document(((source, 0, 0),), out, fmt, report)
                assert Path(out).read_bytes() == b'converted'
                expected_filter = 'HTML (StarWriter)' if fmt.key == 'rtf' else fmt.input_filter
                assert '--infilter=' + expected_filter in seen[-1]
                assert any(a.startswith('-env:UserInstallation=file:///') for a in seen[-1])
                assert fmt.extensions[0] + ':' + fmt.output_filter in seen[-1]


def test_save_as_routes_formats_honours_selection_and_keeps_pdf_tab():
    with tempfile.TemporaryDirectory(dir=_TMP) as folder:
        source = _fixture(folder)
        viewer, single = _open_single_view(source)
        tab = viewer.tabs.currentWidget()
        original_order = list(tab.model.order)
        try:
            viewer._toggle_manage()
            assert _wait_for(tab.in_manage_mode)
            tab.model.order.reverse()
            tab.model.rotations[1] = 90
            tab.model.selected = {1}
            out = os.path.join(folder, 'selected.png')
            fmt = next(f for f in export_formats() if f.key == 'png')
            with patch('tools.viewer.panel.QFileDialog.getSaveFileName', return_value=(out, fmt.dialog_filter())) as picker, \
                 patch('tools.viewer.panel.QInputDialog.getInt', return_value=(72, True)), \
                 patch('tools.viewer.panel.QMessageBox.warning') as warning:
                viewer._save_as_current()
                assert _wait_for(lambda: getattr(tab, '_export_job', None) and tab._export_job.is_finished)
                _app.processEvents()
                assert os.path.isfile(out)
                assert not warning.called
                assert '*.docx' in picker.call_args.args[3] and '*.svg' in picker.call_args.args[3]
                assert tab.pdf_path == source
                assert tab.model.order == list(reversed(original_order))
                assert tab.model.rotations[1] == 90
                with Image.open(out) as image:
                    assert image.size == (120, 90)
                    assert image.convert('RGB').getpixel((10, 10))[2] > 240
            # Cancel the generated-file confirmation before launching any work.
            with patch('tools.viewer.panel.QMessageBox.question', return_value=QMessageBox.StandardButton.Cancel):
                old_job = tab._export_job
                viewer._export_current(tab, os.path.join(folder, 'all.png'), fmt)
                assert tab._export_job is old_job
        finally:
            viewer._close_tab(0)
            viewer.close()
            viewer.deleteLater()
            _app.processEvents()


def test_multi_file_publish_failure_rolls_back_already_replaced_pages():
    fmt = next(f for f in export_formats() if f.key == 'png')
    with tempfile.TemporaryDirectory(dir=_TMP) as folder:
        source = _fixture(folder)
        out = os.path.join(folder, 'out.png')
        targets = output_paths(out, fmt, 2)
        for i, path in enumerate(targets):
            Path(path).write_bytes(f'original-{i}'.encode())
        real_replace = os.replace
        def replace(src, dst):
            if dst == targets[1]:
                raise OSError('second page could not be replaced')
            return real_replace(src, dst)
        with patch('tools.export.os.replace', side_effect=replace):
            try:
                export_document(((source, 0, 0), (source, 1, 0)), out, fmt, null_progress(), dpi=72)
            except OSError:
                pass
            else:
                raise AssertionError('failed replacement was ignored')
        assert [Path(p).read_bytes() for p in targets] == [b'original-0', b'original-1']


def test_snapshot_preserves_crop_box_and_form_values():
    with tempfile.TemporaryDirectory(dir=_TMP) as folder:
        source = os.path.join(folder, 'form.pdf')
        c = canvas.Canvas(source, pagesize=(200, 150))
        c.acroForm.textfield(name='name', value='Filled value', x=20, y=40, width=100, height=30)
        c.showPage(); c.save()
        reader = PdfReader(source)
        cropped = os.path.join(folder, 'cropped.pdf')
        writer = PdfWriter(clone_from=reader)
        writer.pages[0].cropbox.lower_left = (10, 20)
        writer.pages[0].cropbox.upper_right = (180, 140)
        writer.write(cropped)
        writer.close()
        out = os.path.join(folder, 'view.pdf')
        _snapshot(((cropped, 0, 90),), out, null_progress())
        result = PdfReader(out)
        assert result.get_fields()['name']['/V'] == 'Filled value'
        assert tuple(result.pages[0].mediabox) == (10, 20, 180, 140)
        assert result.pages[0].rotation == 90


def test_office_success_exit_without_output_is_still_a_failure():
    from subprocess import CompletedProcess
    fmt = next(f for f in export_formats() if f.key == 'docx')
    with tempfile.TemporaryDirectory(dir=_TMP) as folder:
        source = _fixture(folder)
        out = os.path.join(folder, 'out.docx')
        Path(out).write_bytes(b'original')
        report = null_progress()
        with patch('tools.export._program', return_value='/usr/bin/soffice'), \
             patch.object(report, 'run', return_value=CompletedProcess([], 0, b'', b'')):
            try:
                export_document(((source, 0, 0),), out, fmt, report)
            except RuntimeError:
                pass
            else:
                raise AssertionError('LibreOffice zero exit without output was accepted')
        assert Path(out).read_bytes() == b'original'


def test_replacing_an_export_on_filesystems_without_hardlinks():
    fmt = next(f for f in export_formats() if f.key == 'txt')
    with tempfile.TemporaryDirectory(dir=_TMP) as folder:
        source = _fixture(folder)
        out = os.path.join(folder, 'out.txt')
        Path(out).write_text('old output')
        with patch('tools.export.os.link', side_effect=OSError('hardlinks unavailable')):
            export_document(((source, 1, 0),), out, fmt, null_progress())
        assert 'PAGE 2' in Path(out).read_text()


def test_epub_contains_all_pages_with_fixed_geometry_and_valid_references():
    fmt = next(f for f in export_formats() if f.key == 'epub')
    with tempfile.TemporaryDirectory(dir=_TMP) as folder:
        source = _fixture(folder)
        out = os.path.join(folder, 'book.epub')
        export_document(((source, 1, 90), (source, 0, 0)), out, fmt, null_progress())
        with zipfile.ZipFile(out) as book:
            first = book.infolist()[0]
            assert first.filename == 'mimetype' and first.compress_type == zipfile.ZIP_STORED
            assert book.read('mimetype') == b'application/epub+zip'
            root = ElementTree.fromstring(book.read('OEBPS/content.opf'))
            ns = {'opf': 'http://www.idpf.org/2007/opf', 'x': 'http://www.w3.org/1999/xhtml'}
            items = {item.attrib['id']: item for item in root.findall('opf:manifest/opf:item', ns)}
            for item in items.values():
                assert 'OEBPS/' + item.attrib['href'] in book.namelist()
            spine = root.findall('opf:spine/opf:itemref', ns)
            assert [item.attrib['idref'] for item in spine] == ['p1', 'p2']
            assert items['p1'].attrib['properties'] == 'svg'
            for i, dimensions in ((1, (120, 90)), (2, (90, 120))):
                page = ElementTree.fromstring(book.read(f'OEBPS/page-{i}.xhtml'))
                viewport = page.find('x:head/x:meta', ns).attrib['content']
                assert viewport == f'width={dimensions[0]}, height={dimensions[1]}'
                svg = ElementTree.fromstring(book.read(f'OEBPS/page-{i}.svg'))
                assert svg.attrib['width'] == f'{dimensions[0]}pt'
                assert svg.attrib['height'] == f'{dimensions[1]}pt'


def test_failed_pdf_save_as_does_not_delete_existing_destination():
    with tempfile.TemporaryDirectory(dir=_TMP) as folder:
        source = _fixture(folder)
        viewer, single = _open_single_view(source)
        tab = viewer.tabs.currentWidget()
        destination = os.path.join(folder, 'existing.pdf')
        Path(destination).write_bytes(b'existing document')
        try:
            with patch('tools.viewer.panel.QFileDialog.getSaveFileName', return_value=(destination, '')), \
                 patch.object(tab, 'save_to', side_effect=OSError('disk full')):
                viewer._save_as_current()
            assert Path(destination).read_bytes() == b'existing document'
            assert tab.pdf_path == source
        finally:
            viewer._close_tab(0)
            viewer.close()
            viewer.deleteLater()
            _app.processEvents()
