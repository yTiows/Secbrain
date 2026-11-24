"""Claim extraction: structured (subject, predicate, value) assertions with evidence sentences.

Only conservative patterns produce *structured* claims, because those are what contradiction/update
detection runs on. `functional` marks single-valued predicates (a subject can have only one value at a time),
which is what makes a differing value a potential contradiction.
"""
from __future__ import annotations
import re
from dataclasses import dataclass

from ..text import key as name_key, sentences

UNIT_BASE = {  # unit -> (base unit, factor)
    "ms": ("s", 0.001), "millisecond": ("s", 0.001), "milliseconds": ("s", 0.001),
    "s": ("s", 1), "sec": ("s", 1), "secs": ("s", 1), "second": ("s", 1), "seconds": ("s", 1),
    "min": ("s", 60), "mins": ("s", 60), "minute": ("s", 60), "minutes": ("s", 60),
    "h": ("s", 3600), "hr": ("s", 3600), "hour": ("s", 3600), "hours": ("s", 3600),
    "day": ("s", 86400), "days": ("s", 86400), "week": ("s", 604800), "weeks": ("s", 604800),
    "kb": ("b", 1024), "mb": ("b", 1024 ** 2), "gb": ("b", 1024 ** 3), "tb": ("b", 1024 ** 4),
    "%": ("%", 1), "percent": ("%", 1), "requests": ("req", 1), "request": ("req", 1), "users": ("users", 1),
    "connections": ("conn", 1), "workers": ("workers", 1), "retries": ("retries", 1), "nodes": ("nodes", 1),
}
NUM = r"(?P<num>\$?\d[\d,]*(?:\.\d+)?)\s*(?P<unit>%|[A-Za-z]+(?:/[A-Za-z]+)?)?"
ART = re.compile(r"^(the|a|an|our|its|their|this|that)\s+", re.I)
NEG = r"(?P<neg>does not|doesn't|do not|don't|cannot|can't|can not|no longer|never|did not|didn't|will not|won't|is not|isn't)"

FUNCTIONAL_VERBS = {  # predicate key -> functional?
    "runs on": True, "hosted on": True, "deployed on": True, "written in": True, "built with": False,
    "uses": False, "supports": False, "requires": False, "chosen": True, "stores in": True, "is": True,
    "limit": True, "default": True, "version": True, "owner": True, "deadline": True,
}


@dataclass
class ParsedClaim:
    text: str
    subject: str
    predicate: str
    value: str
    subj_key: str
    pred_key: str
    val_key: str
    functional: bool
    polarity: int
    confidence: float


def _clean_np(s: str) -> str:
    s = ART.sub("", s.strip().strip("*_`\"'“”"))
    return re.sub(r"\s+", " ", s).strip(" ,;:.")


def norm_value(v: str) -> str:
    """Canonical comparable form: numbers+units converted to base units; text lowercased, articles dropped."""
    v = v.strip().strip(".;,")
    m = re.fullmatch(r"\$?(\d[\d,]*(?:\.\d+)?)\s*([A-Za-z%]+)?(?:\s*(?:/|per)\s*([A-Za-z]+))?", v)
    if m:
        n = float(m.group(1).replace(",", ""))
        u = (m.group(2) or "").lower()
        base, f = UNIT_BASE.get(u, (u, 1))
        if v.startswith("$"):
            base, f = "usd", 1
        if m.group(3):
            base = f"{base}/{m.group(3).lower()}"
        return f"{round(n * f, 6):g}{base}"
    return re.sub(r"[^\w+#.%/ ]", "", ART.sub("", v.lower())).strip()


MONTHS = r"(?:Jan(?:uary)?|Feb(?:ruary)?|Mar(?:ch)?|Apr(?:il)?|May|Jun(?:e)?|Jul(?:y)?|Aug(?:ust)?|Sep(?:t(?:ember)?)?|Oct(?:ober)?|Nov(?:ember)?|Dec(?:ember)?)"
DATEVAL = r"(?:" + MONTHS + r"\s+\d{1,2}(?:,?\s+\d{4})?|\d{4}-\d{2}-\d{2})"
NUMVAL = r"(?:" + DATEVAL + r"|\$?\d[\d,]*(?:\.\d+)?(?:\s*[A-Za-z%]+)?(?:\s+per\s+[a-z]+)?)"
_OLD_NUMVAL = r"\$?\d[\d,]*(?:\.\d+)?(?:\s*[A-Za-z%]+)?(?:\s+per\s+[a-z]+)?"
QUAL = r"(?:\s+(?:for|in|on|at|after|when|according|by|during|if|as|from|before|until|with|of|under|across)\b[^.;]*)?"
PATTERNS: list[tuple[re.Pattern, str]] = []


