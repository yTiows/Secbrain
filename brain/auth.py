"""API keys: random 256-bit tokens, only SHA-256 hashes are stored. Scopes: read < write < admin; optional project/type restrictions."""
from __future__ import annotations
import hashlib
import os
import secrets
from pathlib import Path

from .db import Database, jdump, jload, now
from .knowledge import new_id, Invalid

SCOPES = ("read", "write", "admin")


def _hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


class KeyManager:
    def __init__(self, db: Database):
        self.db = db

    def create(self, name: str, scopes: list[str], projects: list[str] | None = None, types: list[str] | None = None) -> dict:
        if not name.strip():
            raise Invalid("name required")
        bad = set(scopes) - set(SCOPES)
        if bad or not scopes:
            raise Invalid(f"scopes must be a non-empty subset of {SCOPES}")
        if "admin" in scopes and (projects or types):
            raise Invalid("admin keys cannot be project/type scoped")
        token = "brk_" + secrets.token_urlsafe(32)
        kid = new_id("key")
        with self.db.tx():
            self.db.exec("INSERT INTO api_keys(id,name,key_hash,scopes,projects,types,created_at) VALUES(?,?,?,?,?,?,?)",
                         (kid, name.strip(), _hash(token), jdump(sorted(set(scopes))), jdump(projects or []), jdump(types or []), now()))
        return {"id": kid, "name": name, "token": token, "scopes": sorted(set(scopes)), "projects": projects or [], "types": types or []}

    def verify(self, token: str | None) -> dict | None:
        if not token or not token.startswith("brk_"):
            return None
        r = self.db.one("SELECT * FROM api_keys WHERE key_hash=? AND revoked_at IS NULL", (_hash(token),))
        if not r:
            return None
        return {"id": r["id"], "name": r["name"], "scopes": jload(r["scopes"], []), "projects": jload(r["projects"], []), "types": jload(r["types"], [])}

    def touch(self, kid: str):
        try:
            self.db.exec("UPDATE api_keys SET last_used_at=? WHERE id=?", (now(), kid))
        except Exception:
            pass

    def list(self) -> list[dict]:
        return [{"id": r["id"], "name": r["name"], "scopes": jload(r["scopes"], []), "projects": jload(r["projects"], []), "types": jload(r["types"], []),
                 "created_at": r["created_at"], "revoked_at": r["revoked_at"], "last_used_at": r["last_used_at"]}
                for r in self.db.all("SELECT * FROM api_keys ORDER BY created_at")]

    def revoke(self, kid: str) -> bool:
        with self.db.tx():
            return self.db.exec("UPDATE api_keys SET revoked_at=? WHERE id=? AND revoked_at IS NULL", (now(), kid)).rowcount > 0

    def bootstrap_admin(self, data_dir: Path) -> str | None:
        """First start: create an admin key and write it (0600) to data/admin.key. Returns the token only when newly created."""
        if self.db.one("SELECT 1 FROM api_keys WHERE revoked_at IS NULL AND scopes LIKE '%admin%'"):
            return None
        k = self.create("bootstrap-admin", ["read", "write", "admin"])
        p = Path(data_dir) / "admin.key"
        fd = os.open(p, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as f:
            f.write(k["token"] + "\n")
        return k["token"]
