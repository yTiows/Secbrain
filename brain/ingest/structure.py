"""Identify document structure: split text into sections by markdown headings (or paragraph groups)."""
from __future__ import annotations
import re
from dataclasses import dataclass, field

from ..text import sentences, truncate

HEAD = re.compile(r"^(#{1,6})\s+(.+?)\s*#*\s*$", re.M)
MAX_SECTION_CHARS = 1800
MIN_SECTION_CHARS = 40


@dataclass
class Section:
    title: str
    body: str
    level: int
    path: list[str] = field(default_factory=list)   # heading path
    index: int = 0

    @property
    def key(self) -> str:
        return " > ".join(self.path) if self.path else self.title


def _split_long(sec: Section) -> list[Section]:
    if len(sec.body) <= MAX_SECTION_CHARS:
        return [sec]
    paras = [p for p in re.split(r"\n\s*\n", sec.body) if p.strip()]
    out, buf = [], ""
    for p in paras:
        if buf and len(buf) + len(p) > MAX_SECTION_CHARS:
            out.append(buf)
            buf = p
        else:
            buf = (buf + "\n\n" + p) if buf else p
    if buf:
        out.append(buf)
    if len(out) == 1:        # one giant paragraph: split by sentences
        sents, out, buf = sentences(sec.body), [], ""
        for s in sents:
            if buf and len(buf) + len(s) > MAX_SECTION_CHARS:
                out.append(buf); buf = s
            else:
                buf = (buf + " " + s) if buf else s
        if buf:
            out.append(buf)
    res = []
    for i, chunk in enumerate(out):
        res.append(Section(f"{sec.title} (part {i + 1})", chunk, sec.level, sec.path[:-1] + [f"{sec.title} (part {i + 1})"] if sec.path else [f"{sec.title} (part {i + 1})"]))
    return res


def sectionize(text: str, doc_title: str) -> tuple[str, list[Section]]:
    """Returns (document title, sections). Sections with body shorter than MIN_SECTION_CHARS are merged into their parent."""
    text = text.strip("\n")
    heads = list(HEAD.finditer(text))
    # strip code-fence false positives
    fenced = [(m.start(), m.end()) for m in re.finditer(r"```.*?```", text, re.S)]
    heads = [h for h in heads if not any(a <= h.start() < b for a, b in fenced)]
    title = doc_title
    sections: list[Section] = []
    if not heads:
        paras = [p.strip() for p in re.split(r"\n\s*\n", text) if p.strip()]
        if not title:
            title = truncate(paras[0].split("\n")[0], 80) if paras else "Untitled"
        sec = Section(title, text.strip(), 1, [title])
        return title, _split_long(sec)
    first = heads[0]
    pre = text[: first.start()].strip()
    # H1 as title if first heading is level 1
    if first.group(1) == "#" and not title:
        title = first.group(2).strip()
    stack: list[tuple[int, str]] = []
    bounds = [(h, heads[i + 1].start() if i + 1 < len(heads) else len(text)) for i, h in enumerate(heads)]
    if pre:
        sections.append(Section("Introduction" if title else truncate(pre.split("\n")[0], 60), pre, 1, ["Introduction"]))
    for h, end in bounds:
        lvl, name = len(h.group(1)), h.group(2).strip()
        while stack and stack[-1][0] >= lvl:
            stack.pop()
        stack.append((lvl, name))
        body = text[h.end():end].strip()
        sections.append(Section(name, body, lvl, [n for _, n in stack]))
    # merge tiny sections into previous one so we don't create content-free objects
    merged: list[Section] = []
    for s in sections:
        if len(s.body) < MIN_SECTION_CHARS:
            if merged and s.body:
                merged[-1].body += f"\n\n{s.title}: {s.body}"
            elif not s.body:
                continue        # empty heading, skip (its children carry the content)
            else:
                merged.append(s)
        else:
            merged.append(s)
    out: list[Section] = []
    for s in merged:
        out.extend(_split_long(s))
    # drop the H1-only section if it merely repeats the title with no body
    out = [s for s in out if s.body.strip()]
    for i, s in enumerate(out):
        s.index = i
    if not title:
        title = out[0].title if out else "Untitled"
    return title, out
