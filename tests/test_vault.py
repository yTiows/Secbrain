import zipfile, io
import pytest
from pathlib import Path
from tests.test_ingest import DB_DOC


def test_export_import_roundtrip_creates_versions_not_duplicates(brain, tmp_path):
    brain.ingest.ingest_bytes("db.md", DB_DOC.encode())
    n_before = brain.store.count_objects(None)
    out = tmp_path / "vault"
    info = brain.vault.export_to_dir(out)
    assert info["notes"] == n_before and info["attachments"] == 1
    files = list(out.rglob("*.md"))
    dec = [f for f in files if "Rationale" in f.name][0]
    txt = dec.read_text()
    assert txt.startswith("---\nid: k_") and "[[" in txt and "brain:generated:start" in txt and "source: db.md" in txt
    # edit externally (Obsidian user): change the body + add a tag
    edited = txt.replace("The default timeout is 30 seconds.", "The default timeout is 45 seconds.").replace("tags: [", "tags: [reviewed, ")
    dec.write_text(edited)
    res = brain.vault.import_directory(out)
    assert res["failed"] == []
    assert brain.store.count_objects(None) == n_before                      # no duplicates
    oid = txt.split("id: ")[1].split("\n")[0]
    o = brain.store.get_object(oid)
    assert "45 seconds" in o["content"] and "reviewed" in o["tags"] and o["version"] == 2
    assert "30 seconds" in brain.store.versions(oid)[1]["snapshot"]["content"]   # history kept
    # re-import unchanged: no new versions
    brain.vault.import_directory(out)
    assert brain.store.get_object(oid)["version"] == 2


def test_import_external_obsidian_vault_with_wikilinks_and_frontmatter(brain, tmp_path):
    v = tmp_path / "ext"
    (v / "Projects").mkdir(parents=True)
    (v / ".obsidian").mkdir()
    (v / ".obsidian" / "app.json").write_text("{}")
    (v / "Projects" / "Billing.md").write_text("---\ntags: [finance]\naliases: [Invoices]\n---\n# Billing\n\nBilling runs nightly and uses [[Stripe]] to charge customers. We decided to retry failed charges three times.\n")
    (v / "Stripe.md").write_text("# Stripe\n\nStripe is the payment processor used for card payments in the billing system today.\n")
    res = brain.vault.import_directory(v, folders_as_projects=True)
    assert res["failed"] == [] and res["files"] == 2
    billing = [o for o in brain.store.list_objects(limit=50) if o["title"].startswith("Billing")][0]
    assert billing["project"] == "Projects" and "finance" in billing["tags"]
    edges = brain.graph.edges_of(billing["id"], "out")
    assert any(e["type"] == "references" and e["verification"] == "verified" and "Stripe" in e["other_title"] for e in edges)


def test_zip_export_import_and_zip_slip_guard(brain):
    brain.ingest.ingest_bytes("db.md", DB_DOC.encode())
    z = brain.vault.export_zip()
    names = zipfile.ZipFile(io.BytesIO(z)).namelist()
    assert "README.md" in names and any(n.startswith("attachments/") for n in names)
    evil = io.BytesIO()
    with zipfile.ZipFile(evil, "w") as zz:
        zz.writestr("../../escape.md", "# x\n\nmalicious path")
    with pytest.raises(ValueError):
        brain.vault.import_zip(evil.getvalue())


def test_backup_is_restorable(brain, tmp_path):
    brain.ingest.ingest_bytes("db.md", DB_DOC.encode())
    path = brain.vault.backup(brain.settings)
    import sqlite3
    c = sqlite3.connect(path)
    assert c.execute("SELECT COUNT(*) FROM objects").fetchone()[0] == brain.store.count_objects(None)
