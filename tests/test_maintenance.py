import pytest
from brain.knowledge import Invalid
from brain.findings import list_findings

A = "# Cache policy\n\nThe session cache uses Memcached with explicit TTLs for every key stored by the auth service in production."
A_COPY = "# Cache policy notes\n\nThe session cache uses Memcached with explicit TTLs for every key stored by the auth service in production environments."


def ing(b, name, text, **kw):
    return b.ingest.ingest_bytes(name, text.encode(), **kw)


def test_duplicate_detection_and_reversible_merge(brain):
    m = brain.maintenance
    ing(brain, "a.md", A)
    # near-duplicate that slips under the ingest evidence threshold
    brain.store.create_object(type="fact", title="Cache policy", content="The session cache uses Memcached with explicit TTLs for every key stored by the auth service in production environments today.",
                              origin="import")
    r = m.run(["duplicates"])
    assert r["duplicates"]["candidates"] >= 1
    f = [x for x in m.list(kind="duplicate")][0]
    assert f["level"] == "conditional" and f["status"] == "open" and "cosine" in f["explanation"]
    a, b = f["targets"]
    brain.graph.add_edge(a, brain.store.create_object(type="fact", title="Other", content="other object about something else entirely")["id"], "related_to", origin="user")
    before = {x: brain.store.get_object(x) for x in (a, b)}
    res = m.apply(f["id"], actor="user")
    arch = [x for x in (a, b) if brain.store.get_object(x)["status"] == "archived"]
    assert len(arch) == 1 and brain.store.get_object(arch[0])["extra"]["merged_into"] in (a, b)
    # history preserved: the loser is archived, not deleted
    assert brain.store.versions(arch[0])
    # undo restores both objects
    brain.maintenance.changes.undo(res["change_id"])
    assert brain.store.get_object(arch[0])["status"] == "active"
    assert brain.store.get_object(arch[0])["title"] == before[arch[0]]["title"]


def test_safe_actions_auto_conditional_do_not(brain):
    m = brain.maintenance
    o = brain.store.create_object(type="fact", title="Plain object", content="This object has plenty of text but no summary and no tags at all so maintenance should add both.")
    r = m.run(["summaries_tags", "relationships"])
    assert r["summaries_tags"]["summaries_added"] == 1 and r["summaries_tags"]["tags_added"] == 1
    got = brain.store.get_object(o["id"])
    assert got["summary"] and got["tags"]
    ch = brain.store.list_changes()
    assert any(c["kind"] == "tags_added" and c["actor"] == "maintenance" for c in ch)
    # undo the tags
    tagchg = [c for c in ch if c["kind"] == "tags_added"][0]
    brain.maintenance.changes.undo(tagchg["id"])
    assert brain.store.get_object(o["id"])["tags"] == []
    # conditional findings stay open without approval
    ing(brain, "a.md", A); brain.store.create_object(type="fact", title="Cache policy", content="The session cache uses Memcached with explicit TTLs for every key stored by the auth service in production environments today.")
    m.run(["duplicates"])
    assert all(f["status"] == "open" for f in m.list(kind="duplicate"))


def test_protected_findings_cannot_be_applied(brain):
    ing(brain, "x.md", "# X\n\nSome content that is long enough to be stored as an object in the vault for testing.")
    # tamper with the stored original -> integrity finding
    sid = brain.store.list_sources()[0]["id"]
    from pathlib import Path
    Path(brain.store.get_source(sid)["file_path"]).write_bytes(b"tampered")
    r = brain.maintenance.run(["sources"])
    assert r["sources"]["broken"] == 1
    f = brain.maintenance.list(kind="broken_source")[0]
    assert f["level"] == "protected"
    with pytest.raises(Invalid):
        brain.maintenance.apply(f["id"])


