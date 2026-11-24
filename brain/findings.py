"""Findings: detected issues/opportunities with explanation, confidence and autonomy level."""
from __future__ import annotations
from .db import Database, jdump, jload, now
from .knowledge import new_id

LEVELS = ("safe", "conditional", "protected")


def add_finding(db: Database, *, kind: str, level: str, title: str, explanation: str, confidence: float,
                targets: list[str] | None = None, payload: dict | None = None, severity: str = "info",
                dedupe_key: str | None = None) -> dict | None:
    """Idempotent on dedupe_key: an open or dismissed finding is never re-created; returns None when it already exists."""
    assert level in LEVELS
    with db.tx():
        if dedupe_key and db.one("SELECT 1 FROM findings WHERE dedupe_key=?", (dedupe_key,)):
            return None
        fid = new_id("f")
        db.exec("INSERT INTO findings(id,kind,level,status,confidence,severity,title,explanation,payload,targets,dedupe_key,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                (fid, kind, level, "open", round(confidence, 3), severity, title, explanation, jdump(payload or {}),
                 jdump(targets or []), dedupe_key, now()))
        return get_finding(db, fid)


def _row(r) -> dict:
    d = dict(r)
    d["payload"] = jload(d["payload"], {})
    d["targets"] = jload(d["targets"], [])
    return d


def get_finding(db: Database, fid: str) -> dict:
    r = db.one("SELECT * FROM findings WHERE id=?", (fid,))
    if not r:
        raise KeyError(fid)
    return _row(r)


def list_findings(db: Database, status: str | None = "open", kind: str | None = None, limit: int = 200) -> list[dict]:
    sql, args = "SELECT * FROM findings WHERE 1=1", []
    if status:
        sql += " AND status=?"; args.append(status)
    if kind:
        sql += " AND kind=?"; args.append(kind)
    sql += " ORDER BY created_at DESC LIMIT ?"; args.append(limit)
    return [_row(r) for r in db.all(sql, args)]
