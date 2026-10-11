"""Inputs offered by the picker must have real PDF conversion routes."""
import io
import os
import struct
import tempfile
from pathlib import Path
from subprocess import CompletedProcess
from unittest.mock import patch

from PIL import Image
from pypdf import PdfReader

from tests.support import _TMP, _app
from tools.input_formats import IMAGE_EXTS, OFFICE_EXTS, ALL_EXTS, _registry_extensions
from tools.jobs import Cancelled, null_progress
from tools.multi_open import convert_to_pdf, file_dialog_filter, images_to_pdf_bytes, classify
from tools.panels.img_pdf import ImgPdfPanel
from tools.viewer.merge import FileCard


def test_input_picker_exposes_new_image_office_and_postscript_families():
    first = file_dialog_filter().split(';;')[0]
    for ext in ALL_EXTS:
        assert '*' + ext in first
        assert classify('input' + ext.upper())
    assert {'.qoi', '.jp2', '.ppm', '.pgm', '.pbm', '.tga', '.ico', '.psd', '.mpo'} <= IMAGE_EXTS
    assert {'.fodt', '.fods', '.fodp', '.fodg', '.ott', '.ots', '.otp', '.otg',
            '.docm', '.xlsm', '.pptm', '.dotx', '.xltx', '.potx', '.ppsx',
            '.numbers', '.key', '.abw', '.wpd', '.vsdx', '.svgz'} <= OFFICE_EXTS
    assert classify('input.eps') == 'postscript'
    assert classify('input.ps') == 'postscript'
    assert classify('input.ai') == 'postscript'
    assert not ({'.bufr', '.grib', '.h5', '.hdf', '.mpeg', '.mov', '.zip', '.xxx'} & ALL_EXTS)
    panel = ImgPdfPanel()
    try:
        assert set(panel.img_list.extensions) == IMAGE_EXTS
    finally:
        panel.close()
        panel.deleteLater()
        _app.processEvents()


def test_registry_only_exposes_imports_with_a_pdf_exporter():
    xml = '''<oor:data xmlns:oor="http://openoffice.org/2001/registry">
    <oor:component-data oor:name="Types" oor:package="org.openoffice.TypeDetection"><node oor:name="Types">
    <node oor:name="sample"><prop oor:name="Extensions"><value>foo;bar baz</value></prop></node>
    <node oor:name="out"><prop oor:name="Extensions"><value>out</value></prop></node>
    <node oor:name="media"><prop oor:name="Extensions"><value>mov zip xxx *</value></prop></node>
    </node></oor:component-data>
    <oor:component-data oor:name="Filter" oor:package="org.openoffice.TypeDetection"><node oor:name="Filters">
    <node oor:name="pdf"><prop oor:name="Flags"><value>EXPORT</value></prop><prop oor:name="Type"><value>pdf_Portable_Document_Format</value></prop><prop oor:name="DocumentService"><value>Writer</value></prop></node>
    <node oor:name="in"><prop oor:name="Flags"><value>IMPORT</value></prop><prop oor:name="Type"><value>sample</value></prop><prop oor:name="DocumentService"><value>Writer</value></prop></node>
    <node oor:name="out"><prop oor:name="Flags"><value>EXPORT</value></prop><prop oor:name="Type"><value>out</value></prop><prop oor:name="DocumentService"><value>Writer</value></prop></node>
    <node oor:name="base"><prop oor:name="Flags"><value>IMPORT</value></prop><prop oor:name="Type"><value>out</value></prop><prop oor:name="DocumentService"><value>Base</value></prop></node>
    <node oor:name="media"><prop oor:name="Flags"><value>IMPORT</value></prop><prop oor:name="Type"><value>media</value></prop><prop oor:name="DocumentService"><value>Writer</value></prop></node>
    </node></oor:component-data></oor:data>'''
    with tempfile.TemporaryDirectory(dir=_TMP) as folder:
        Path(folder, 'filters.xcd').write_text(xml)
        assert _registry_extensions(folder) == {'.foo', '.bar', '.baz'}


def test_all_writable_image_families_can_be_read_back_as_pdf():
    Image.init()
    families = {Image.registered_extensions()[e] for e in IMAGE_EXTS} & Image.SAVE.keys()
    with tempfile.TemporaryDirectory(dir=_TMP) as folder:
        for family in sorted(families):
            suffix = next(e for e in IMAGE_EXTS if Image.registered_extensions()[e] == family)
            path = os.path.join(folder, 'image' + suffix)
            mode = {'BLP': 'P', 'MSP': '1', 'XBM': '1'}.get(family, 'RGB')
            with Image.new(mode, (64, 48), 1) as image:
                image.save(path, family)
            out = convert_to_pdf(path, folder)
            assert len(PdfReader(out).pages) == 1, family
        # A JPEG alias must keep the same main-image-only boundary as .jpg.
        path = os.path.join(folder, 'phone.jfif')
        Image.new('RGB', (64, 48)).save(path, 'MPO', save_all=True,
                                       append_images=[Image.new('RGB', (32, 24))])
        assert len(PdfReader(io.BytesIO(images_to_pdf_bytes([path]))).pages) == 1
        mpo = os.path.join(folder, 'phone.mpo')
        Path(mpo).write_bytes(Path(path).read_bytes())
        assert len(PdfReader(convert_to_pdf(mpo, folder)).pages) == 1


