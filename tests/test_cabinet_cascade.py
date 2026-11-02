"""ACL cascades must include every child without following unrelated roots."""
from collections import Counter

import pytest

from common.exceptions import NetworkError, ParseError
from services.infoboards_service import cabinet_cascade as cascade


def link(board, *, title="child", scheme="kodeks://link/d"):
    return {"href": f"{scheme}?nd=1&infoboard={board}", "title": title}


def widget(*children, title_child=None, header="Children"):
    return {"__typename": "LinksWidget", "header": header,
            "titleLink": link(title_child) if title_child else None,
            "links": [link(child) for child in children]}


def patch_graph(monkeypatch, graph):
    reads = Counter()

    async def graphql(base, client, cookies, query, variables):
        reads["snapshot"] += 1
        assert variables == {}
        assert "many" in query and "one(" not in query
        assert "... on LinksWidget" in query
        return {"board": {"many": [{"id": board, "widgets": widgets} for board, widgets in graph.items()]}}

    monkeypatch.setattr(cascade, "_graphql", graphql)
    return reads


@pytest.mark.asyncio
async def test_every_widget_link_multilevel_cycles_shared_cache_and_root_boundaries(monkeypatch):
    reads = patch_graph(monkeypatch, {
        "R1": [widget("B", "R2", title_child="A")],
        "R2": [widget("B")], "A": [widget("C")],
        "B": [widget("D")], "C": [widget("R1", "A", "D")], "D": [],
    })
    actual = await cascade.resolve_cabinet_cascades("https://catalog", None, {}, ["R1", "R2", "R1"])
    assert actual == {"R1": {"R1", "A", "B", "C", "D"}, "R2": {"R2", "B", "D"}}
    assert reads == {"snapshot": 1}


@pytest.mark.asyncio
async def test_back_navigation_at_widget_title_and_individual_link_is_ignored(monkeypatch):
    reads = patch_graph(monkeypatch, {"R": [
        widget("BACK1", header="  Вернуться\u00a0назад "),
        {"__typename": "LinksWidget", "header": "Navigation", "titleLink":
         {"href": "?infoboard=BACK2", "about": "Назад"},
         "links": [link("BACK3", title="Вернуться на главную"), link("OK")]},
    ], "OK": []})
    assert await cascade.resolve_cabinet_cascades("https://catalog", None, {}, ["R"]) == {"R": {"R", "OK"}}
    assert reads == {"snapshot": 1}


@pytest.mark.asyncio
async def test_native_relative_local_links_and_external_navigation(monkeypatch):
    candidates = [
        link("A"), link("B", scheme="/docs/"), link("C", scheme="https://catalog/docs/"),
        link("D", scheme="http://catalog:123/docs/"), link("E", scheme="//catalog/docs/"),
        link("NO1", scheme="https://other/docs/"), link("NO2", scheme="//other/docs/"),
        link("NO3", scheme="javascript:ignored"), link("NO4", scheme="kodeks://other/d"),
        {"href": "/docs/?nd=123", "title": "document"},
    ]
    reads = patch_graph(monkeypatch, {"R": [{"__typename": "LinksWidget", "links": candidates}],
                                        **{board: [] for board in "ABCDE"}})
    actual = await cascade.resolve_cabinet_cascades("https://catalog", None, {}, ["R"])
    assert actual["R"] == {"R", *"ABCDE"}
    assert reads == {"snapshot": 1}


@pytest.mark.asyncio
async def test_missing_child_fails_entire_read(monkeypatch):
    patch_graph(monkeypatch, {"R": [widget("MISSING")]})
    with pytest.raises(ParseError, match="MISSING") as caught:
        await cascade.resolve_cabinet_cascades("https://catalog", None, {}, ["R"])
    assert caught.value.code == "BOARD_NOT_FOUND"


@pytest.mark.asyncio
async def test_global_board_limit_fails_instead_of_returning_partial_graph(monkeypatch):
    reads = patch_graph(monkeypatch, {"A": [widget("B")], "B": [widget("C")], "C": []})
    monkeypatch.setattr(cascade, "MAX_CASCADE_BOARDS", 2)
    with pytest.raises(ParseError) as caught:
        await cascade.resolve_cabinet_cascades("https://catalog", None, {}, ["A"])
    assert caught.value.code == "CABINET_CASCADE_LIMIT"
    assert reads == {"snapshot": 1}


