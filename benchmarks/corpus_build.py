"""Materialise the benchmark corpus into a directory (text files are checked in; DOCX/PDF are generated)."""
from __future__ import annotations
import io
import shutil
from pathlib import Path

CORPUS = Path(__file__).parent / "corpus"

# relative path -> ingest options for generated binaries
BINARIES = {
    "atlas/payments-design.docx": {"project": "Atlas"},
    "atlas/perf-report.pdf": {"project": "Atlas"},
}


def _docx() -> bytes:
    import docx
    d = docx.Document()
    d.add_heading("Payments service design", 0)
    d.add_heading("Overview", 1)
    d.add_paragraph("The payments service processes card transactions for the Atlas platform. It uses Stripe for card processing and stores the payment ledger in PostgreSQL.")
    d.add_heading("Reliability", 1)
    d.add_paragraph("The payments service retries failed webhook deliveries up to 5 retries with exponential backoff. We decided to use idempotency keys for every charge request so that a retry never double charges a customer. The retry queue depends on RabbitMQ.")
    b = io.BytesIO()
    d.save(b)
    return b.getvalue()


def _pdf() -> bytes:
    from fpdf import FPDF
    pdf = FPDF()
    pdf.add_page()
    pdf.set_font("Helvetica", size=12)
    for p in ["Atlas API performance report",
              "We measured the API gateway latency under sustained load. The p95 latency is 120 ms at 500 requests per second.",
              "The bottleneck was PostgreSQL connection pooling. Raising the PgBouncer pool size improved throughput by 18 percent in the benchmark."]:
        pdf.multi_cell(0, 8, p)
        pdf.ln(2)
    return bytes(pdf.output())


def materialise(dest: Path) -> list[tuple[str, dict]]:
    """Copy the corpus to `dest` and generate binaries. Returns [(relative path, options)] in deterministic order."""
    if dest.exists():
        shutil.rmtree(dest)
    shutil.copytree(CORPUS, dest)
    (dest / "atlas" / "payments-design.docx").write_bytes(_docx())
    (dest / "atlas" / "perf-report.pdf").write_bytes(_pdf())
    files = sorted(p.relative_to(dest).as_posix() for p in dest.rglob("*") if p.is_file())
    return [(f, BINARIES.get(f, {})) for f in files]
