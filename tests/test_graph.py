import pytest
from brain.knowledge import Invalid, NotFound


def mk(b, title, type="fact", **kw):
    return b.store.create_object(type=type, title=title, content=title + " body", **kw)["id"]


def test_edge_persistence_provenance_and_validation(brain):
    a, c = mk(brain, "A"), mk(brain, "C")
    e = brain.graph.add_edge(a, c, "depends_on", origin="ai", confidence=0.7, explanation="A needs C", evidence={"quote": "needs C"})
    assert e["verification"] == "suggested" and e["origin"] == "ai" and e["explanation"] == "A needs C"
    assert e["evidence"] == {"quote": "needs C"} and e["created_at"] and e["updated_at"]
    with pytest.raises(Invalid):
        brain.graph.add_edge(a, c, "friends_with")
    with pytest.raises(Invalid):
        brain.graph.add_edge(a, a, "related_to")
    with pytest.raises(NotFound):
        brain.graph.add_edge(a, "k_nope", "related_to")
    # types are not interchangeable
    brain.graph.add_edge(a, c, "similar_to", origin="ai", confidence=0.6)
    assert len(brain.graph.edges_of(a)) == 2


def test_symmetric_edges_are_canonical_and_directed_have_inverse_labels(brain):
    a, b = mk(brain, "A"), mk(brain, "B")
    e1 = brain.graph.add_edge(a, b, "similar_to", confidence=0.6)
    e2 = brain.graph.add_edge(b, a, "similar_to", confidence=0.6)
    assert e1["id"] == e2["id"]
    brain.graph.add_edge(a, b, "depends_on")
    labels = {e["label"] for e in brain.graph.edges_of(b)}
    assert "required_by" in labels and "similar_to" in labels


def test_user_rejection_is_a_tombstone_ai_cannot_resurrect(brain):
    a, b = mk(brain, "A"), mk(brain, "B")
    e = brain.graph.add_edge(a, b, "related_to", confidence=0.8)
    brain.graph.reject_edge(e["id"])
    assert brain.graph.edges_of(a) == []
    assert brain.graph.add_edge(a, b, "related_to", origin="ai", confidence=0.99) is None
    assert brain.graph.is_rejected(a, b)
    # explicit user action may re-create it
    again = brain.graph.add_edge(a, b, "related_to", origin="user")
    assert again["verification"] == "verified"
    hist = [h["action"] for h in brain.graph.edge_history(e["id"])]
    assert hist[0] == "create" and "reactivate" in hist


def test_rediscovery_merges_instead_of_duplicating(brain):
    a, b = mk(brain, "A"), mk(brain, "B")
    brain.graph.add_edge(a, b, "supports", confidence=0.5, evidence={"x": 1})
    e = brain.graph.add_edge(a, b, "supports", confidence=0.8, evidence={"y": 2})
    assert e["confidence"] == 0.8 and e["evidence"] == {"x": 1, "y": 2}
    assert brain.db.one("SELECT COUNT(*) c FROM edges")["c"] == 1


def build_chain(b, n):
    ids = [mk(b, f"N{i}") for i in range(n)]
    for i in range(n - 1):
        b.graph.add_edge(ids[i], ids[i + 1], "depends_on", confidence=0.9)
    return ids


def test_bounded_traversal(brain):
    ids = build_chain(brain, 8)
    r = brain.graph.neighborhood(ids[0], hops=2)
    assert {n["id"] for n in r["nodes"]} == set(ids[:3])
    assert {n["id"]: n["hops"] for n in r["nodes"]}[ids[2]] == 2
    r = brain.graph.neighborhood(ids[0], hops=10, limit=4)
    assert len(r["nodes"]) == 4 and r["truncated"]
    # strongest paths are explored first
    hub = mk(brain, "hub")
    weak, strong = mk(brain, "weak"), mk(brain, "strong")
    brain.graph.add_edge(hub, weak, "related_to", confidence=0.3)
    brain.graph.add_edge(hub, strong, "depends_on", confidence=1.0)
    r = brain.graph.neighborhood(hub, hops=1, limit=2)
    assert {n["id"] for n in r["nodes"]} == {hub, strong}


def test_type_filter_and_path(brain):
    ids = build_chain(brain, 5)
    brain.graph.add_edge(ids[0], ids[4], "related_to", confidence=0.4)
    r = brain.graph.neighborhood(ids[0], hops=1, types=["depends_on"])
    assert {n["id"] for n in r["nodes"]} == {ids[0], ids[1]}
    p = brain.graph.path(ids[0], ids[3])
    assert [s["to"] for s in p] == [ids[1], ids[2], ids[3]] or len(p) == 2   # via shortcut ids[4]
    assert brain.graph.path(ids[0], mk(brain, "island")) is None


def test_components_isolated_clusters_centrality(brain):
    a = build_chain(brain, 4)
    b = build_chain(brain, 3)
    lone = mk(brain, "lone")
    comps = brain.graph.components()
    assert sorted(len(c) for c in comps) == [1, 3, 4]
    assert brain.graph.isolated() == [lone]
    cl = brain.graph.clusters()
    assert cl[lone] not in {cl[x] for x in a + b}                 # isolated node never shares a cluster
    assert not ({cl[x] for x in a} & {cl[x] for x in b})            # disconnected components never merge
    pr = brain.graph.centrality()
    assert pr[a[1]] > pr[a[0]]                          # interior nodes outrank chain ends
    st = brain.graph.stats()
    assert st["nodes"] == 8 and st["edges"] == 5 and st["components"] == 3 and st["isolated"] == 1


def test_clusters_find_dense_groups(brain):
    import itertools
    g1 = [mk(brain, f"A{i}") for i in range(5)]
    g2 = [mk(brain, f"B{i}") for i in range(5)]
    for grp in (g1, g2):
        for x, y in itertools.combinations(grp, 2):
            brain.graph.add_edge(x, y, "related_to", confidence=0.9)
    brain.graph.add_edge(g1[0], g2[0], "related_to", confidence=0.2)      # weak bridge
    cl = brain.graph.clusters()
    assert len({cl[x] for x in g1}) == 1 and len({cl[x] for x in g2}) == 1 and cl[g1[0]] != cl[g2[0]]


def test_deleted_nodes_leave_graph(brain):
    ids = build_chain(brain, 3)
    brain.store.set_status(ids[1], "deleted")
    assert len(brain.graph.components()) == 2
    assert brain.graph.edges_of(ids[0]) == []


def test_overview_is_bounded(brain):
    ids = build_chain(brain, 30)
    ov = brain.graph.overview(max_nodes=10)
    assert ov["shown"] == 10 and ov["total_nodes"] == 30 and len(ov["nodes"]) == 10
    assert all(e["src"] in {n["id"] for n in ov["nodes"]} for e in ov["edges"])
