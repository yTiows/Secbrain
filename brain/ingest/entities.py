"""Entity & alias extraction (heuristic, precision-oriented)."""
from __future__ import annotations
import re
from collections import defaultdict
from dataclasses import dataclass, field

from ..text import key as name_key

WIKILINK = re.compile(r"\[\[([^\]|#]+)(?:#[^\]|]*)?(?:\|([^\]]+))?\]\]")
_W = r"[A-Z][\w\-]*"
_CONN = r"(?: (?:of|for|the) | )"
ACRO_DEF = re.compile(r"\b(" + _W + r"(?:" + _CONN + _W + r"){0,5})[ ]\(([A-Z][A-Za-z0-9]{1,9}s?)\)")
ACRO_DEF2 = re.compile(r"\b([A-Z][A-Za-z0-9]{1,9})[ ]\((" + _W + r"(?:" + _CONN + _W + r"){1,5})\)")
_N = r"[A-Z][\w+#\-]*(?:\.[A-Za-z][\w+#]*)*"
AKA = re.compile(r"\b(" + _N + r"(?: " + _N + r"){0,3}),?[ ]+(?:also (?:known|called|referred to) as|a\.?k\.?a\.?|or simply|commonly (?:called|known as)|short for)[ ]+[\"“']?(" + _N + r"(?: " + _N + r"){0,3})")
CAP_PHRASE = re.compile(r"(?<![\w\-/@.])(" + _N + r"(?: " + _N + r"){0,3})(?![\w])")
TECH_TOKEN = re.compile(r"\b([A-Za-z][A-Za-z0-9]*(?:\.[A-Za-z]{1,6}|\+\+|#))(?![\w])")
ALLCAPS = re.compile(r"(?<![\w.])([A-Z][A-Z0-9]{1,6})\b(?!\.\w)")

STOP_CAPS = set("""
The A An This That These Those We I You He She It They Our My Your Their His Her Its In On At For With From By To Of As If When Then Else
However But And Or So Because Since While Also Here There Each Every Some Many Most Any All No Not Note Notes See Use Using Run Set Add Check
Monday Tuesday Wednesday Thursday Friday Saturday Sunday January February March April May June July August September October November December
Overview Summary Introduction Conclusion Background Goals Goal Steps Step Results Result Context Decision Decisions Status Problem Solution Rationale
Todo Fixme Warning Important Example Examples Appendix Details Description Purpose Scope Requirements Next Open Question Questions Idea Ideas
Yes Yes. After Before During Once Today Yesterday Tomorrow Currently Previously Finally First Second Third Next Last Another Other Both Either
What Why How Who Which Whose Should Could Would Can Will Does Do Did Is Are Was Were Has Have Had Let Make Take Keep Get Given Based Due Per
Only Just Still Even Instead Already Always Never Often Sometimes Usually Therefore Thus Hence Overall Additionally Furthermore Moreover Meanwhile
Version Phase Section Table Figure Chapter Part Item Option Options Case Type Types Level Mode Team Project Release Sprint Day Week Month Year
""".split())
STOP_ACRO = set("""OK NOTE TODO FIXME ASAP FYI AM PM US EU UK ID IDS NA TBD TBA ETA OR AND NOT THE FOR YES NO IF IS IT AS AT BY OF ON TO IN""".split())


@dataclass
class Cand:
    display: str
    key: str
    count: int = 0
    strong: bool = False
    kind: str = "name"
    aliases: set = field(default_factory=set)
    mid_count: int = 0          # occurrences not at sentence start
    contexts: list = field(default_factory=list)


def _is_techy(tok: str) -> bool:
    return bool(re.search(r"[a-z][A-Z]|[A-Z]{2,}|\d|\.|\+|#", tok)) and not tok.isdigit()


def _strip_stop_prefix(phrase: str) -> str:
    parts = re.split(r"([ \-])", phrase)
    words = parts[0::2]
    while words and (words[0] in STOP_CAPS):
        words = words[1:]
        parts = parts[2:]
    return "".join(parts).strip(" -")


def _sentence_starts(text: str) -> set[int]:
    starts = {0}
    for m in re.finditer(r"(?:[.!?]\s+|\n\s*(?:[-*>]\s+|\d+[.)]\s+)?)", text):
        starts.add(m.end())
    return starts


