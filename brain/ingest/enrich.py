"""Metadata enrichment: summaries, keyword tags, importance, dates."""
from __future__ import annotations
import math
import re
from collections import Counter
from datetime import datetime, timezone

from ..text import tokens, sentences, truncate, stem, STOPWORDS

TAG_RX = re.compile(r"(?<![\w&/])#([A-Za-z][\w\-/]{1,40})\b")
DATE_RX = re.compile(r"\b(20\d\d|19\d\d)-(\d\d)-(\d\d)\b")
GENERIC_TITLES = {"overview", "summary", "notes", "introduction", "details", "background", "context", "conclusion", "conclusions",
                  "next steps", "description", "discussion", "results", "status", "open questions", "references", "decision", "rationale",
                  "goals", "scope", "setup", "steps", "problem", "solution", "intro", "todo", "analysis", "findings", "motivation",
                  "consequences", "alternatives", "proposal"}


def summarize(text: str, maxlen: int = 280) -> str:
    text = re.sub(r"^#+\s+.*$", "", text, flags=re.M)
    text = re.sub(r"\s+", " ", re.sub(r"^\s*(?:[-*>]|\d+[.)])\s+", "", text, flags=re.M)).strip()
    sents = sentences(text)
    out = ""
    for s in sents:
        if len(s) < 15:
            continue
        if len(out) + len(s) > maxlen:
            break
        out = (out + " " + s).strip()
        if len(out) >= 120:
            break
    return truncate(out or text, maxlen)


GENERIC_WORDS = set("""against build builds using used uses make makes made need needs needed work works working want wants good great better best
new old first last also still already really many much more most some such other same different important following include includes including
based allow allows allowed provide provides provided support supports supported example examples please check ensure system thing things way ways
time times step steps part parts case cases like likely used""".split())


def keywords(text: str, df_lookup=None, n: int = 3, title: str = "") -> list[str]:
    """Readable keyword tags: surface forms ranked by (tf x idf), preferring terms repeated or present in the title."""
    surface: dict[str, Counter] = {}
    for w in re.findall(r"[A-Za-z][A-Za-z\-]{3,}", text):
        lw = w.lower()
        if lw in STOPWORDS or lw in GENERIC_WORDS:
            continue
        surface.setdefault(stem(lw), Counter())[lw] += 1
    tstems = {stem(w.lower()) for w in re.findall(r"[A-Za-z]{4,}", title)}
    scored = []
    for st_, forms in surface.items():
        tf = sum(forms.values())
        if tf < 2 and st_ not in tstems:
            continue
        idf = df_lookup(st_) if df_lookup else 1.0
        scored.append((tf * idf * (1.5 if st_ in tstems else 1.0), forms.most_common(1)[0][0]))
    scored.sort(key=lambda x: (-x[0], x[1]))
    return [w for _, w in scored[:n]]


def inline_tags(text: str) -> list[str]:
    return list(dict.fromkeys(m.group(1).lower() for m in TAG_RX.finditer(text)))[:12]


def find_date(text: str, head_chars: int = 400) -> float | None:
    m = DATE_RX.search(text[:head_chars])
    if not m:
        return None
    try:
        return datetime(int(m.group(1)), int(m.group(2)), int(m.group(3)), tzinfo=timezone.utc).timestamp()
    except ValueError:
        return None


def parse_date(v) -> float | None:
    if isinstance(v, (int, float)):
        return float(v) if v > 10_000 else None
    if isinstance(v, str):
        m = DATE_RX.search(v)
        if m:
            return find_date(v)
    return None


BASE_IMPORTANCE = {"project": 0.7, "decision": 0.65, "procedure": 0.55, "instruction": 0.6, "preference": 0.6, "concept": 0.55,
                   "research": 0.55, "fact": 0.5, "experience": 0.5, "idea": 0.45, "hypothesis": 0.45, "question": 0.4,
                   "observation": 0.4, "reference": 0.35, "document": 0.4, "entity": 0.45}


def importance_for(type_: str, level: int = 2, user_hint: float | None = None) -> float:
    if user_hint is not None:
        return max(0.0, min(1.0, float(user_hint)))
    base = BASE_IMPORTANCE.get(type_, 0.5)
    if level <= 1:
        base += 0.05
    return round(min(base, 0.95), 2)