@pytest.mark.asyncio
async def test_long_chain_has_no_silent_depth_cutoff(monkeypatch):
    graph = {f"P{i}": [widget(f"P{i + 1}")] for i in range(12)}
    graph["P12"] = []
    patch_graph(monkeypatch, graph)
    actual = await cascade.resolve_cabinet_cascades("https://catalog", None, {}, ["P0"])
    assert actual == {"P0": set(graph)}


@pytest.mark.asyncio
async def test_size_limit_counts_distinct_boards_across_roots(monkeypatch):
    reads = patch_graph(monkeypatch, {"A": [widget("C")], "B": [], "C": []})
    monkeypatch.setattr(cascade, "MAX_CASCADE_BOARDS", 2)
    with pytest.raises(ParseError) as caught:
        await cascade.resolve_cabinet_cascades("https://catalog", None, {}, ["A", "B"])
    assert caught.value.code == "CABINET_CASCADE_LIMIT"
    assert reads == {"snapshot": 1}


@pytest.mark.asyncio
@pytest.mark.parametrize("board", [
    {"id": "R"},
    {"id": "R", "widgets": [None]},
    {"id": "R", "widgets": [{"__typename": "LinksWidget", "links": "invalid"}]},
    {"id": "R", "widgets": [{"__typename": "LinksWidget", "links": [{"href": "?infoboard=A&infoboard=B"}]}]},
])
async def test_malformed_response_and_ambiguous_id_fail(monkeypatch, board):
    async def graphql(*args):
        return {"board": {"many": [board]}}
    monkeypatch.setattr(cascade, "_graphql", graphql)
    with pytest.raises(ParseError) as caught:
        await cascade.resolve_cabinet_cascades("https://catalog", None, {}, ["R"])
    assert caught.value.code == "CABINET_CASCADE_INVALID"


@pytest.mark.asyncio
async def test_duplicate_snapshot_ids_fail_even_when_not_reachable(monkeypatch):
    async def graphql(*args):
        return {"board": {"many": [{"id": "R", "widgets": []}, {"id": "OTHER"}, {"id": "OTHER"}]}}
    monkeypatch.setattr(cascade, "_graphql", graphql)
    with pytest.raises(ParseError, match="Duplicate cabinet ID"):
        await cascade.resolve_cabinet_cascades("https://catalog", None, {}, ["R"])


@pytest.mark.asyncio
async def test_irrelevant_widgets_and_snapshot_size_do_not_restrict_valid_cascade(monkeypatch):
    patch_graph(monkeypatch, {"R": [], "UNREACHABLE": None, "UNREACHABLE2": "bad widgets"})
    monkeypatch.setattr(cascade, "MAX_CASCADE_BOARDS", 1)
    assert await cascade.resolve_cabinet_cascades("https://catalog", None, {}, ["R"]) == {"R": {"R"}}


@pytest.mark.asyncio
@pytest.mark.parametrize("data", [{}, {"board": None}, {"board": {"many": None}}, {"board": {"many": {}}}])
async def test_invalid_snapshot_response_fails(monkeypatch, data):
    async def graphql(*args):
        return data
    monkeypatch.setattr(cascade, "_graphql", graphql)
    with pytest.raises(ParseError) as caught:
        await cascade.resolve_cabinet_cascades("https://catalog", None, {}, ["R"])
    assert caught.value.code == "CABINET_CASCADE_INVALID"


@pytest.mark.asyncio
async def test_network_failure_propagates_for_retry(monkeypatch):
    async def graphql(*args):
        raise NetworkError(code="GRAPHQL_REQUEST_FAILED", message="timeout")
    monkeypatch.setattr(cascade, "_graphql", graphql)
    with pytest.raises(NetworkError):
        await cascade.resolve_cabinet_cascades("https://catalog", None, {}, ["R"])


@pytest.mark.asyncio
async def test_empty_roots_are_noop(monkeypatch):
    reads = patch_graph(monkeypatch, {})
    assert await cascade.resolve_cabinet_cascades("https://catalog", None, {}, []) == {}
    assert not reads
