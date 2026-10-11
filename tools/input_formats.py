"""File inputs backed by real decoders and PDF-capable Office import filters.

Use installed registries so a newly available codec/filter also appears in the
picker. Pillow's identification-only plugins and LibreOffice's media/archive
and placeholder extensions are not document converters.
"""
import re
import shutil
from pathlib import Path
from xml.etree import ElementTree

from PIL import Image, ImageFile


# Standard LibreOffice import families, also shown before the optional binary
# is installed. Installed registry data replaces this fallback when available.
# Source: LibreOffice's TypeDetection configuration and conversion-filter table.
_OFFICE_FALLBACK = '''
123 602 abw cdr cgm cmx csv cwk dbf dif doc docm docx dot dotm dotx dps dpt dxf
emf emz et ett fb2 fh fh1 fh2 fh3 fh4 fh5 fh6 fh7 fh8 fh9 fh10 fh11 fodg fodp
fods fodt gnm gnumeric htm html hwp jfif jif json key lrf lwp markdown mcw md
met mml mp mw mwd nx^d numbers odf odg odm odp ods odt otg oth otm otp ots ott p65 pages pdb
pct pict pm pm6 pmd pot potm potx pps ppsx ppt pptm pptx psw pub qxd qxt rtf
sda sdc sdd sdw slk stc std sti stw svg svgz svm sxc sxd sxg sxi sxm sxw sylk tab
tsv txt uof uop uos uot vdx vsd vsdm vsdx vstx wb1 wb2 wdb wk1 wk3 wk4 wks
wmf wmz wn wpd wpg wps wpt wq1 wq2 wri xhtml xlc xlk xlm xls xlsb xlsm xlsx
xlt xltm xltx xlw xml zabw zmf
'''
_EXCLUDED_OFFICE = {'pdf', 'zip', 'mov', 'xxx', '*'}
_OOR = '{http://openoffice.org/2001/registry}'


def image_extensions():
    Image.init()
    formats = set()
    for name, (factory, _) in Image.OPEN.items():
        if isinstance(factory, type) and issubclass(factory, ImageFile.StubImageFile):
            continue
        # MPEG only reads a header; PostScript has a vector-preserving GS path.
        if name not in {'MPEG', 'EPS'}:
            formats.add(name)
    # MPO is decoded through JPEG's factory, so it has no separate OPEN entry.
    if 'JPEG' in formats:
        formats.add('MPO')
    return {ext.lower() for ext, fmt in Image.registered_extensions().items()
            if fmt in formats}


def _registry_extensions(directory):
    types, filters = {}, {}
    for path in Path(directory).glob('*.xcd'):
        try:
            root = ElementTree.parse(path).getroot()
        except (OSError, ElementTree.ParseError):
            continue
        for component in root:
            if (component.tag != _OOR + 'component-data'
                    or component.get(_OOR + 'package') != 'org.openoffice.TypeDetection'):
                continue
            for group in component.findall('node'):
                kind = group.get(_OOR + 'name')
                if kind not in ('Types', 'Filters'):
                    continue
                for node in group.findall('node'):
                    props = {p.get(_OOR + 'name'): ' '.join(v.text or '' for v in p.findall('value'))
                             for p in node.findall('prop')}
                    target = types if kind == 'Types' else filters
                    target.setdefault(node.get(_OOR + 'name'), {}).update(props)
    # Only applications which actually have a PDF exporter qualify. This also
    # excludes Base/database documents, which cannot be printed this way.
    services = {f.get('DocumentService') for f in filters.values()
                if 'EXPORT' in f.get('Flags', '').split()
                and f.get('Type') == 'pdf_Portable_Document_Format'}
    extensions = set()
    for f in filters.values():
        if ('IMPORT' not in f.get('Flags', '').split()
                or f.get('DocumentService') not in services):
            continue
        for ext in re.split(r'[;\s]+', types.get(f.get('Type'), {}).get('Extensions', '')):
            ext = ext.lower()
            if ext not in _EXCLUDED_OFFICE and re.fullmatch(r'[a-z0-9][a-z0-9_^+-]*', ext):
                extensions.add('.' + ext)
    return extensions


def office_extensions():
    binary = shutil.which('soffice') or shutil.which('libreoffice')
    if binary:
        program = Path(binary).resolve().parent
        for directory in (program.parent / 'share' / 'registry',
                          program.parent / 'lib' / 'libreoffice' / 'share' / 'registry'):
            discovered = _registry_extensions(directory)
            if discovered:
                return discovered
    return {'.' + ext for ext in _OFFICE_FALLBACK.split()}


IMAGE_EXTS = image_extensions()
POSTSCRIPT_EXTS = {'.ps', '.eps', '.epsf', '.epsi', '.ai'}
OFFICE_EXTS = office_extensions() - IMAGE_EXTS - POSTSCRIPT_EXTS
PDF_EXT = {'.pdf'}
ALL_EXTS = PDF_EXT | IMAGE_EXTS | OFFICE_EXTS | POSTSCRIPT_EXTS