def test_floating_point_image_gets_a_nonblank_display_range():
    with tempfile.TemporaryDirectory(dir=_TMP) as folder:
        path = os.path.join(folder, 'image.pfm')
        Path(path).write_bytes(b'Pf\n8 8\n-1.0\n' + struct.pack('<64f', *(i / 63 for i in range(64))))
        out = convert_to_pdf(path, folder)
        image = PdfReader(out).pages[0].images[0].image
        assert image.getextrema() == (0, 255)


def test_postscript_and_eps_convert_with_their_original_page_geometry():
    with tempfile.TemporaryDirectory(dir=_TMP) as folder:
        eps = Path(folder, 'test.eps')
        eps.write_text('%!PS-Adobe-3.0 EPSF-3.0\n%%BoundingBox: 0 0 90 120\n/Helvetica findfont 12 scalefont setfont\n10 60 moveto (EPS PAGE) show\nshowpage\n%%EOF\n')
        ps = Path(folder, 'test.ps')
        ps.write_text('%!PS-Adobe-3.0\n<< /PageSize [90 120] >> setpagedevice\n/Helvetica findfont 12 scalefont setfont\n10 60 moveto (FIRST) show showpage\n10 60 moveto (SECOND) show showpage\n')
        for path, count in ((eps, 1), (ps, 2)):
            out = convert_to_pdf(str(path), folder)
            reader = PdfReader(out)
            assert len(reader.pages) == count
            assert tuple(reader.pages[0].mediabox)[2:] == (90, 120)
            assert reader.pages[0].extract_text().strip() in ('EPS PAGE', 'FIRST')


def test_same_named_inputs_do_not_overwrite_each_other_or_reuse_stale_pdfs():
    with tempfile.TemporaryDirectory(dir=_TMP) as folder:
        a = Path(folder, 'same.png')
        b = Path(folder, 'same.bmp')
        Image.new('RGB', (64, 48), 'red').save(a)
        Image.new('RGB', (80, 60), 'blue').save(b)
        one = convert_to_pdf(str(a), folder)
        two = convert_to_pdf(str(b), folder)
        assert one != two and os.path.isfile(one) and os.path.isfile(two)
        Path(folder, 'same.docx').write_bytes(b'not a valid document')
        report = null_progress()
        with patch('tools.multi_open.shutil.which', return_value='/usr/bin/soffice'), \
             patch.object(report, 'run', return_value=CompletedProcess([], 0, b'', b'')):
            try:
                convert_to_pdf(str(Path(folder, 'same.docx')), folder, report)
            except RuntimeError:
                pass
            else:
                raise AssertionError('stale PDF accepted for a failed conversion')
        assert os.path.isfile(one) and os.path.isfile(two)
        assert len(list(Path(folder).glob('input-*'))) == 2


def test_office_input_uses_an_isolated_profile_and_cancellation_cleans_up():
    with tempfile.TemporaryDirectory(dir=_TMP) as folder:
        source = Path(folder, 'input.odt')
        source.write_bytes(b'input')
        report = null_progress()
        def run(command, **kwargs):
            assert any(arg.startswith('-env:UserInstallation=file:///') for arg in command)
            directory = command[command.index('--outdir') + 1]
            with open(os.path.join(directory, 'input.pdf'), 'wb') as out:
                out.write(b'converted')
            return CompletedProcess(command, 0, b'', b'')
        with patch('tools.multi_open.shutil.which', return_value='/usr/bin/soffice'), \
             patch.object(report, 'run', side_effect=run):
            out = convert_to_pdf(str(source), folder, report)
            assert Path(out).read_bytes() == b'converted'
        with patch('tools.multi_open.shutil.which', return_value='/usr/bin/soffice'), \
             patch.object(report, 'run', side_effect=Cancelled()):
            try:
                convert_to_pdf(str(source), folder, report)
            except Cancelled:
                pass
            else:
                raise AssertionError('cancellation ignored')
        assert len(list(Path(folder).glob('input-*'))) == 1


def test_new_image_format_gets_a_merge_thumbnail():
    with tempfile.TemporaryDirectory(dir=_TMP) as folder:
        path = os.path.join(folder, 'test.qoi')
        Image.new('RGB', (64, 48), 'red').save(path, 'QOI')
        card = FileCard(0, path)
        try:
            assert card._pixmap is not None and not card._pixmap.isNull()
        finally:
            card.close()
            card.deleteLater()
            _app.processEvents()


def test_illustrator_accepts_pdf_and_postscript_content():
    from reportlab.pdfgen import canvas
    with tempfile.TemporaryDirectory(dir=_TMP) as folder:
        pdf_ai = os.path.join(folder, 'pdf-compatible.ai')
        c = canvas.Canvas(pdf_ai, pagesize=(90, 120))
        c.drawString(10, 60, 'VECTOR'); c.showPage(); c.save()
        assert convert_to_pdf(pdf_ai, folder) == pdf_ai
        eps_ai = Path(folder, 'postscript.ai')
        eps_ai.write_text('%!PS-Adobe-3.0 EPSF-3.0\n%%BoundingBox: 0 0 90 120\n/Helvetica findfont 12 scalefont setfont\n10 60 moveto (VECTOR) show\nshowpage\n%%EOF\n')
        out = convert_to_pdf(str(eps_ai), folder)
        reader = PdfReader(out)
        assert tuple(reader.pages[0].mediabox)[2:] == (90, 120)
        assert reader.pages[0].extract_text().strip() == 'VECTOR'
