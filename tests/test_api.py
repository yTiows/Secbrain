import io
import json
import pytest
from fastapi.testclient import TestClient
from brain.api.app import create_app
from tests.test_ingest import DB_DOC
from tests.helpers import make_docx, make_pdf

AUTH_DOC = """---
project: Atlas
date: 2025-02-25
---
# Auth service

The auth service issues JSON Web Token (JWT) credentials to clients after login. The access token lifetime is 15 minutes. Clients renew access with refresh tokens.

## Sessions
The auth service uses Redis for the session store and token revocation list.
"""
SEC_DOC = """---
project: Atlas
date: 2025-07-10
---
# Security review

The access token lifetime is 30 minutes according to the production configuration we audited.
"""
ORION_DOC = """---
project: Orion
---
# Orion vectors

We decided to use pgvector for vector search in Orion because it runs inside PostgreSQL and avoids a separate database.
"""
PASTA = "# Pasta\n\nBoil salted water and cook the spaghetti for ten minutes, then toss it with olive oil and garlic."


@pytest.fixture
def api(brain):
    app = create_app(brain, start_workers=False, serve_ui=False)
    c = TestClient(app)
    admin = brain.keys.create("admin", ["read", "write", "admin"])["token"]
    return brain, c, {"Authorization": f"Bearer {admin}"}


def load(brain):
    for n, t in [("db.md", DB_DOC), ("auth.md", AUTH_DOC), ("sec.md", SEC_DOC), ("orion.md", ORION_DOC), ("pasta.md", PASTA)]:
        assert brain.ingest.ingest_bytes(n, t.encode())["status"] == "done"
    brain.store.reindex()


def test_auth_required_and_scopes(api):
    brain, c, h = api
    assert c.get("/api/v1/health").status_code == 200
    assert c.get("/api/v1/objects").status_code == 401
    assert c.get("/api/v1/objects", headers={"Authorization": "Bearer brk_nope"}).status_code == 401
    ro = brain.keys.create("reader", ["read"])["token"]
    hr = {"Authorization": f"Bearer {ro}"}
    assert c.get("/api/v1/objects", headers=hr).status_code == 200
    assert c.post("/api/v1/ingest/note", json={"content": "x y z"}, headers=hr).status_code == 403
    assert c.get("/api/v1/maintenance/findings", headers=hr).status_code == 403
    assert c.get("/api/v1/vault/export", headers=hr).status_code == 403
    assert c.get("/api/v1/keys", headers=hr).status_code == 403
    # revoked keys stop working immediately
    k = brain.keys.create("tmp", ["read"])
    assert c.get("/api/v1/objects", headers={"Authorization": f"Bearer {k['token']}"}).status_code == 200
    brain.keys.revoke(k["id"])
    assert c.get("/api/v1/objects", headers={"Authorization": f"Bearer {k['token']}"}).status_code == 401
    # tokens are stored hashed
    assert not brain.db.one("SELECT 1 FROM api_keys WHERE key_hash=?", (k["token"],))


def test_security_headers_and_error_shapes(api):
    brain, c, h = api
    r = c.get("/api/v1/objects/k_missing", headers=h)
    assert r.status_code == 404 and r.json()["error"] == "not_found"
    assert r.headers["x-content-type-options"] == "nosniff" and r.headers["cache-control"] == "no-store"
    assert c.patch("/api/v1/objects/k_x", json={"importance": 5}, headers=h).status_code == 422       # validation
    assert c.post("/api/v1/search", json={"query": ""}, headers=h).status_code == 422


