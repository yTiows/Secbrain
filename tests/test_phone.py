import os, stat, time
from pathlib import Path
from brain.autopilot import Autopilot
from brain import phone


def test_inbox_autoingest_moves_files_and_never_deletes(brain):
    ap = Autopilot(brain)
    (ap.inbox / "Atlas").mkdir()
    a = ap.inbox / "Atlas" / "note.md"
    a.write_text("# Retry policy\n\nWe decided to retry failed webhooks five times because partners are flaky.")
    bad = ap.inbox / "broken.pdf"; bad.write_bytes(b"%PDF-1.4 junk")
    ignored = ap.inbox / "photo.png"; ignored.write_bytes(b"\x89PNG")
    fresh = ap.inbox / "writing.md"; fresh.write_text("# still being written\n\ncontent that is long enough to be ingested later on.")
    old = time.time() - 10
    for f in (a, bad, ignored):
        os.utime(f, (old, old))
    assert ap.scan_inbox() == 1
    o = [x for x in brain.store.list_objects() if x["project"] == "Atlas"]
    assert o and o[0]["type"] == "decision"                              # sub-folder became the project
    assert not a.exists() and list((ap.inbox / "processed").rglob("note.md"))
    assert (ap.inbox / "failed" / "broken.pdf").exists() and (ap.inbox / "failed" / "broken.pdf.error.txt").read_text()
    assert ignored.exists() and fresh.exists()                           # unsupported and in-flight files are left alone
    assert ap.scan_inbox() == 0


def test_autopilot_backup_prunes_to_keep(brain):
    ap = Autopilot(brain, keep_backups=2)
    for i in range(4):
        brain.settings.backups_dir.joinpath(f"brain-2020010{i}-000000.sqlite").write_bytes(b"x")
    ap.backup()
    assert len(list(brain.settings.backups_dir.glob("brain-*.sqlite"))) == 2


def test_phone_key_and_link(brain):
    t1 = phone.phone_key(brain); t2 = phone.phone_key(brain)
    assert t1 == t2 and brain.keys.verify(t1)["scopes"] == ["admin", "read", "write"]
    assert stat.S_IMODE(os.stat(brain.settings.data_dir / "phone.key").st_mode) == 0o600
    assert phone.login_link("https://x.ts.net/", t1) == f"https://x.ts.net/#key={t1}"
    brain.keys.revoke(brain.keys.verify(t1)["id"])
    assert phone.phone_key(brain) != t1                                    # revoked key is replaced automatically
    assert phone.qr_ascii("https://x/#key=brk_a")


def test_manifest_and_service_worker_served(brain):
    from fastapi.testclient import TestClient
    from brain.api.app import create_app
    c = TestClient(create_app(brain, start_workers=False))
    m = c.get("/manifest.webmanifest")
    assert m.status_code == 200 and "manifest" in m.headers["content-type"]
    j = m.json(); assert j["display"] == "standalone" and j["share_target"]["method"] == "GET"
    assert c.get("/sw.js").status_code == 200 and c.get("/icons/icon-512.png").status_code == 200
    assert "/api/" in c.get("/sw.js").text                                # service worker explicitly bypasses the API
