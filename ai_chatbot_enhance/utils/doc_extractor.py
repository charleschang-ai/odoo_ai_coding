"""Convert chat attachments into LLM-consumable inputs.

Text-bearing formats (txt/csv/xlsx/docx/text-PDF) are extracted server-side
and work with EVERY provider. Scanned PDFs and images are passed through
natively (base64) only to vision-capable providers (openai/google); other
providers get an honest "use a vision assistant" note instead.
"""
import base64
import io
import logging
import zipfile

from lxml import etree

_logger = logging.getLogger(__name__)

MAX_ATTACHMENTS = 3
MAX_FILE_BYTES = 10 * 1024 * 1024  # 10MB
MAX_TEXT_CHARS = 30000
XLSX_MAX_SHEETS = 3
XLSX_MAX_ROWS = 200
PDF_MAX_PAGES = 20

# Providers whose LLM API accepts PDF/images natively (built-in OCR)
NATIVE_FILE_PROVIDERS = ('openai', 'google')

W_NS = '{http://schemas.openxmlformats.org/wordprocessingml/2006/main}'


def _extract_xlsx(raw):
    try:
        import openpyxl  # bundled with Odoo
    except ImportError:
        return None, "openpyxl is not installed on the server"
    try:
        workbook = openpyxl.load_workbook(io.BytesIO(raw), read_only=True, data_only=True)
    except Exception:  # noqa: BLE001
        return None, "could not parse the spreadsheet"
    parts = []
    for sheet in workbook.worksheets[:XLSX_MAX_SHEETS]:
        parts.append(f"### Sheet: {sheet.title}")
        count = 0
        for row in sheet.iter_rows(values_only=True):
            cells = ["" if c is None else str(c) for c in row]
            if not any(c.strip() for c in cells):
                continue
            parts.append(", ".join(cells))
            count += 1
            if count >= XLSX_MAX_ROWS:
                parts.append(f"... (sheet truncated at {XLSX_MAX_ROWS} rows)")
                break
    if len(workbook.worksheets) > XLSX_MAX_SHEETS:
        parts.append(f"... ({len(workbook.worksheets) - XLSX_MAX_SHEETS} more sheets not shown)")
    return "\n".join(parts), None


def _extract_docx(raw):
    try:
        with zipfile.ZipFile(io.BytesIO(raw)) as archive:
            xml_content = archive.read('word/document.xml')
    except (zipfile.BadZipFile, KeyError):
        return None, "not a valid .docx file"
    try:
        root = etree.fromstring(xml_content)
    except etree.XMLSyntaxError:
        return None, "could not parse the document"
    paragraphs = []
    for para in root.iter(f'{W_NS}p'):
        texts = [t.text for t in para.iter(f'{W_NS}t') if t.text]
        if texts:
            paragraphs.append("".join(texts))
    return "\n".join(paragraphs), None


def _extract_pdf_text(raw):
    try:
        from pypdf import PdfReader
    except ImportError:
        try:
            from PyPDF2 import PdfReader
        except ImportError:
            return None, "no PDF text-extraction library available on the server"
    try:
        reader = PdfReader(io.BytesIO(raw))
    except Exception:  # noqa: BLE001
        return None, "could not parse the PDF"
    parts = []
    for index, page in enumerate(reader.pages):
        if index >= PDF_MAX_PAGES:
            parts.append(f"... (truncated at {PDF_MAX_PAGES} pages)")
            break
        try:
            parts.append(page.extract_text() or "")
        except Exception:  # noqa: BLE001
            parts.append("")
    text = "\n".join(parts).strip()
    if not text:
        return None, "no extractable text (probably a scanned PDF)"
    return text, None


def extract_ai_files(attachments, provider):
    """Build the `files` list for LLMApiService.request_llm from attachments.

    :return: (files, notes) — `files` entries are {'mimetype', 'value'}
             (text/plain value = extracted text; pdf/image value = base64);
             `notes` are honest, user-facing processing remarks.
    """
    files = []
    notes = []
    native = provider in NATIVE_FILE_PROVIDERS

    for attachment in attachments[:MAX_ATTACHMENTS]:
        name = attachment.name or 'file'
        lower = name.lower()
        mimetype = (attachment.mimetype or '').lower()
        raw = attachment.raw or b''
        if len(raw) > MAX_FILE_BYTES:
            notes.append(f"'{name}' skipped: larger than 10MB.")
            continue

        text = None
        error = None
        if mimetype.startswith('text/') or lower.endswith(('.txt', '.csv', '.md', '.json')):
            text = raw.decode('utf-8', errors='replace')
        elif 'spreadsheetml' in mimetype or lower.endswith('.xlsx'):
            text, error = _extract_xlsx(raw)
        elif 'wordprocessingml' in mimetype or lower.endswith('.docx'):
            text, error = _extract_docx(raw)
        elif mimetype == 'application/pdf' or lower.endswith('.pdf'):
            if native:
                files.append({'mimetype': 'application/pdf', 'value': base64.b64encode(raw).decode()})
                notes.append(f"'{name}': PDF passed to the model natively (built-in OCR).")
                continue
            text, error = _extract_pdf_text(raw)
            if error:
                error += " — this assistant's model cannot view documents; for scanned files use a GPT/Gemini based assistant"
        elif mimetype.startswith('image/'):
            if native:
                files.append({'mimetype': mimetype, 'value': base64.b64encode(raw).decode()})
                notes.append(f"'{name}': image passed to the model natively.")
                continue
            error = "images need a vision model — use a GPT/Gemini based assistant"
        else:
            error = f"unsupported file type ({mimetype or 'unknown'})"

        if error:
            notes.append(f"'{name}' could not be processed: {error}.")
            continue
        if text is not None:
            if len(text) > MAX_TEXT_CHARS:
                text = text[:MAX_TEXT_CHARS]
                notes.append(f"'{name}': content truncated to {MAX_TEXT_CHARS} characters.")
            files.append({'mimetype': 'text/plain', 'value': f"=== Document: {name} ===\n{text}"})

    if len(attachments) > MAX_ATTACHMENTS:
        notes.append(f"Only the first {MAX_ATTACHMENTS} attachments were processed ({len(attachments)} were sent).")
    return files, notes