def test_contradiction_resolution_and_rollback(brain):
    ing(brain, "old.md", "---\ndate: 2025-01-01\n---\n# Old\n\nThe default timeout is 30 seconds for all gateway requests.")
    ing(brain, "new.md", "---\ndate: 2025-09-01\n---\n# New\n\nThe default timeout is 60 seconds for all gateway requests.")
    f = brain.maintenance.list(kind="contradiction")[0]
    assert f["level"] == "conditional" and f["confidence"] >= 0.8
    old = [o for o in brain.store.list_objects() if o["title"] == "Old"][0]
    new = [o for o in brain.store.list_objects() if o["title"] == "New"][0]
    res = brain.maintenance.apply(f["id"])
    assert brain.store.get_object(old["id"])["status"] == "superseded" and brain.store.get_object(new["id"])["status"] == "active"
    assert any(e["type"] == "replaces" and e["src"] == new["id"] for e in brain.graph.edges_of(old["id"]))
    brain.maintenance.changes.undo(res["change_id"])
    assert brain.store.get_object(old["id"])["status"] == "active"
    assert not any(e["type"] == "replaces" for e in brain.graph.edges_of(old["id"]))
    assert any(e["type"] == "contradicts" for e in brain.graph.edges_of(old["id"]))        # contradiction edge restored
    with pytest.raises(Invalid):
        brain.maintenance.changes.undo(res["change_id"])                                     # no double undo


def test_replacement_detection_marks_outdated(brain):
    ing(brain, "a.md", "# Cache\n\nWe chose Redis for session caching in the gateway.")
    ing(brain, "b.md", "# Cache update\n\nWe replaced Redis with Memcached for session caching in the gateway.")
    outdated = brain.maintenance.list(kind="outdated")
    assert outdated and outdated[0]["level"] == "conditional"
    res = brain.maintenance.apply(outdated[0]["id"])
    assert brain.store.get_object(outdated[0]["payload"]["supersede"])["status"] == "superseded"
    brain.maintenance.changes.undo(res["change_id"])
    assert brain.store.get_object(outdated[0]["payload"]["supersede"])["status"] == "active"


def test_unsupported_claim_is_flagged_not_trusted(brain):
    ing(brain, "a.md", "# T\n\nThe default timeout is 30 seconds for all requests handled by this service.")
    cid = brain.db.one("SELECT id FROM claims WHERE subj_key='defaulttimeout'")["id"]
    brain.db.exec("UPDATE claims SET evidence='The default timeout is 5 seconds for nothing in the source.' WHERE id=?", (cid,))
    r = brain.maintenance.run(["claims"])
    assert r["claims"]["flagged"] == 1
    assert brain.db.one("SELECT status FROM claims WHERE id=?", (cid,))["status"] == "unsupported"


def test_reclassification_finding_skips_user_corrected_objects(brain):
    o = brain.store.create_object(type="fact", title="Postmortem", origin="import", content="Postmortem of the outage. We learned that retries amplify load. Lessons learned: add jitter to every retry policy now.")
    r = brain.maintenance.run(["classification"])
    assert r["classification"]["new_findings"] == 1
    brain.evolution.record_feedback("classification", o["id"], "fact")          # user confirms: keep as fact
    brain.db.exec("DELETE FROM findings")
    assert brain.maintenance.run(["classification"])["classification"]["new_findings"] == 0


def test_isolated_findings(brain):
    brain.store.create_object(type="fact", title="Lonely", content="Nothing links to this object at all and it stands alone.")
    r = brain.maintenance.run(["isolated"])
    assert r["isolated"]["isolated"] == 1
    assert brain.maintenance.list(kind="isolated")


def test_findings_are_idempotent(brain):
    brain.store.create_object(type="fact", title="Lonely", content="Nothing links to this object at all and it stands alone.")
    brain.maintenance.run(["isolated"])
    n = len(brain.maintenance.list(None))
    brain.maintenance.run(["isolated"])
    assert len(brain.maintenance.list(None)) == n


