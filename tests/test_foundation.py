import pytest
from brain.app import Brain
from brain.config import Settings
from brain.knowledge import Invalid, NotFound


def test_persistence_across_reopen(tmp_path):
    s = Settings(data_dir=tmp_path / "d")
    b = Brain(s)
    o = b.store.create_object(type="decision", title="Use PostgreSQL", content="We chose PostgreSQL for storage.",
                              tags=["db", "#Storage"], aliases=["Postgres"])
    oid = o["id"]
    b.close()
    b2 = Brain(s)
    got = b2.store.get_object(oid)
    assert got["title"] == "Use PostgreSQL" and got["tags"] == ["db", "storage"] and got["aliases"] == ["Postgres"]
    assert b2.store.embedding_of(oid) is not None
    assert b2.db.one("SELECT COUNT(*) c FROM fts WHERE fts MATCH 'postgresql'")["c"] == 1
    b2.close()


def test_validation(brain):
    with pytest.raises(Invalid):
        brain.store.create_object(type="bogus", title="x")
    with pytest.raises(Invalid):
        brain.store.create_object(type="fact", title="  ")
    with pytest.raises(Invalid):
        brain.store.create_object(type="fact", title="x", importance=1.5)
    with pytest.raises(NotFound):
        brain.store.get_object("k_missing")


def test_versions_and_restore(brain):
    st = brain.store
    o = st.create_object(type="fact", title="Port", content="The port is 8080.")
    st.update_object(o["id"], {"content": "The port is 9090."}, reason="correction")
    st.update_object(o["id"], {"importance": 0.9})
    vs = st.versions(o["id"])
    assert [v["version"] for v in vs] == [3, 2, 1]
    assert vs[2]["snapshot"]["content"] == "The port is 8080."           # nothing silently erased
    restored = st.restore_version(o["id"], 1)
    assert restored["content"] == "The port is 8080." and restored["version"] == 4
    assert st.versions(o["id"])[1]["snapshot"]["content"] == "The port is 9090."


def test_noop_update_creates_no_version(brain):
    o = brain.store.create_object(type="fact", title="A", content="b")
    brain.store.update_object(o["id"], {"content": "b"})
    assert brain.store.get_object(o["id"])["version"] == 1


def test_soft_delete_purge_safeguards(brain):
    st = brain.store
    o = st.create_object(type="fact", title="Temp", content="x")
    with pytest.raises(Invalid):
        st.purge_object(o["id"], confirm=o["id"])        # must be soft-deleted first
    st.set_status(o["id"], "deleted")
    assert st.db.one("SELECT COUNT(*) c FROM fts WHERE object_id=?", (o["id"],))["c"] == 0
    with pytest.raises(Invalid):
        st.purge_object(o["id"], confirm="nope")
    st.set_status(o["id"], "active")                     # recoverable
    assert st.get_object(o["id"])["status"] == "active"
    st.set_status(o["id"], "deleted")
    st.purge_object(o["id"], confirm=o["id"])
    with pytest.raises(NotFound):
        st.get_object(o["id"])


def test_sources_are_content_addressed_and_idempotent(brain):
    st = brain.store
    s1, a = st.add_source(kind="file", name="a.txt", data=b"hello", text="hello")
    s2, b = st.add_source(kind="file", name="a.txt", data=b"hello", text="hello")
    s3, c = st.add_source(kind="file", name="a.txt", data=b"hello v2", text="hello v2")
    assert (a, b, c) == ("new", "unchanged", "new_version")
    assert s1["id"] == s2["id"] and s3["supersedes"] == s1["id"]
    assert st.read_source_bytes(s1["id"]) == b"hello"            # original preserved untouched
    assert st.read_source_bytes(s3["id"]) == b"hello v2"


def test_transaction_rollback_leaves_no_partial_state(brain):
    st = brain.store
    n0 = st.count_objects()
    with pytest.raises(RuntimeError):
        with brain.db.tx():
            st.create_object(type="fact", title="ghost", content="x")
            raise RuntimeError("boom")
    assert st.count_objects() == n0
    assert brain.db.one("SELECT COUNT(*) c FROM fts WHERE fts MATCH 'ghost'")["c"] == 0


def test_reindex_rebuilds_derived_data(brain):
    st = brain.store
    o = st.create_object(type="fact", title="Alpha", content="alpha beta gamma")
    brain.db.exec("DELETE FROM fts"); brain.db.exec("DELETE FROM embeddings")
    r = st.reindex()
    assert r["objects"] == 1
    assert st.embedding_of(o["id"]) is not None
    assert brain.db.one("SELECT COUNT(*) c FROM fts WHERE fts MATCH 'alpha'")["c"] == 1


def test_embedding_cache_tracks_status_and_discards_rolled_back_state(brain):
    st = brain.store
    a = st.create_object(type="fact", title="Alpha", content="alpha beta gamma delta")
    v = st.embedder.embed("alpha beta gamma delta")
    assert [i for i, _ in st.similar(v, k=5)] == [a["id"]]
    st.set_status(a["id"], "archived")
    assert st.similar(v, k=5) == []                              # archived objects leave the candidate matrix
    st.set_status(a["id"], "active")
    assert [i for i, _ in st.similar(v, k=5)] == [a["id"]]
    # a dry-run ingestion creates objects inside a transaction that is rolled back: no phantom vectors may survive
    brain.ingest.ingest_bytes("d.md", b"# Dry\n\nAlpha beta gamma delta epsilon zeta eta theta content for the dry run only.", dry_run=True)
    ids = [i for i, _ in st.similar(v, k=10)]
    assert ids == [a["id"]] and st.count_objects() == 1
    b = st.create_object(type="fact", title="Beta", content="alpha beta gamma delta epsilon")
    assert {i for i, _ in st.similar(v, k=10)} == {a["id"], b["id"]}