def extract_entities(text: str, hint_entities: list[str] | None = None) -> dict[str, Cand]:
    cands: dict[str, Cand] = {}

    def get(display: str, kind: str = "name", strong: bool = False) -> Cand:
        k = name_key(display)
        c = cands.get(k)
        if c is None:
            c = cands[k] = Cand(display=display.strip(), key=k, kind=kind)
        if strong:
            c.strong = True
        return c

    def add_alias(a: Cand, alias: str):
        if name_key(alias) and name_key(alias) != a.key:
            a.aliases.add(alias.strip())

    for h in hint_entities or []:
        if isinstance(h, str) and h.strip():
            get(h, strong=True).count += 1
    for m in WIKILINK.finditer(text):
        c = get(m.group(1).strip(), strong=True)
        c.count += 1
        if m.group(2):
            add_alias(c, m.group(2))
    # definitions / aliases
    for rx, swap in ((ACRO_DEF, False), (ACRO_DEF2, True)):
        for m in rx.finditer(text):
            long_, short = (m.group(2), m.group(1)) if swap else (m.group(1), m.group(2))
            long_ = _strip_stop_prefix(long_.strip())
            short = short.strip()[:-1] if re.fullmatch(r"[A-Z]{2,}s", short.strip()) else short.strip()
            if not long_:
                continue
            initials = "".join(w[0] for w in re.split(r"[\s\-]+", long_) if w and w.lower() not in ("of", "and", "for", "the"))
            if short.upper() == initials.upper()[: len(short)] or initials.upper().startswith(short.upper()[:2]) or short.isupper():
                c = get(long_, strong=True)
                c.count += 1
                add_alias(c, short)
                c.kind = "concept-term"
    for m in AKA.finditer(text):
        c = get(m.group(1), strong=True)
        c.count += 1
        add_alias(c, m.group(2))
    # capitalized phrases
    starts = _sentence_starts(text)
    heading_spans = [(m.start(), m.end()) for m in re.finditer(r"^#{1,6}\s+.*$", text, re.M)]
    for m in CAP_PHRASE.finditer(text):
        if any(a <= m.start() < b for a, b in heading_spans):
            continue
        phrase = _strip_stop_prefix(m.group(1))
        if not phrase:
            continue
        # strip trailing sentence period captured by [\w\.]
        phrase = phrase.rstrip(".")
        if not phrase or phrase in STOP_CAPS:
            continue
        words_ = re.split(r"[ \-]", phrase)
        at_start = m.start() in starts or (m.start() - 1 >= 0 and text[:m.start()].rstrip().endswith((":", "-", "*", ">")) and False)
        single = len(words_) == 1
        if single and phrase.isupper():
            continue                                   # acronyms handled below
        if single and at_start and not _is_techy(phrase):
            c = cands.get(name_key(phrase))
            if c:                                       # seen elsewhere: ok
                c.count += 1
            else:
                t = cands.setdefault("~" + name_key(phrase), Cand(phrase, name_key(phrase), 0, False, "name", set(), 0))
                t.count += 1
            continue
        if single and len(phrase) < 3:
            continue
        c = get(phrase, "technology" if (single and _is_techy(phrase)) else "name")
        c.count += 1
        if not at_start:
            c.mid_count += 1
        if len(c.contexts) < 2:
            c.contexts.append(text[max(0, m.start() - 60): m.end() + 60].replace("\n", " "))
    # promote sentence-start-only singles that also appeared mid-sentence
    lower_words = set(re.findall(r"\b[a-z][a-z0-9]+\b", text))
    for k in [k for k in cands if k.startswith("~")]:
        tmp = cands.pop(k)
        real = cands.get(tmp.key)
        if real:
            real.count += tmp.count
        elif tmp.count >= 2 and len(tmp.display) >= 4 and tmp.display.lower() not in lower_words and tmp.display not in STOP_CAPS:
            cands[tmp.key] = tmp                         # capitalized every time, never lowercase: a proper noun
    for m in TECH_TOKEN.finditer(text):
        tok = m.group(1)
        if tok.lower().endswith((".md", ".py", ".txt", ".json", ".yml", ".yaml", ".sh", ".js", ".ts", ".html", ".css", ".com", ".org", ".io", ".net", ".pdf", ".docx")) and tok.lower() not in ("node.js", "next.js", "vue.js", "d3.js", "express.js", "asp.net"):
            continue
        if re.search(r"\d+\.\d+", tok):
            continue
        if re.fullmatch(r"[a-z]+\.[a-z]{1,3}", tok):
            continue
        if tok[0].isupper() or tok.endswith(("++", "#")) or tok.lower().endswith(".js"):
            c = get(tok, "technology")
            c.count += 1
    for m in ALLCAPS.finditer(text):
        a = m.group(1)
        if a in STOP_ACRO or a.isdigit() or a in STOP_CAPS:
            continue
        if any(s <= m.start() < e for s, e in heading_spans) and len(a) < 4:
            continue
        c = get(a, "acronym")
        c.count += 1
        c.mid_count += 1
    # merge candidates whose key is covered by a multiword candidate's alias
    for c in list(cands.values()):
        for al in c.aliases:
            k = name_key(al)
            other = cands.get(k)
            if other and other is not c:
                c.count += other.count
                c.mid_count += other.mid_count
                c.strong = c.strong or other.strong
                del cands[k]
    return cands


def significant(c: Cand, doc_len: int) -> bool:
    """Within-document significance: strong signal, repeated, or distinctive token form."""
    if c.strong:
        return True
    if c.count >= 2:
        return True
    return False


def find_known(text: str, known: dict[str, str]) -> dict[str, int]:
    """Scan `text` for already-known entity names/aliases (key → object id). Returns id → count.
    Matches word n-grams (n ≤ 5) on normalized keys, case-insensitively."""
    out: dict[str, int] = defaultdict(int)
    if not known:
        return out
    toks = re.findall(r"[\w\.\+#]+(?:-[\w\.\+#]+)*", text)
    n = len(toks)
    maxlen = 5
    for i in range(n):
        acc = ""
        for j in range(i, min(n, i + maxlen)):
            acc += name_key(toks[j].replace("-", ""))
            oid = known.get(acc)
            if oid:
                out[oid] += 1
    return out
