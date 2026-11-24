"""Text primitives: tokenization, light stemming, sentence splitting, token estimates."""
from __future__ import annotations
import hashlib
import re
import unicodedata

STOPWORDS = set("""
a an the and or but if then else of to in on at by for with from as is are was were be been being it its this that these those
i you he she we they me my our your their his her them us not no nor so than too very can could should would will shall may might
do does did done have has had having just also into over under about above below between through during before after again further
once here there when where why how all any both each few more most other some such only own same s t don now what which who whom
up down out off while per via etc eg ie vs
de da do das dos e o a os as um uma uns umas em no na nos nas por para com sem sobre que se ao aos
""".split())

_WORD = re.compile(r"[^\W_]+(?:[\-'.+#][^\W_]+)*[+#]*", re.UNICODE)
_SENT = re.compile(r"(?<=[.!?])\s+(?=[\"'(\[]?[A-Z0-9#\-*])|\n+")


def normalize_unicode(s: str) -> str:
    return unicodedata.normalize("NFKC", s)


def sha256_text(s: str) -> str:
    return hashlib.sha256(s.encode("utf-8", "replace")).hexdigest()


def words(text: str) -> list[str]:
    return [m.group(0) for m in _WORD.finditer(text)]


def stem(w: str) -> str:
    """Conservative suffix stripper; only needs to be consistent between index and query."""
    w = w.lower()
    if len(w) <= 3 or not w.isalpha():
        return w
    for suf, rep in (("ies", "y"), ("sses", "ss"), ("ational", "ate"), ("ization", "ize"), ("izing", "ize"), ("ised", "ise")):
        if w.endswith(suf) and len(w) - len(suf) >= 2:
            return w[: -len(suf)] + rep
    for suf in ("ingly", "edly", "ment", "ness", "ings", "ing", "ed", "ly", "es", "s"):
        if w.endswith(suf) and len(w) - len(suf) >= 3:
            if suf == "s" and w.endswith("ss"):
                return w
            base = w[: -len(suf)]
            if len(base) > 3 and base[-1] == base[-2] and base[-1] not in "lsz":
                base = base[:-1]
            return base
    return w


def tokens(text: str, keep_stop: bool = False) -> list[str]:
    out = []
    for w in words(normalize_unicode(text).lower().replace("-", " ").replace("_", " ")):
        w = w.strip(".'")
        if not w:
            continue
        if not keep_stop and w in STOPWORDS:
            continue
        if len(w) < 2 and not w.isdigit():
            continue
        out.append(stem(w))
    return out


def sentences(text: str) -> list[str]:
    parts = [p.strip() for p in _SENT.split(text) if p and p.strip()]
    return parts


def estimate_tokens(text: str) -> int:
    """Estimate LLM tokens. No tokenizer is available offline, so this is an estimate:
    max(chars/4, words*1.33). Reported as `tokens_est` everywhere."""
    if not text:
        return 0
    return max(int(len(text) / 4 + 0.5), int(len(text.split()) * 1.33 + 0.5))


def key(s: str) -> str:
    """Normalized identity key for names/aliases."""
    s = normalize_unicode(s).lower().strip()
    s = re.sub(r"^(the|a|an)\s+", "", s)
    s = re.sub(r"[^\w+#]+", "", s)
    return s


def shingles(text: str, n: int = 5) -> set[str]:
    toks = [t for t in re.findall(r"\w+", normalize_unicode(text).lower())]
    if len(toks) < n:
        return {" ".join(toks)} if toks else set()
    return {" ".join(toks[i:i + n]) for i in range(len(toks) - n + 1)}


def jaccard(a: set, b: set) -> float:
    if not a or not b:
        return 0.0
    return len(a & b) / len(a | b)


def truncate(s: str, n: int) -> str:
    s = s.strip()
    return s if len(s) <= n else s[: n - 1].rstrip() + "…"


def slugify(s: str, maxlen: int = 80) -> str:
    s = normalize_unicode(s)
    s = re.sub(r"[^\w\s\-]", "", s).strip()
    s = re.sub(r"\s+", " ", s)
    return s[:maxlen].strip() or "untitled"
