"""File → text extraction with explicit failure reporting (never pretend an unreadable file was understood)."""
from __future__ import annotations
import io
import json
import re
from dataclasses import dataclass, field
from pathlib import Path

from .security import sanitize_text

SUPPORTED = {".txt", ".md", ".markdown", ".json", ".pdf", ".docx", ".text", ".log", ".csv"}


class ExtractionError(Exception):
    pass


@dataclass
class Extracted:
    text: str
    format: str
    title: str | None = None
    meta: dict = field(default_factory=dict)       # frontmatter / document properties
    warnings: list[str] = field(default_factory=list)


_FM = re.compile(r"\A---\s*\n(.*?)\n---\s*(?:\n|\Z)", re.S)


def parse_frontmatter(text: str) -> tuple[dict, str]:
    """Minimal YAML-subset frontmatter parser (scalars, inline/block lists). No code execution, no external deps."""
    m = _FM.match(text)
    if not m:
        return {}, text
    meta: dict = {}
    cur_key = None
    for line in m.group(1).split("\n"):
        if not line.strip() or line.strip().startswith("#"):
            continue
        if re.match(r"^\s+-\s+", line) and cur_key:
            meta.setdefault(cur_key, [])
            if isinstance(meta[cur_key], list):
                meta[cur_key].append(_scalar(line.split("-", 1)[1].strip()))
            continue
        kv = re.match(r"^([A-Za-z0-9_\-]+)\s*:\s*(.*)$", line)
        if not kv:
            continue
        cur_key, val = kv.group(1).lower(), kv.group(2).strip()
        if val == "":
            meta[cur_key] = []
        elif val.startswith("[") and val.endswith("]"):
            meta[cur_key] = [_scalar(v.strip()) for v in val[1:-1].split(",") if v.strip()]
        else:
            meta[cur_key] = _scalar(val)
    return meta, text[m.end():]


def _scalar(v: str):
    v = v.strip().strip("'\"")
    if re.fullmatch(r"-?\d+(\.\d+)?", v):
        return float(v) if "." in v else int(v)
    if v.lower() in ("true", "false"):
        return v.lower() == "true"
    return v


def _decode(data: bytes) -> str:
    for enc in ("utf-8-sig", "utf-16"):
        try:
            t = data.decode(enc)
            if enc == "utf-16" and not data[:2] in (b"\xff\xfe", b"\xfe\xff"):
                continue
            return t
        except UnicodeDecodeError:
            continue
    try:
        return data.decode("cp1252")
    except UnicodeDecodeError:
        raise ExtractionError("cannot decode text (not UTF-8/UTF-16/cp1252)")


def _json_to_text(obj, prefix="", depth=0, out=None) -> list[str]:
    out = out if out is not None else []
    if depth > 8:
        return out
    if isinstance(obj, dict):
        for k, v in obj.items():
            if isinstance(v, (dict, list)):
                out.append(f"{'#' * min(depth + 2, 6)} {k}")
                _json_to_text(v, "", depth + 1, out)
            else:
                out.append(f"{k}: {v}")
    elif isinstance(obj, list):
        for i, v in enumerate(obj):
            if isinstance(v, (dict, list)):
                _json_to_text(v, "", depth + 1, out)
                out.append("")
            else:
                out.append(f"- {v}")
    else:
        out.append(str(obj))
    return out


def extract(name: str, data: bytes, mime: str | None = None) -> Extracted:
    ext = Path(name).suffix.lower()
    if not data:
        raise ExtractionError("empty input")
    if ext == ".pdf" or data[:5] == b"%PDF-":
        return _pdf(data)
    if ext == ".docx" or data[:2] == b"PK" and ext == ".docx":
        return _docx(data)
    if data[:2] == b"PK":
        raise ExtractionError("zip container that is not .docx; unsupported")
    if b"\x00" in data[:4096] and not data[:2] in (b"\xff\xfe", b"\xfe\xff"):
        raise ExtractionError("binary content detected; unsupported file type")
    text = sanitize_text(_decode(data))
    if ext == ".json" or (ext == "" and text.lstrip()[:1] in "{["):
        try:
            obj = json.loads(text)
        except json.JSONDecodeError as e:
            raise ExtractionError(f"invalid JSON: {e}")
        title = obj.get("title") or obj.get("name") if isinstance(obj, dict) else None
        meta = {}
        if isinstance(obj, dict):
            for k in ("tags", "project", "type", "id"):
                if k in obj and isinstance(obj[k], (str, list, int)):
                    meta[k] = obj[k]
        body = "\n".join(_json_to_text(obj))
        return Extracted(body, "json", title if isinstance(title, str) else None, meta)
    if ext in (".md", ".markdown"):
        meta, body = parse_frontmatter(text)
        return Extracted(body, "markdown", meta.get("title") if isinstance(meta.get("title"), str) else None, meta)
    # plain text (frontmatter tolerated)
    meta, body = parse_frontmatter(text)
    return Extracted(body, "text", meta.get("title") if isinstance(meta.get("title"), str) else None, meta)


def _pdf(data: bytes) -> Extracted:
    try:
        from pypdf import PdfReader
        r = PdfReader(io.BytesIO(data))
        if r.is_encrypted:
            raise ExtractionError("PDF is encrypted")
        pages, warnings = [], []
        for i, p in enumerate(r.pages):
            try:
                pages.append(p.extract_text() or "")
            except Exception as e:  # corrupt page
                warnings.append(f"page {i + 1}: {e}")
        text = sanitize_text("\n\n".join(pages)).strip()
        if len(text) < 20:
            raise ExtractionError("no extractable text (scanned/image-only PDF? OCR is not supported)")
        info = r.metadata or {}
        title = str(info.title).strip() if info and info.title else None
        return Extracted(text, "pdf", title, {"pages": len(r.pages)}, warnings)
    except ExtractionError:
        raise
    except Exception as e:
        raise ExtractionError(f"cannot read PDF: {e}")


def _docx(data: bytes) -> Extracted:
    try:
        import docx
        d = docx.Document(io.BytesIO(data))
    except Exception as e:
        raise ExtractionError(f"cannot read DOCX: {e}")
    lines = []
    for p in d.paragraphs:
        t = p.text.strip()
        if not t:
            lines.append("")
            continue
        style = (p.style.name or "") if p.style is not None else ""
        m = re.match(r"Heading (\d)", style)
        if m:
            lines.append("#" * int(m.group(1)) + " " + t)
        elif style == "Title":
            lines.append("# " + t)
        elif "List Number" in style:
            lines.append("1. " + t)
        elif "List" in style:
            lines.append("- " + t)
        else:
            lines.append(t)
    for tbl in d.tables:
        for row in tbl.rows:
            lines.append(" | ".join(c.text.strip() for c in row.cells))
    text = sanitize_text("\n".join(lines)).strip()
    if len(text) < 5:
        raise ExtractionError("DOCX contains no text")
    cp = d.core_properties
    return Extracted(text, "docx", (cp.title or None), {"author": cp.author or ""})