def test_scoped_keys_cannot_see_other_projects(api):
    brain, c, h = api
    load(brain)
    scoped = brain.keys.create("atlas-agent", ["read", "write"], projects=["Atlas"])["token"]
    hs = {"Authorization": f"Bearer {scoped}"}
    r = c.post("/api/v1/search", json={"query": "pgvector vector search PostgreSQL"}, headers=hs).json()
    titles = " ".join(x["title"] for x in r["results"])
    assert "Orion" not in titles and all(x["project"] == "Atlas" for x in r["results"])
    orion = [o for o in brain.store.list_objects() if o["project"] == "Orion"][0]
    assert c.get(f"/api/v1/objects/{orion['id']}", headers=hs).status_code == 404       # out of scope looks non-existent
    pkg = c.post("/api/v1/context", json={"query": "pgvector for Orion"}, headers=hs).json()
    assert all("Orion" not in (i.get("project") or "") for i in pkg["items"])
    ov = c.get("/api/v1/graph/overview", headers=hs).json()
    assert ov["nodes"] and all(n["project"] == "Atlas" for n in ov["nodes"])
    assert c.get("/api/v1/graph/stats", headers=hs).status_code == 403
    assert c.post("/api/v1/ingest/note", json={"content": "A new fact about something relevant here.", "project": "Orion"}, headers=hs).status_code == 403
    r = c.post("/api/v1/ingest/note", json={"content": "A new fact about the gateway behaviour in production."}, headers=hs)
    assert r.status_code == 200 and brain.store.list_objects(project="Atlas", order="created_at DESC")[0]["origin"] == "external"
    # external submissions are not presented as verified facts
    assert brain.store.list_objects(project="Atlas", order="created_at DESC")[0]["verification"] == "ai_extracted"


def test_retrieval_endpoints_return_traceable_explained_packages(api):
    brain, c, h = api
    load(brain)
    r = c.post("/api/v1/search", json={"query": "What is the access token lifetime?"}, headers=h).json()
    assert r["results"] and all(x["sources"] and x["why"] for x in r["results"])
    pkg = c.post("/api/v1/context", json={"query": "What is the access token lifetime?", "token_budget": 800}, headers=h).json()
    assert pkg["version"] == "1" and pkg["items"] and pkg["conflicts"], pkg                 # 15 vs 30 minutes surfaced
    assert pkg["stats"]["tokens_est"] <= 800
    assert all(i["sources"][0]["name"] for i in pkg["items"])
    cf = pkg["conflicts"][0]
    assert cf["preferred"] and "newer" in cf["basis"]
    # irrelevant knowledge is not retrieved
    names = {s["name"] for i in pkg["items"] for s in i["sources"]}
    assert "pasta.md" not in names and "orion.md" not in names
    r = c.post("/api/v1/search", json={"query": "quantum chromodynamics lattice"}, headers=h).json()
    assert r["results"] == []


def test_token_budget_is_respected_and_measured(api):
    brain, c, h = api
    load(brain)
    for budget in (300, 600, 1200):
        pkg = c.post("/api/v1/context", json={"query": "auth service tokens sessions Redis PostgreSQL decision", "token_budget": budget, "k": 10}, headers=h).json()
        from brain.text import estimate_tokens
        assert estimate_tokens(json.dumps(pkg, separators=(",", ":"))) <= budget + 15, budget


def test_ingest_endpoints_all_formats_and_failure_reporting(api):
    brain, c, h = api
    for name, data in [("a.md", AUTH_DOC.encode()), ("p.docx", make_docx()), ("r.pdf", make_pdf()), ("c.json", b'{"title":"Cfg","limit":"100 requests per minute"}')]:
        r = c.post("/api/v1/ingest/file", files={"file": (name, data)}, headers=h)
        assert r.status_code == 200 and r.json()["status"] == "done", (name, r.text)
    bad = c.post("/api/v1/ingest/file", files={"file": ("bad.pdf", b"%PDF-1.4 junk")}, headers=h).json()
    assert bad["status"] == "failed" and bad["error"]
    assert c.post("/api/v1/ingest/file", files={"file": ("x.exe", b"MZ")}, headers=h).status_code == 422
    j = c.post("/api/v1/ingest/file", files={"file": ("a.md", AUTH_DOC.encode())}, headers=h).json()
    assert j["status"] == "unchanged"
    brain.settings.max_upload_bytes = 100
    assert c.post("/api/v1/ingest/file", files={"file": ("big.txt", b"x" * 500)}, headers=h).status_code == 413


