"""
Turn an uploaded prescription (image or PDF) into pages the OCR engine can read.

Digital PDFs that already carry a text layer skip OCR entirely; scanned PDFs
and photos are rasterised to RGB numpy arrays.
"""

from __future__ import annotations

import io
from dataclasses import dataclass, field

import numpy as np

MAX_UPLOAD_BYTES = 10 * 1024 * 1024
MAX_PAGES = 5
MAX_IMAGE_SIDE = 2400
PDF_RENDER_DPI = 200
# A page with fewer extractable characters than this is treated as scanned.
MIN_TEXT_LAYER_CHARS = 15


class DocumentError(ValueError):
    """Raised for uploads that cannot be read as a prescription."""


@dataclass
class PageInput:
    page: int
    image: np.ndarray | None = None
    text_lines: list[str] = field(default_factory=list)

    @property
    def has_text_layer(self) -> bool:
        return self.image is None


def sniff_kind(data: bytes) -> str:
    """Identify the upload by magic bytes; client content types are not trusted."""
    if data.startswith(b"%PDF"):
        return "pdf"
    if data.startswith(b"\xff\xd8\xff"):
        return "image"
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image"
    if data.startswith((b"BM", b"II*\x00", b"MM\x00*")):
        return "image"
    raise DocumentError("Unsupported file type. Upload a JPG, PNG, WEBP, BMP, TIFF or PDF.")


def load_document(data: bytes) -> list[PageInput]:
    if not data:
        raise DocumentError("The uploaded file is empty.")
    if len(data) > MAX_UPLOAD_BYTES:
        raise DocumentError(
            f"File is too large. Maximum size is {MAX_UPLOAD_BYTES // (1024 * 1024)} MB."
        )
    kind = sniff_kind(data)
    if kind == "pdf":
        return _load_pdf(data)
    return [PageInput(page=1, image=_load_image(data))]


def _load_image(data: bytes) -> np.ndarray:
    from PIL import Image, ImageOps, UnidentifiedImageError

    try:
        img = Image.open(io.BytesIO(data))
        # Phone photos are often stored sideways with an EXIF rotation flag.
        img = ImageOps.exif_transpose(img)
        img = img.convert("RGB")
    except (UnidentifiedImageError, OSError) as exc:
        raise DocumentError("The image could not be read.") from exc

    longest = max(img.size)
    if longest > MAX_IMAGE_SIDE:
        scale = MAX_IMAGE_SIDE / longest
        img = img.resize((round(img.width * scale), round(img.height * scale)))
    return np.asarray(img)


def _load_pdf(data: bytes) -> list[PageInput]:
    import pymupdf

    try:
        doc = pymupdf.open(stream=data, filetype="pdf")
    except (pymupdf.FileDataError, RuntimeError) as exc:
        raise DocumentError("The PDF could not be read.") from exc

    pages: list[PageInput] = []
    with doc:
        if doc.page_count == 0:
            raise DocumentError("The PDF has no pages.")
        if doc.needs_pass:
            raise DocumentError("Password-protected PDFs are not supported.")
        for index in range(min(doc.page_count, MAX_PAGES)):
            page = doc.load_page(index)
            text = page.get_text("text")
            if len(text.strip()) >= MIN_TEXT_LAYER_CHARS:
                lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
                pages.append(PageInput(page=index + 1, text_lines=lines))
                continue
            pix = page.get_pixmap(dpi=PDF_RENDER_DPI, colorspace=pymupdf.csRGB, alpha=False)
            image = np.frombuffer(pix.samples, dtype=np.uint8).reshape(pix.height, pix.width, 3)
            pages.append(PageInput(page=index + 1, image=image.copy()))
    return pages
