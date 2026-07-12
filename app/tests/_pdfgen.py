"""Test helper: build a minimal, real PDF in-memory that the app's pypdf can read.

Not a test module (underscore prefix) — imported by the Knowledge test suites so they can
exercise the real ingest path without a checked-in binary fixture or a PDF-writer dependency.
"""

from __future__ import annotations


def make_pdf(pages_text: list[str]) -> bytes:
    """Build a minimal multi-page PDF (Helvetica) whose text pypdf can extract."""
    assert pages_text, "at least one page required"
    n = len(pages_text)
    catalog_num, pages_num, font_num = 1, 2, 3
    page_nums = [4 + 2 * i for i in range(n)]
    content_nums = [5 + 2 * i for i in range(n)]

    def esc(s: str) -> str:
        return s.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")

    def content_stream(text: str) -> str:
        body = "BT\n/F1 12 Tf\n72 760 Td\n"
        for line in text.split("\n"):
            body += f"({esc(line)}) Tj\n0 -14 Td\n"
        return body + "ET"

    parts: dict[int, bytes] = {}
    parts[catalog_num] = f"<< /Type /Catalog /Pages {pages_num} 0 R >>".encode()
    kids = " ".join(f"{p} 0 R" for p in page_nums)
    parts[pages_num] = f"<< /Type /Pages /Count {n} /Kids [{kids}] >>".encode()
    parts[font_num] = b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>"
    for i in range(n):
        parts[page_nums[i]] = (
            f"<< /Type /Page /Parent {pages_num} 0 R /MediaBox [0 0 612 792] "
            f"/Resources << /Font << /F1 {font_num} 0 R >> >> /Contents {content_nums[i]} 0 R >>"
        ).encode()
        cs = content_stream(pages_text[i]).encode()
        parts[content_nums[i]] = b"<< /Length %d >>\nstream\n" % len(cs) + cs + b"\nendstream"

    total = max(parts)
    out = bytearray(b"%PDF-1.4\n")
    offsets: dict[int, int] = {}
    for num in range(1, total + 1):
        offsets[num] = len(out)
        out += f"{num} 0 obj\n".encode() + parts[num] + b"\nendobj\n"
    xref_pos = len(out)
    out += f"xref\n0 {total + 1}\n".encode()
    out += b"0000000000 65535 f \n"
    for num in range(1, total + 1):
        out += ("%010d 00000 n \n" % offsets[num]).encode()
    out += f"trailer\n<< /Size {total + 1} /Root {catalog_num} 0 R >>\nstartxref\n{xref_pos}\n%%EOF".encode()
    return bytes(out)


def long_pages(marker: str, pages: int = 6, lines_per_page: int = 18) -> list[str]:
    """Filler pages (~80 chars/line, on-page) with ``marker`` on the LAST line of the LAST page,
    so it lands only in a late chunk — proving the whole doc (not just its head) is embedded."""
    out: list[str] = []
    line_no = 0
    for p in range(pages):
        lines = []
        for _ in range(lines_per_page):
            line_no += 1
            lines.append(f"Filler line {line_no:03d} of the annual compliance record padding text here.")
        if p == pages - 1:
            lines[-1] = f"Final binding clause reference token {marker} concludes this record."
        out.append("\n".join(lines))
    return out
