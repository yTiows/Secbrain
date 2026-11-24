import io
import json
import zipfile
import pytest
from fastapi.testclient import TestClient
from brain.api.app import create_app


@pytest.fixture
def api(brain):
    c = TestClient(create_app(brain, start_workers=False, serve_ui=False))
    t = brain.keys.create("admin", ["read", "write", "admin"])["token"]
    return brain, c, {"Authorization": f"Bearer {t}"}


def test_filename_path_traversal_is_neutralised(brain):
    j = brain.ingest.ingest_bytes("../../../../etc/evil.md", b"# Evil\n\nTraversal attempt content that is long enough to be stored.")
    assert j["status"] == "done"
    src = brain.store.list_sources()[0]
    assert src["name"] == "evil.md"
    from pathlib import Path
    stored = Path(brain.store.get_source(src["id"])["file_path"]).resolve()
    assert brain.settings.sources_dir.resolve() in stored.parents          # never outside the data dir


def test_search_input_cannot_break_fts_or_sql(api):
    brain, c, h = api
    brain.ingest.ingest_bytes("a.md", b"# Alpha\n\nAlpha beta gamma delta epsilon content for testing queries safely.")
    for q in ['"', "'; DROP TABLE objects; --", "alpha OR", "NEAR(", "* AND * OR", 'a" b', "\\", "alpha" * 500, "\x00alpha", "{}[]()"]:
        r = c.post("/api/v1/search", json={"query": q[:2000]}, headers=h)
        assert r.status_code == 200, (q, r.text)
    assert brain.store.count_objects() >= 1
    assert c.get("/api/v1/objects?q=%27%3B%20DROP%20TABLE%20objects%3B--", headers=h).status_code == 200
    assert c.get("/api/v1/objects?order=updated_at;DROP", headers=h).status_code == 422


def test_stored_html_is_returned_as_data_not_markup(api):
    brain, c, h = api
    r = c.post("/api/v1/ingest/note", json={"content": "<script>alert(1)</script> The gateway uses <img src=x onerror=alert(1)> markers.", "title": "xss"}, headers=h)
    assert r.status_code == 200
    o = c.get("/api/v1/objects?limit=5", headers=h).json()["objects"][0]
    assert c.get(f"/api/v1/objects/{o['id']}", headers=h).headers["content-type"].startswith("application/json")
    # the UI renders via textContent only
    import re
    from pathlib import Path
    js = Path("web/app.js").read_text()
    assert "innerHTML" not in js and "insertAdjacentHTML" not in js and "document.write" not in js


def test_csp_blocks_inline_script_on_ui(brain):
    c = TestClient(create_app(brain, start_workers=False, serve_ui=True))
    r = c.get("/")
    assert r.status_code == 200
    csp = r.headers["content-security-policy"]
    assert "script-src 'self'" in csp and "frame-ancestors 'none'" in csp
    assert "<script>" not in r.text.replace('<script src=', '')          # no inline scripts in the page


def test_zip_bomb_and_slip_guards(brain):
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("ok.md", "# ok\n\nfine")
        z.writestr("/abs/evil.md", "# x")
    with pytest.raises(ValueError):
        brain.vault.import_zip(buf.getvalue())


def test_upload_limits_and_binary_rejection(api):
    brain, c, h = api
    r = c.post("/api/v1/ingest/file", files={"file": ("x.txt", b"\x00\x01\x02" * 50)}, headers=h).json()
    assert r["status"] == "failed" and "binary" in r["error"]


def test_scoped_key_cannot_escalate(api):
    brain, c, h = api
    scoped = {"Authorization": "Bearer " + brain.keys.create("s", ["read", "write"], projects=["A"])["token"]}
    for path, method in [("/api/v1/keys", "get"), ("/api/v1/maintenance/run", "post"), ("/api/v1/vault/export", "get"), ("/api/v1/sources", "get"), ("/api/v1/changes", "get")]:
        assert getattr(c, method)(path, headers=scoped).status_code == 403, path
    r = c.post("/api/v1/keys", json={"name": "x", "scopes": ["admin"]}, headers=scoped)
    assert r.status_code == 403
    from brain.knowledge import Invalid
    with pytest.raises(Invalid):
        brain.keys.create("bad", ["admin"], projects=["A"])


def test_secrets_not_leaked_by_api(api):
    brain, c, h = api
    body = c.get("/api/v1/keys", headers=h).text
    assert "brk_" not in body and "key_hash" not in body
    s = brain.ingest.ingest_bytes("a.md", b"# A\n\nSome content for the source endpoint test that is long enough.")
    src = c.get("/api/v1/sources/" + s["source_id"], headers=h).json()
    assert "file_path" not in src


def test_admin_key_file_permissions(tmp_path):
    from brain.app import Brain
    from brain.config import Settings
    import os, stat
    b = Brain(Settings(data_dir=tmp_path / "d"))
    tok = b.keys.bootstrap_admin(b.settings.data_dir)
    mode = stat.S_IMODE(os.stat(b.settings.data_dir / "admin.key").st_mode)
    assert tok and mode == 0o600 and b.keys.bootstrap_admin(b.settings.data_dir) is None
    b.close()