def _p(rx: str, kind: str):
    PATTERNS.append((re.compile(rx, re.I), kind))


# decision: "We chose X for/as Y" / "decided to use X for Y"
_p(r"^(?:we|the team|i|they)\s+(?:have\s+)?(?:decided|chose|selected|adopted|picked|settled)\s+(?:on\s+|to use\s+|to adopt\s+|to go with\s+|on using\s+)?(?P<v>[^,.;]+?)\s+(?:for|as|to be)\s+(?P<s>[^,.;]+?)\s*[.;]?$", "chosen")
_p(r"^(?:we|the team|i|they)\s+(?:will\s+|now\s+)?(?:use|adopt|go with)\s+(?P<v>[^,.;]+?)\s+(?:for|as)\s+(?P<s>[^,.;]+?)\s*[.;]?$", "chosen")
# replacement / migration
_p(r"^(?:we\s+|the team\s+)?(?:have\s+)?(?:migrated|switched|moved)\s+from\s+(?P<old>[^,.;]+?)\s+to\s+(?P<v>[^,.;]+?)(?:\s+(?:for|in|on)\s+(?P<s>[^,.;]+?))?\s*[.;]?$", "replaced")
_p(r"^(?:we\s+)?(?:have\s+)?replaced\s+(?P<old>[^,.;]+?)\s+with\s+(?P<v>[^,.;]+?)(?:\s+(?:for|in|on)\s+(?P<s>[^,.;]+?))?\s*[.;]?$", "replaced")
_p(r"^(?P<v>[^,.;]+?)\s+replaces\s+(?P<old>[^,.;]+?)\s*[.;]?$", "replaced")
# attribute: "The default timeout is 30 seconds" / "X's timeout is 30 s" / "The timeout of X is 30 s"
_p(r"^(?:the\s+)?(?P<attr>[\w\- ]{2,40}?)\s+(?:of|for)\s+(?P<s>[^,.;]+?)\s+(?:is|was|are|=)\s+(?:set to\s+|now\s+)?(?:(?P<v>" + NUMVAL + r")" + QUAL + r"|(?P<v2>[^,;]+?))\s*[.;]?$", "attr_of")
_p(r"^(?P<s>(?-i:[A-Z])[\w\.\-]*(?: (?-i:[A-Z])[\w\.\-]*){0,3})'s\s+(?P<attr>[\w\- ]{2,40}?)\s+(?:is|was|are)\s+(?:set to\s+|now\s+)?(?:(?P<v>" + NUMVAL + r")" + QUAL + r"|(?P<v2>[^,;]+?))\s*[.;]?$", "attr_of")
_p(r"^(?:the\s+)?(?P<s>[\w\- ]{3,50}?)\s+(?:is|are|was|will be)\s+(?:now\s+|set to\s+|currently\s+)?(?:(?P<v>" + NUMVAL + r")" + QUAL + r"|(?P<v2>(?-i:[A-Z])[\w\.\+#\-]*(?: (?-i:[A-Z])[\w\.\+#\-]*){0,2}))\s*[.;]?$", "is")
# runs on / hosted on / deployed on / written in
_p(r"^(?:the\s+)?(?P<s>[\w\.\- ]{2,50}?)\s+(?P<neg>does not |doesn't |no longer )?(?P<p>runs on|is hosted on|is deployed (?:on|to)|is written in|stores (?:its |all )?[\w ]{2,20} in)\s+(?P<v>[^,.;]+?)(?:\s+(?:and|but|which|while|so|because)\b.*)?\s*[.;]?$", "verb_func")
# numeric attribute verbs
_p(r"^(?:the\s+)?(?P<s>[\w\.\- ]{2,50}?)\s+(?P<p>allows|limits|caps|handles|supports|has|have|retains|keeps)\s+(?:up to |a maximum of |a limit of |at most |a max of )?(?P<v>\$?\d[\d,]*(?:\.\d+)?\s*[A-Za-z%]+(?:\s+per\s+[a-z]+)?)\s*[.;]?$", "numeric")
# uses / requires / supports (multi-valued; with polarity for contradiction vs negation)
_p(r"^(?:the\s+)?(?P<s>[\w\.\-]+(?: [\w\.\-]+){0,3}?)\s+" + NEG + r"?\s*(?P<p>uses?|requires?|supports?|depends on|relies on)\s+(?P<v>[^,.;]+?)(?:\s+(?:and|but|which|while|so|because)\b.*)?\s*[.;]?$", "verb_multi")


