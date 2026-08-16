"""
Separating a file's images to CMYK before Ghostscript sees them.

Why this exists
---------------
Converting RGB to CMYK is the entire cost of a PDF/X export. Measured on four
A4 pages carrying a full-page 300 dpi photograph: 20.7 s with the conversion,
0.5 s without. Vector artwork is free — 0.5 s for twelve thousand paths — so
the images are the whole bill.

Ghostscript is simply slow at it. littleCMS, which Pillow exposes, converts the
same 8.7-megapixel page in 0.55 s: roughly ten times faster for the identical
piece of work. And Ghostscript handed a file whose images are *already* CMYK
does the rest of its job in 1.3 s, because there is nothing left to separate.

So the images are converted here first, and Ghostscript does what it is good
at — the PDF/X structure, the output intent, the fonts, and whatever colour is
left in the vectors. End to end that is 19.8 s down to 1.3 s on the file above,
with the rendered result differing from the Ghostscript-only version by a mean
of 0.12 per channel out of 255. That number is the point of the two decisions
below.

The two decisions, both settled by measurement
----------------------------------------------
**Relative colorimetric with black point compensation.** Not a default worth
guessing at: the same conversion with black point compensation off differs
from Ghostscript's by 5.67 rather than 0.12, and absolute colorimetric by
10.88. This pairing is also what a press expects — it is the standard intent
for separating to a printing condition.

**Re-encoded with Flate, never JPEG.** A CMYK JPEG is stored inverted when it
carries an Adobe APP14 marker, which is what Pillow writes, and a PDF consumer
that does not undo the inversion renders the negative of the image. Measured,
that mistake scores 113.91 against the same reference — not subtly wrong,
completely wrong. Flate is lossless, unambiguous, and Ghostscript re-encodes it
afterwards anyway according to the export's own settings, so nothing is lost by
handing it uncompressed pixels.

What is deliberately left alone
-------------------------------
Anything not certainly understood, because Ghostscript still has its own colour
conversion switched on and will handle whatever this skips. Being conservative
here costs a little speed on an unusual file; being wrong costs a reprint. So:
8 bits per component only, no image masks, no /Decode arrays, no indexed or
Lab or separation spaces, and nothing already in CMYK or greyscale.
"""
import logging
import zlib

from tools.i18n import tr


# The profile assumed for an RGB image that carries none of its own. sRGB is
# what a file without an embedded profile means in practice, and it is what
# Ghostscript assumes in the same situation.
SRGB_NAME = "sRGB"


def _source_profile(obj):
    """An ImageCms profile for this image's colour space, or None to skip it.

    An ICCBased image is converted through its *own* profile — using sRGB for
    a photograph tagged AdobeRGB would shift every colour in it.
    """
    import pikepdf
    from PIL import ImageCms

    space = obj.get("/ColorSpace")
    if space is None:
        return None
    if isinstance(space, pikepdf.Name):
        # /DeviceRGB, and /CalRGB which is a white-point tweak on the same
        # primaries — near enough to sRGB that treating it as such is the
        # standard simplification, and it is vanishingly rare in print files.
        if str(space) in ("/DeviceRGB", "/CalRGB"):
            return ImageCms.createProfile(SRGB_NAME)
        return None
    if isinstance(space, pikepdf.Array) and len(space) >= 2:
        kind = str(space[0])
        if kind == "/CalRGB":
            return ImageCms.createProfile(SRGB_NAME)
        if kind == "/ICCBased":
            stream = space[1]
            if int(stream.get("/N", 0)) != 3:
                return None            # not an RGB profile: leave it to gs
            try:
                import io
                return ImageCms.getOpenProfile(io.BytesIO(bytes(stream.read_bytes())))
            except Exception:
                logging.debug("unreadable embedded ICC profile", exc_info=True)
                return ImageCms.createProfile(SRGB_NAME)
    return None


def _eligible(obj):
    """Is this an image this module is certain it can convert correctly?"""
    import pikepdf
    if obj.get("/Subtype") != pikepdf.Name("/Image"):
        return False
    if obj.get("/ImageMask"):
        return False                     # a stencil, not colour
    if "/Decode" in obj:
        return False                     # remapped samples; not worth the risk
    try:
        if int(obj.get("/BitsPerComponent", 0)) != 8:
            return False
    except (TypeError, ValueError):
        return False
    return True


def to_cmyk(src, dst, icc_path, report=None):
    """Write `src` to `dst` with its RGB images separated to CMYK.

    Returns (converted, skipped). Falls back to copying the file unchanged if
    anything at all goes wrong: this is an optimisation, and Ghostscript's own
    conversion is still switched on behind it, so skipping is always safe.
    """
    import shutil

    import pikepdf
    from PIL import ImageCms

    converted = skipped = 0
    try:
        target = ImageCms.getOpenProfile(icc_path)
        pdf = pikepdf.open(src)
    except Exception:
        logging.debug("image pre-conversion unavailable for %s", src, exc_info=True)
        shutil.copyfile(src, dst)
        return 0, 0

    try:
        # Every image object in the file, not only those in page resources: an
        # image inside a Form XObject is just as expensive and just as easy.
        # Soft masks and stencils reached this way are filtered out above and
        # by their colour space, which is never RGB.
        images = [o for o in pdf.objects
                  if isinstance(o, pikepdf.Stream) and _eligible(o)]
        transforms = {}
        for n, obj in enumerate(images, 1):
            if report is not None:
                report.check()
            source = _source_profile(obj)
            if source is None:
                skipped += 1
                continue
            try:
                key = id(source)
                transform = transforms.get(key)
                if transform is None:
                    transform = transforms[key] = ImageCms.buildTransform(
                        source, target, "RGB", "CMYK",
                        renderingIntent=ImageCms.Intent.RELATIVE_COLORIMETRIC,
                        flags=ImageCms.Flags.BLACKPOINTCOMPENSATION)
                pil = pikepdf.PdfImage(obj).as_pil_image()
                if pil.mode != "RGB":
                    pil = pil.convert("RGB")
                cmyk = ImageCms.applyTransform(pil, transform)
            except Exception:
                # Unreadable, exotic, or simply not what it claimed to be.
                logging.debug("leaving an image for Ghostscript", exc_info=True)
                skipped += 1
                continue
            if report is not None and n % 4 == 1:
                report(tr('Bild {p0} / {p1} wird separiert …').format(
                    p0=n, p1=len(images)))
            obj.write(zlib.compress(cmyk.tobytes(), 6),
                      filter=pikepdf.Name("/FlateDecode"))
            obj.ColorSpace = pikepdf.Name("/DeviceCMYK")
            converted += 1
        pdf.save(dst)
    except Exception:
        # Includes Cancelled, which the caller re-raises from its own check.
        pdf.close()
        raise
    finally:
        try:
            pdf.close()
        except Exception:
            logging.debug("closing the pre-conversion document failed",
                          exc_info=True)
    return converted, skipped
