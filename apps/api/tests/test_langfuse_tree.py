"""Unit tests for the observation-tree reconstruction (no I/O)."""

from agentos_api.langfuse import build_tree


def _observations() -> list[dict[str, object]]:
    # A 3-level tree: agent.run -> llm.generation -> {search_repo, write_file}.
    return [
        {
            "id": "root",
            "type": "SPAN",
            "name": "agent.run",
            "startTime": "2026-07-05T00:00:00Z",
            "parentObservationId": None,
        },
        {
            "id": "gen",
            "type": "GENERATION",
            "name": "llm.generation",
            "startTime": "2026-07-05T00:00:01Z",
            "model": "claude-opus-4-8",
            "usageDetails": {"input": 1200, "output": 88},
            "parentObservationId": "root",
        },
        {
            "id": "tool-b",
            "type": "SPAN",
            "name": "write_file",
            "startTime": "2026-07-05T00:00:03Z",
            "parentObservationId": "gen",
        },
        {
            "id": "tool-a",
            "type": "SPAN",
            "name": "search_repo",
            "startTime": "2026-07-05T00:00:02Z",
            "parentObservationId": "gen",
        },
    ]


def test_build_tree_reconstructs_three_levels() -> None:
    tree = build_tree(_observations())

    assert len(tree) == 1
    root = tree[0]
    assert root.name == "agent.run"

    assert len(root.children) == 1
    gen = root.children[0]
    assert gen.type == "GENERATION"
    assert gen.model == "claude-opus-4-8"
    assert gen.usageDetails == {"input": 1200, "output": 88}

    # Children are ordered by startTime, so search_repo (t2) precedes write_file (t3).
    assert [c.name for c in gen.children] == ["search_repo", "write_file"]


def test_orphaned_parent_is_promoted_to_root() -> None:
    # An observation whose parent is not in the set is treated as a root, so no
    # data is dropped when a partial page is returned.
    observations = [
        {"id": "a", "type": "SPAN", "name": "a", "parentObservationId": "missing"},
    ]
    tree = build_tree(observations)
    assert [n.id for n in tree] == ["a"]
