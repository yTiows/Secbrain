"""Rollback: every logged change carries an inverse payload; `undo` replays it."""
from __future__ import annotations
from .db import Database, jdump, jload, now
from .graph import GraphEngine
from .knowledge import KnowledgeStore, NotFound, Invalid


class ChangeManager:
    def __init__(self, db: Database, store: KnowledgeStore, graph: GraphEngine):
        self.db, self.store, self.graph = db, store, graph

    def undo(self, change_id: str, actor: str = "user") -> dict:
        r = self.db.one("SELECT * FROM changes WHERE id=?", (change_id,))
        if not r:
            raise NotFound(change_id)
        if r["undone"]:
            raise Invalid("change already undone")
        u = jload(r["undo"], {})
        if not u or u.get("op") in (None, "none"):
            raise Invalid("this change is irreversible (no inverse recorded)")
        with self.db.tx():
            self._apply(u, actor)
            self.db.exec("UPDATE changes SET undone=1 WHERE id=?", (change_id,))
            self.store.log_change(actor, "undo", change_id, f"undid: {r['summary']}", {"op": "none"})
        return {"undone": change_id, "summary": r["summary"]}

    def _apply(self, u: dict, actor: str):
        op = u["op"]
        st = self.store
        if op == "composite":
            for sub in reversed(u["ops"]):
                self._apply(sub, actor)
        elif op == "restore_snapshot":
            snap = dict(u["snapshot"])
            st.update_object(u["object_id"], snap, actor=actor, reason="undo", kind="undo", log_change=False)
        elif op == "remove_tags":
            with self.db.tx():
                for t in u["tags"]:
                    self.db.exec("DELETE FROM tags WHERE object_id=? AND tag=?", (u["object_id"], t))
                st.update_object(u["object_id"], {}, actor=actor)            # no-op guard
                st._index(u["object_id"], observe=False)
        elif op == "edge_status":
            self.graph.update_edge(u["edge_id"], {"status": u["status"]}, actor=actor, log=False)
        elif op == "edge_restore":
            e = u["edge"]
            patch = {k: e[k] for k in ("verification", "explanation", "confidence", "status") if k in e}
            cur = self.graph.get_edge(e["id"])
            if cur["type"] != e["type"]:
                patch["type"] = e["type"]
            self.graph.update_edge(e["id"], patch, actor=actor, log=False)
        elif op == "claim_status":
            self.db.exec("UPDATE claims SET status=? WHERE id=?", (u["status"], u["claim_id"]))
        elif op == "unmerge":
            self._unmerge(u, actor)
        elif op == "delete_object":
            st.set_status(u["object_id"], "deleted", actor=actor, reason="undo create")
        else:
            raise Invalid(f"unknown undo op {op}")

    def _unmerge(self, u: dict, actor: str):
        st = self.store
        loser, survivor = u["loser"], u["survivor"]
        st.update_object(loser, u["loser_snapshot"], actor=actor, reason="undo merge", kind="undo", log_change=False)
        for eid, before in u["moved_edges"].items():
            self.db.exec("UPDATE edges SET src=?, dst=?, status=?, updated_at=? WHERE id=?", (before["src"], before["dst"], before["status"], now(), eid))
        for cid in u["moved_claims"]:
            self.db.exec("UPDATE claims SET object_id=? WHERE id=?", (loser, cid))
        for sid in u["added_sources"]:
            self.db.exec("DELETE FROM object_sources WHERE object_id=? AND source_id=?", (survivor, sid))
        for t in u["added_tags"]:
            self.db.exec("DELETE FROM tags WHERE object_id=? AND tag=?", (survivor, t))
        for a in u["added_aliases"]:
            self.db.exec("DELETE FROM aliases WHERE object_id=? AND alias_key=?", (survivor, a))
        st._index(survivor, observe=False)
        st._index(loser, observe=False)
