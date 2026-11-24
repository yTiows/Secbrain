"""Heuristic knowledge-type classification with calibrated-ish confidence.

Each type has weighted cue patterns over the heading path and body. Returns the winning type, a confidence
derived from margin over the runner-up, and the evidence cues (inspectable). An optional LLM can refine it.
"""
from __future__ import annotations
import re

from ..text import sentences

R = lambda p: re.compile(p, re.I)

CUES: dict[str, list[tuple[re.Pattern, float]]] = {
    "decision": [(R(r"\b(we|i|team) (decided|chose|selected|adopted|picked|agreed|settled)\b"), 3), (R(r"\bdecision\b"), 2.5),
                 (R(r"\b(adr|rationale|trade-?offs?)\b"), 2), (R(r"\bwe will (use|adopt|go with|move)\b"), 2.5),
                 (R(r"\b(chosen|selected) (over|instead of)\b"), 2), (R(r"\b(migrat\w+|switch\w+) (from|to)\b"), 1.5)],
    "procedure": [(R(r"^\s*(\d+[.)]|step \d+)\s+\S"), 2.2), (R(r"\b(how to|steps?|procedure|runbook|playbook|checklist|walkthrough)\b"), 2),
                  (R(r"\bfirst,? .*\bthen\b"), 1.5)],
    "instruction": [(R(r"\b(always|never|must not|must|do not|don't|should not|shouldn't|make sure|ensure)\b"), 1.4),
                    (R(r"\b(guidelines?|rules?|policy|conventions?|requirements?|best practices?)\b"), 2)],
    "preference": [(R(r"\bi (prefer|like|love|hate|dislike|favou?r|enjoy)\b"), 3), (R(r"\bmy (favou?rite|preferred)\b"), 3),
                   (R(r"\bpreferences?\b"), 2)],
    "experience": [(R(r"\b(i|we) (learned|tried|ran into|struggled|noticed|spent|discovered|realized|went)\b"), 2.2),
                   (R(r"\b(post-?mortem|retro(spective)?|incident|lessons? learned|outage|what went wrong)\b"), 2.5),
                   (R(r"\b(yesterday|last (week|month|year|sprint)|ago)\b"), 1)],
    "hypothesis": [(R(r"\bhypothes[ie]s\b"), 3), (R(r"\b(we|i) (suspect|assume|believe|conjecture|guess)\b"), 2.5),
                   (R(r"\b(might|may|could) (be|explain|cause|improve|reduce)\b"), 1.4), (R(r"\bif .{3,60} then\b"), 1.5),
                   (R(r"\b(probably|presumably|likely)\b"), 1)],
    "question": [(R(r"\?\s*$"), 2.5), (R(r"\b(open question|unclear|unknown|todo: investigate|need to find out|should we)\b"), 2.2)],
    "research": [(R(r"\b(study|paper|survey|literature|findings?|benchmark(s|ed)?|experiments?|et al\.?|dataset|evaluation|results? show)\b"), 2),
                 (R(r"\b(arxiv|doi:|journal|abstract|methodology)\b"), 2.5),
                 (R(r"\b(accuracy|precision|recall|latency|throughput|p\d\d|f1)\b.{0,40}\d"), 1.8)],
    "reference": [(R(r"https?://\S+"), 1.2), (R(r"\b(references?|bibliography|further reading|see also|links?|resources)\b"), 2.2)],
    "project": [(R(r"\b(project|roadmap|milestones?|deadline|sprint|scope|deliverables?|stakeholders?|timeline|release plan)\b"), 1.8),
                (R(r"\b(goals?|objectives?|status):"), 1.2)],
    "idea": [(R(r"\b(idea|what if|brainstorm|proposal|we could|i could|maybe we|concept for|wouldn't it be)\b"), 2.2)],
    "observation": [(R(r"\b(observed|noticed|seems? to|appears? to|logs? show|measured|saw that|it looks like)\b"), 2.2)],
    "concept": [(R(r"\b(is a|is an|refers to|is defined as|are a type of|is the (process|practice|concept|idea) of|means)\b"), 1.6),
                (R(r"\b(definition|overview|introduction to|glossary|what is)\b"), 2)],
    "fact": [(R(r"\b(is|are|was|were|has|have|uses?|supports?|requires?|runs? on)\b.{0,60}\d"), 1.0)],
}