def _mk(subject, predicate, value, text, functional, polarity=1, conf=0.6) -> ParsedClaim | None:
    subject, value = _clean_np(subject), _clean_np(value)
    if not subject or not value or len(subject) > 60 or len(value) > 60:
        return None
    if len(subject.split()) > 6 or len(value.split()) > 8:
        return None
    return ParsedClaim(text, subject, predicate, value, name_key(subject), name_key(predicate), name_key(norm_value(value)),
                       functional, polarity, conf)


def parse_sentence(sent: str) -> list[ParsedClaim]:
    s = re.sub(r"^\s*(?:[-*>]|\d+[.)])\s+", "", sent.strip())
    s = re.sub(r"\*\*|__|`", "", s)
    if len(s) < 12 or len(s) > 260 or s.endswith("?"):
        return []
    out: list[ParsedClaim] = []
    for rx, kind in PATTERNS:
        m = rx.match(s)
        if not m:
            continue
        g = m.groupdict()
        if kind == "chosen":
            c = _mk(g["s"], "chosen", g["v"], s, True, 1, 0.7)
        elif kind == "replaced":
            # subject = the NEW thing, value = the OLD thing it supersedes
            c = _mk(g["v"], "replaced", g["old"], s, False, 1, 0.75)
        elif kind == "attr_of":
            c = _mk(g["s"], _clean_np(g["attr"]).lower(), g.get("v") or g["v2"], s, True, 1, 0.6)
        elif kind == "is":
            if re.match(r"(this|that|it|there|what|which)\b", g["s"].strip(), re.I):
                continue
            c = _mk(g["s"], "is", g.get("v") or g["v2"], s, True, 1, 0.5)
        elif kind == "verb_func":
            c = _mk(g["s"], re.sub(r"^stores .*? in$", "stores in", g["p"].lower()).replace("is ", ""), g["v"], s, True,
                    -1 if g.get("neg") else 1, 0.6)
        elif kind == "numeric":
            if g["p"].lower() in ("has", "have") and not re.search(r"\d", g["v"]):
                continue
            c = _mk(g["s"], g["p"].lower() + " limit" if g["p"].lower() in ("allows", "limits", "caps", "handles", "supports") else "has", g["v"], s, True, 1, 0.6)
        elif kind == "verb_multi":
            pol = -1 if g.get("neg") else 1
            c = _mk(g["s"], g["p"].lower().rstrip("s") if g["p"].lower().endswith("s") and not g["p"].lower().endswith("ss") else g["p"].lower(), g["v"], s, False, pol, 0.55)
        else:
            c = None
        if c:
            out.append(c)
            break           # first matching pattern wins
    return out


def extract_claims(text: str, max_claims: int = 40) -> list[ParsedClaim]:
    claims: list[ParsedClaim] = []
    seen = set()
    for s in sentences(text):
        for c in parse_sentence(s):
            k = (c.subj_key, c.pred_key, c.val_key)
            if k in seen:
                continue
            seen.add(k)
            claims.append(c)
            if len(claims) >= max_claims:
                return claims
    return claims


def key_sentences(text: str, n: int = 2) -> list[str]:
    """Salient assertive sentences kept as unstructured, source-traceable claims."""
    cands = []
    for s in sentences(text):
        s = re.sub(r"^\s*(?:[-*>]|\d+[.)])\s+", "", s.strip())
        if 40 <= len(s) <= 240 and not s.endswith("?") and re.search(r"\b(is|are|was|were|has|have|uses?|supports?|requires?|chose|decided|runs?|achieves?|reduces?|increases?)\b", s):
            score = (2 if re.search(r"\d", s) else 0) + (1 if re.search(r"\b[A-Z][a-z]+[A-Z]|\b[A-Z]{2,}\b", s) else 0)
            cands.append((score, s))
    cands.sort(key=lambda x: -x[0])
    return [s for _, s in cands[:n]]