def test_review_mode_via_api(api):
    brain, c, h = api
    r = c.post("/api/v1/ingest/note", json={"content": "We decided to use Kafka for events because throughput matters.", "review": True}, headers=h).json()
    assert r["status"] == "awaiting_review" and r["result"]["objects"] and brain.store.count_objects(None) == 0
    r2 = c.post(f"/api/v1/jobs/{r['id']}/approve", headers=h).json()
    assert r2["status"] == "done" and brain.store.count_objects() >= 1


def test_corrections_persist_with_history_and_undo(api):
    brain, c, h = api
    load(brain)
    o = [x for x in brain.store.list_objects(limit=200) if x["title"] == "Security review — overview" or x["title"] == "Security review"][0]
    r = c.patch(f"/api/v1/objects/{o['id']}", json={"content": "The access token lifetime is 15 minutes after the fix.", "verification": "user_verified", "reason": "fixed after review"}, headers=h)
    assert r.status_code == 200 and r.json()["version"] == 2
    got = c.get(f"/api/v1/objects/{o['id']}", headers=h).json()
    assert "after the fix" in got["content"] and got["verification"] == "user_verified"
    hist = c.get(f"/api/v1/objects/{o['id']}/history", headers=h).json()
    assert [v["version"] for v in hist["versions"]][:2] == [2, 1]
    # relationship corrections persist (reject → tombstone)
    rel = c.get(f"/api/v1/objects/{o['id']}/relationships", headers=h).json()["relationships"]
    ai = [e for e in rel if e["origin"] == "ai"][0]
    assert c.patch(f"/api/v1/relationships/{ai['id']}", json={"verification": "rejected"}, headers=h).status_code == 200
    rel2 = c.get(f"/api/v1/objects/{o['id']}/relationships", headers=h).json()["relationships"]
    assert next(e for e in rel2 if e["id"] == ai["id"])["verification"] == "rejected"
    # app restart keeps corrections
    from brain.app import Brain
    b2 = Brain(brain.settings)
    assert "after the fix" in b2.store.get_object(o["id"])["content"]
    b2.close()
    ch = c.get("/api/v1/changes", headers=h).json()["changes"]
    edit = [x for x in ch if x["kind"] == "object_update"][0]
    assert c.post(f"/api/v1/changes/{edit['id']}/undo", headers=h).status_code == 200
    assert "after the fix" not in c.get(f"/api/v1/objects/{o['id']}", headers=h).json()["content"]


def test_delete_requires_two_step_for_purge(api):
    brain, c, h = api
    o = brain.store.create_object(type="fact", title="Tmp", content="temporary object")
    assert c.delete(f"/api/v1/objects/{o['id']}?purge=true&confirm={o['id']}", headers=h).status_code == 422     # not soft-deleted yet
    assert c.delete(f"/api/v1/objects/{o['id']}", headers=h).json()["status"] == "deleted"
    assert c.post(f"/api/v1/objects/{o['id']}/restore", headers=h).json()["status"] == "active"
    c.delete(f"/api/v1/objects/{o['id']}", headers=h)
    assert c.delete(f"/api/v1/objects/{o['id']}?purge=true&confirm=wrong", headers=h).status_code == 422
    assert c.delete(f"/api/v1/objects/{o['id']}?purge=true&confirm={o['id']}", headers=h).json() == {"purged": o["id"]}