# ---------------------------------------------------------------- evolution --
def test_concept_discovery_and_consolidation(brain):
    for i, (name, txt) in enumerate({
        "a.md": "# One\n\nThe backpressure mechanism protects the queue consumers when producers run faster than expected in production workloads.",
        "b.md": "# Two\n\nWe observed backpressure mechanism failures during the load test of the ingestion workers last quarter again.",
        "c.md": "# Three\n\nAdding backpressure mechanism metrics to the dashboard helps on-call engineers during every incident review meeting.",
    }.items()):
        ing(brain, name, txt)
    found = brain.evolution.discover_concepts()
    assert any("backpressure" in f["payload"]["term"] for f in found)
    f = [x for x in found if "backpressure" in x["payload"]["term"]][0]
    res = brain.maintenance.apply(f["id"])
    c = brain.store.get_object(res["finding"]["targets"][0]) if False else None
    concepts = [o for o in brain.store.list_objects(type="concept")]
    assert any("ackpressure" in o["title"] for o in concepts)
    brain.maintenance.changes.undo(res["change_id"])


def test_gaps_report_without_fabrication(brain):
    brain.store.create_object(type="question", title="What is the failover plan?", content="What is the failover plan for the primary node?")
    brain.store.create_object(type="hypothesis", title="Reranker helps", content="We suspect a reranker helps precision.")
    brain.store.create_object(type="decision", title="Use X", content="We decided to use X.")
    kinds = {g["kind"] for g in brain.evolution.gaps()}
    assert {"unanswered_question", "untested_hypothesis", "decision_without_rationale"} <= kinds
    assert brain.store.count_objects() == 3                 # reporting a gap never creates knowledge


def test_timeline_preserves_history(brain):
    ing(brain, "old.md", "---\ndate: 2025-01-01\n---\n# Cache old\n\nWe chose Redis for session caching in the gateway.")
    ing(brain, "new.md", "---\ndate: 2025-06-01\n---\n# Cache new\n\nWe replaced Redis with Memcached for session caching in the gateway.")
    old = [o for o in brain.store.list_objects() if o["title"] == "Cache old"][0]
    tl = brain.evolution.timeline(old["id"])
    assert [e["title"] for e in tl["events"]] == ["Cache old", "Cache new"]          # chronological, both preserved
    assert any(r["type"] == "replaces" for r in tl["relations"])


def test_feedback_is_bounded_and_separate_from_facts(brain):
    o = brain.store.create_object(type="fact", title="Alpha", content="alpha beta gamma delta", verification="unverified")
    brain.evolution.record_feedback("useful", o["id"], query="alpha")
    assert brain.retrieval._feedback_prior() == {}                         # one signal never moves ranking
    brain.evolution.record_feedback("useful", o["id"], query="alpha2")
    brain.evolution.record_feedback("useful", o["id"], query="alpha3")
    for _ in range(20):
        brain.evolution.record_feedback("useful", o["id"])
    assert brain.retrieval._feedback_prior()[o["id"]] == pytest.approx(0.08)  # capped at +8%
    assert brain.store.get_object(o["id"])["verification"] == "unverified"  # feedback is not verification
    brain.evolution.record_feedback("classification", o["id"], "concept")
    got = brain.store.get_object(o["id"])
    assert got["type"] == "concept" and got["verification"] == "user_verified"


def test_edge_feedback_persists_rejection(brain):
    a = brain.store.create_object(type="fact", title="A", content="aaa")["id"]
    b = brain.store.create_object(type="fact", title="B", content="bbb")["id"]
    e = brain.graph.add_edge(a, b, "related_to", origin="ai", confidence=0.7)
    brain.evolution.record_feedback("edge", e["id"], "reject")
    assert brain.graph.add_edge(a, b, "related_to", origin="ai", confidence=0.99) is None


def test_undo_user_edit(brain):
    o = brain.store.create_object(type="fact", title="T", content="original text")
    brain.store.update_object(o["id"], {"content": "edited text"})
    ch = brain.store.list_changes()[0]
    brain.maintenance.changes.undo(ch["id"])
    assert brain.store.get_object(o["id"])["content"] == "original text"
    assert brain.store.get_object(o["id"])["version"] == 3                       # undo is itself a new version; nothing erased