HEADING_HINT = {
    "decision": ["decision", "decisions", "adr", "rationale", "chosen"],
    "procedure": ["how to", "steps", "procedure", "runbook", "setup", "installation", "deploy", "checklist", "process"],
    "instruction": ["guidelines", "rules", "conventions", "policy", "requirements"],
    "experience": ["retrospective", "postmortem", "post-mortem", "incident", "lessons"],
    "hypothesis": ["hypothesis", "hypotheses"],
    "question": ["questions", "open questions"],
    "research": ["research", "findings", "results", "benchmark", "benchmarks", "evaluation", "experiment", "experiments", "study"],
    "reference": ["references", "links", "resources", "further reading", "bibliography"],
    "project": ["project", "roadmap", "milestones", "timeline", "goals", "scope"],
    "idea": ["ideas", "proposal", "brainstorm"],
    "concept": ["overview", "background", "definition", "glossary", "concepts"],
    "preference": ["preferences"],
    "observation": ["observations", "notes"],
}


def _numbered_lines(body: str) -> int:
    return len(re.findall(r"^\s*(?:\d+[.)]|step \d+[:.]?)\s+\S", body, re.I | re.M))


def classify(title: str, body: str, heading_path: list[str] | None = None) -> dict:
    scores: dict[str, float] = {t: 0.0 for t in CUES}
    why: dict[str, list[str]] = {t: [] for t in CUES}
    head = (heading_path[-1] if heading_path else title).lower()
    for t, hints in HEADING_HINT.items():
        for h in hints:
            if re.search(rf"\b{re.escape(h)}\b", head):
                scores[t] += 2.5
                why[t].append(f"heading:{h}")
                break
    sample = body[:3000]
    for t, cues in CUES.items():
        for rx, w in cues:
            hits = rx.findall(sample) if rx.flags & re.M else None
            if rx.flags & re.M:
                n = len(list(rx.finditer(sample)))
            else:
                n = len(rx.findall(sample)) if t != "question" else len(re.findall(r"\?\s*(?:\n|$)", sample))
            if n:
                add = w * min(1 + 0.35 * (n - 1), 2.2)
                scores[t] += add
                why[t].append(rx.pattern[:28])
    # structural signals
    nn = _numbered_lines(sample)
    if nn >= 3:
        scores["procedure"] += 3.5
        why["procedure"].append(f"numbered-steps:{nn}")
    sents = sentences(sample)
    if sents:
        q = sum(1 for s in sents if s.rstrip().endswith("?"))
        if q / len(sents) >= 0.5:
            scores["question"] += 3
            why["question"].append("mostly-questions")
    urls = len(re.findall(r"https?://", sample))
    words = max(1, len(sample.split()))
    if urls >= 3 and urls * 12 > words * 0.4:
        scores["reference"] += 3
        why["reference"].append("link-list")
    ranked = sorted(scores.items(), key=lambda kv: -kv[1])
    (t1, s1), (t2, s2) = ranked[0], ranked[1]
    if s1 < 1.8:
        # no strong signal: assertive short text → fact, otherwise observation
        typ = "fact" if len(sample) < 600 and not sample.rstrip().endswith("?") else "observation"
        return {"type": typ, "confidence": 0.35, "scores": {}, "cues": []}
    conf = min(0.95, 0.4 + 0.5 * (s1 - s2) / max(s1, 1.0) + min(s1, 8) / 40)
    return {"type": t1, "confidence": round(conf, 3), "scores": {k: round(v, 2) for k, v in ranked[:3]}, "cues": why[t1][:5]}


def classify_document(title: str, body: str) -> str:
    return classify(title, body, [title])["type"]