def test_proposals_flow(api):
    brain, c, h = api
    o = brain.store.create_object(type="fact", title="Doc", content="Original")
    w = {"Authorization": "Bearer " + brain.keys.create("agent", ["read", "write"])["token"]}
    p = c.post("/api/v1/propose/update", json={"object_id": o["id"], "patch": {"content": "Agent suggestion"}, "rationale": "saw newer info"}, headers=w).json()
    assert brain.store.get_object(o["id"])["content"] == "Original"               # proposals never write directly
    assert c.post(f"/api/v1/proposals/{p['id']}/apply", headers=w).status_code == 403
    assert c.post(f"/api/v1/proposals/{p['id']}/apply", headers=h).json()["status"] == "applied"
    assert brain.store.get_object(o["id"])["content"] == "Agent suggestion"


def test_maintenance_and_vault_endpoints(api):
    brain, c, h = api
    load(brain)
    r = c.post("/api/v1/maintenance/run", headers=h)
    assert r.status_code == 200 and "duplicates" in r.json()
    f = c.get("/api/v1/maintenance/findings?kind=contradiction", headers=h).json()["findings"]
    assert f
    intel = c.get("/api/v1/intelligence/summary", headers=h).json()
    assert intel["stats"]["objects"] > 5 and intel["open_findings"]
    z = c.get("/api/v1/vault/export", headers=h)
    assert z.status_code == 200 and z.headers["content-type"] == "application/zip" and len(z.content) > 1000
    imp = c.post("/api/v1/vault/import", files={"file": ("v.zip", z.content)}, data={"folders_as_projects": "false"}, headers=h)
    assert imp.status_code == 200 and imp.json()["failed"] == []


def test_external_agent_end_to_end_definition_of_done(api):
    """The scenario from the product definition of done, through the public API only."""
    brain, c, h = api
    docs = {"db.md": DB_DOC, "auth.md": AUTH_DOC, "sec.md": SEC_DOC, "orion.md": ORION_DOC, "pasta.md": PASTA}
    for n, t in docs.items():
        assert c.post("/api/v1/ingest/file", files={"file": (n, t.encode())}, headers=h).json()["status"] == "done"
    # originals preserved & downloadable
    src = c.get("/api/v1/sources", headers=h).json()["sources"]
    assert len(src) == 5
    raw = c.get(f"/api/v1/sources/{[s for s in src if s['name'] == 'db.md'][0]['id']}/raw", headers=h)
    assert raw.content == DB_DOC.encode()
    # graph reflects stored relationships
    st = c.get("/api/v1/graph/stats", headers=h).json()
    assert st["edges"] > 5 and st["nodes"] > 8
    # related docs connected, unrelated pasta not tied to the tech docs
    pasta = [o for o in brain.store.list_objects() if o["title"].startswith("Pasta")][0]
    others = {e["other_title"] for e in brain.graph.edges_of(pasta["id"]) if e["type"] not in ("part_of",)}
    assert not any("Auth" in t or "Database" in t for t in others)
    # question → knowledge with sources, explanation, relationships, uncertainty
    pkg = c.post("/api/v1/context", json={"query": "How long does an access token last, and what changed?", "token_budget": 1000}, headers=h).json()
    assert pkg["items"] and pkg["conflicts"] and all(i["why"] and i["sources"] for i in pkg["items"])
    # indirect connection: PostgreSQL links Atlas DB decision to Orion's pgvector decision
    pkg2 = c.post("/api/v1/context", json={"query": "Which projects depend on PostgreSQL?", "token_budget": 1500, "k": 10}, headers=h).json()
    projects = {i.get("project") for i in pkg2["items"]}
    assert {"Atlas", "Orion"} <= projects
    # correction persists + visible to another consumer using a scoped read key
    o = pkg["items"][0]
    c.patch(f"/api/v1/objects/{o['id']}", json={"importance": 0.95, "reason": "important"}, headers=h)
    reader = {"Authorization": "Bearer " + brain.keys.create("claude", ["read"])["token"]}
    assert c.get(f"/api/v1/objects/{o['id']}", headers=reader).json()["importance"] == 0.95
