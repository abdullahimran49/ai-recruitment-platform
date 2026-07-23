"""PDF text extraction with an OCR fallback for scanned pages.

extract_text() takes raw PDF bytes (as Streamlit gives us) and returns clean
text. Text-based PDFs go through PyMuPDF; pages that come back nearly empty are
assumed to be scanned images and routed to OCR when the optional deps
(pytesseract + poppler/pdf2image) are available.
"""

import io
import re

import fitz  # PyMuPDF

import config

try:
    import pytesseract
    from pdf2image import convert_from_bytes
    _OCR_AVAILABLE = True
except Exception:  # noqa: BLE001 - OCR is optional
    _OCR_AVAILABLE = False


def _clean(text: str) -> str:
    text = text.replace("\x00", " ")
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def _ocr_page(pdf_bytes: bytes, page_number: int) -> str:
    if not _OCR_AVAILABLE:
        return ""
    try:
        images = convert_from_bytes(
            pdf_bytes, first_page=page_number + 1, last_page=page_number + 1
        )
        return "\n".join(pytesseract.image_to_string(img) for img in images)
    except Exception:  # noqa: BLE001 - OCR best-effort
        return ""


def extract_text(pdf_bytes: bytes) -> str:
    """Return cleaned text extracted from the PDF bytes."""
    pages_text = []
    with fitz.open(stream=pdf_bytes, filetype="pdf") as doc:
        for i, page in enumerate(doc):
            text = page.get_text("text")
            if len(text.strip()) < config.SCANNED_PAGE_CHAR_THRESHOLD:
                ocr = _ocr_page(pdf_bytes, i)
                if len(ocr.strip()) > len(text.strip()):
                    text = ocr
            pages_text.append(text)
    return _clean("\n\n".join(pages_text))


def ocr_available() -> bool:
    return _OCR_AVAILABLE
