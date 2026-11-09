# -*- coding: utf-8 -*-
"""Document ingestion for spec-by-example development.

A customer's real quotation / invoice / price list IS the requirement: its
columns are the fields, its numbers are the compute rules. These tools let the
agent read uploaded attachments (text layer only — Phase A; scanned documents
go through the vision channel in Phase B) so the analyzing phase can derive
the data model from the actual document instead of guessing.
"""
import base64
import io
import logging
import re
import zipfile

from odoo import models, _

_logger = logging.getLogger(__name__)

_MAX_CHARS = 6000          # per read_attachment call (window further with part=)
_MAX_FILE_MB = 15


def _clip(text, limit=_MAX_CHARS):
    text = text or ''
    return text if len(text) <= limit else text[:limit] + '\n…(clipped)'


def _decode(raw):
    for enc in ('utf-8-sig', 'utf-8', 'gb18030', 'latin-1'):
        try:
            return raw.decode(enc)
        except (UnicodeDecodeError, LookupError):
            continue
    return raw.decode('utf-8', errors='replace')


class AICodeFiles(models.AbstractModel):
    _name = 'ai.code.files'
    _description = 'AI Code Attachment Ingestion'

    # ── channel-scoped attachment access ───────────────────────────────
    def _channel_attachments(self, channel):
        return self.env['ir.attachment'].sudo().search([
            ('res_model', '=', 'discuss.channel'),
            ('res_id', '=', channel.id),
        ], order='id')

    def _get_attachment(self, channel, attachment_id):
        att = self.env['ir.attachment'].sudo().browse(int(attachment_id)).exists()
        if not att or att.res_model != 'discuss.channel' or att.res_id != channel.id:
            raise ValueError(_("Attachment %s is not part of this conversation.",
                               attachment_id))
        return att

    # ── tools ──────────────────────────────────────────────────────────
    def tool_list_attachments(self, channel):
        """The documents uploaded to this conversation."""
        atts = self._channel_attachments(channel)
        if not atts:
            return _("No document uploaded in this conversation.")
        lines = [_("Uploaded documents (read with read_attachment):")]
        for att in atts:
            size_kb = round((att.file_size or 0) / 1024)
            lines.append(f"- id={att.id} | {att.name} | {att.mimetype or '?'} | {size_kb} KB")
        return "\n".join(lines)

    def tool_read_attachment(self, channel, attachment_id, part=0):
        """Extract the text of one uploaded document. part= pages/sheets window
        (0-based) for large files; the output says how many parts exist."""
        att = self._get_attachment(channel, attachment_id)
        if (att.file_size or 0) > _MAX_FILE_MB * 1024 * 1024:
            return _("File too large (> %s MB).", _MAX_FILE_MB)
        raw = base64.b64decode(att.datas or b'')
        if not raw:
            return _("The attachment is empty.")
        name = (att.name or '').lower()
        mime = att.mimetype or ''
        part = max(0, int(part or 0))
        try:
            if name.endswith('.pdf') or 'pdf' in mime:
                return self._extract_pdf(raw, part)
            if name.endswith(('.xlsx', '.xlsm')) or 'spreadsheet' in mime:
                return self._extract_xlsx(raw, part)
            if name.endswith('.docx') or 'wordprocessingml' in mime:
                return self._extract_docx(raw, part)
            if name.endswith(('.csv', '.txt', '.json', '.xml', '.md')) or \
                    mime.startswith('text/') or 'json' in mime or 'xml' in mime:
                return self._extract_text(raw, part)
            if mime.startswith('image/'):
                return _("'%s' is an image. Image/scan analysis is not enabled yet "
                         "(Phase B) — ask the user to provide the data as "
                         "PDF-with-text, xlsx or csv.", att.name)
        except Exception as e:  # noqa: BLE001
            _logger.exception("ai_code: attachment extraction failed")
            return _("Could not extract '%s': %s", att.name, e)
        return _("Unsupported file type '%s' (%s). Supported: pdf (text layer), "
                 "xlsx, csv, docx, txt/json/xml.", att.name, mime)

    # ── per-format extractors ──────────────────────────────────────────
    PDF_PAGES_PER_PART = 5

    def _extract_pdf(self, raw, part):
        try:
            from pypdf import PdfReader
        except ImportError:
            from PyPDF2 import PdfReader   # older envs
        reader = PdfReader(io.BytesIO(raw))
        total = len(reader.pages)
        parts = max(1, -(-total // self.PDF_PAGES_PER_PART))
        lo = part * self.PDF_PAGES_PER_PART
        if lo >= total:
            return _("PDF has only %s page(s) — part %s is out of range.", total, part)
        hi = min(total, lo + self.PDF_PAGES_PER_PART)
        chunks = []
        for i in range(lo, hi):
            text = (reader.pages[i].extract_text() or '').strip()
            chunks.append(f"--- page {i + 1}/{total} ---\n{text or '(no text layer on this page)'}")
        body = "\n".join(chunks)
        if not re.search(r'\w', body.replace('-', '').replace('page', '')):
            body += _("\n(This PDF seems to have NO text layer — likely a scan. "
                      "Ask the user for an xlsx/csv, or wait for Phase B vision.)")
        head = _("PDF: %s pages, part %s/%s (pages %s-%s)\n",
                 total, part + 1, parts, lo + 1, hi)
        return head + _clip(body)

    XLSX_ROWS = 40
    XLSX_COLS = 25

    def _extract_xlsx(self, raw, part):
        import openpyxl
        wb = openpyxl.load_workbook(io.BytesIO(raw), read_only=True, data_only=True)
        sheets = wb.sheetnames
        if part >= len(sheets):
            return _("Workbook has only %s sheet(s): %s", len(sheets), ", ".join(sheets))
        ws = wb[sheets[part]]
        lines = [_("Sheet %s/%s: '%s' (other sheets: %s) — first %s rows as TSV:",
                   part + 1, len(sheets), sheets[part],
                   ", ".join(s for s in sheets if s != sheets[part]) or _("none"),
                   self.XLSX_ROWS)]
        for r, row in enumerate(ws.iter_rows(max_row=self.XLSX_ROWS,
                                             max_col=self.XLSX_COLS,
                                             values_only=True), 1):
            cells = ['' if v is None else str(v).replace('\t', ' ').replace('\n', ' ')
                     for v in row]
            while cells and cells[-1] == '':
                cells.pop()
            lines.append(f"{r}\t" + "\t".join(cells))
        wb.close()
        return _clip("\n".join(lines))

    def _extract_docx(self, raw, part):
        with zipfile.ZipFile(io.BytesIO(raw)) as zf:
            xml = zf.read('word/document.xml').decode('utf-8', errors='replace')
        # paragraph breaks, then strip the tags
        xml = xml.replace('</w:p>', '\n').replace('<w:tab/>', '\t')
        text = re.sub(r'<[^>]+>', '', xml)
        text = re.sub(r'\n{3,}', '\n\n', text).strip()
        pages = max(1, -(-len(text) // _MAX_CHARS))
        lo = part * _MAX_CHARS
        if lo >= len(text) and part:
            return _("Document has %s part(s) only.", pages)
        return (_("DOCX text, part %s/%s:\n", part + 1, pages)
                + text[lo:lo + _MAX_CHARS])

    def _extract_text(self, raw, part):
        text = _decode(raw)
        pages = max(1, -(-len(text) // _MAX_CHARS))
        lo = part * _MAX_CHARS
        if lo >= len(text) and part:
            return _("File has %s part(s) only.", pages)
        head = _("Text file, part %s/%s:\n", part + 1, pages) if pages > 1 else ''
        return head + text[lo:lo + _MAX_CHARS]
