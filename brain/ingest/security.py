"""Untrusted-input hygiene. Documents are DATA; nothing here lets a document instruct the system."""
from __future__ import annotations
import re

INJECTION_PATTERNS = [
    (re.compile(r"ignore (all |any |the )?(previous|prior|above|earlier) (instructions|prompts?|rules)", re.I), "override-instructions"),
    (re.compile(r"disregard (all |any |the )?(previous|prior|above|system) (instructions|prompts?)", re.I), "override-instructions"),
    (re.compile(r"(reveal|print|show|leak|exfiltrate) (your|the) (system prompt|api[ _-]?keys?|secrets?|credentials|vault|database)", re.I), "exfiltration"),
    (re.compile(r"you are now (a |an |in )?(developer|dan|jailbreak|unrestricted)", re.I), "role-hijack"),
    (re.compile(r"(delete|erase|wipe|purge) (all|every|the entire) (notes?|knowledge|vault|data|objects?|sources?)", re.I), "destructive-instruction"),
    (re.compile(r"(grant|give) (yourself|this document|this file) (access|admin|permissions?)", re.I), "privilege-escalation"),
    (re.compile(r"<\s*/?\s*(system|assistant|im_start|im_end)\s*>", re.I), "chat-markup"),
]

_CTRL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")


def sanitize_text(text: str) -> str:
    """Remove control characters / NULs; never alters meaning-bearing text."""
    return _CTRL.sub("", text.replace("\r\n", "\n").replace("\r", "\n"))


def scan(text: str) -> list[str]:
    """Return flags for injection-like passages. Flags are advisory metadata: they mark the source and
    resulting objects as suspicious and never cause any action to be taken based on the text."""
    flags = []
    for rx, name in INJECTION_PATTERNS:
        if rx.search(text):
            flags.append(f"injection:{name}")
    return sorted(set(flags))
