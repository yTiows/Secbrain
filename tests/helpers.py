import io

DOCX_TEXT = [
    ("Title", "Payments Service Design"),
    ("Heading 1", "Overview"),
    ("Normal", "The payments service processes card transactions for the Atlas platform. It uses Stripe for card processing and PostgreSQL for ledger storage."),
    ("Heading 1", "Reliability"),
    ("Normal", "The service retries failed webhook deliveries. The maximum retry count is 5 retries. We decided to use idempotency keys for every charge request."),
]


def make_docx() -> bytes:
    import docx
    d = docx.Document()
    for style, text in DOCX_TEXT:
        if style == "Title":
            d.add_heading(text, 0)
        elif style.startswith("Heading"):
            d.add_heading(text, int(style[-1]))
        else:
            d.add_paragraph(text)
    b = io.BytesIO()
    d.save(b)
    return b.getvalue()


def make_pdf(paragraphs: list[str] | None = None) -> bytes:
    from fpdf import FPDF
    paragraphs = paragraphs or [
        "Retrieval Benchmark Report",
        "We measured retrieval latency across 3 index configurations. Hybrid search achieved precision at 5 of 0.82 and recall at 5 of 0.74.",
        "The benchmark shows that graph expansion improves recall for multi-hop questions without hurting precision.",
    ]
    pdf = FPDF()
    pdf.add_page()
    pdf.set_font("Helvetica", size=12)
    for p in paragraphs:
        pdf.multi_cell(0, 8, p)
        pdf.ln(2)
    return bytes(pdf.output())
