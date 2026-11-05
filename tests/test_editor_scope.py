"""Editing rights stay within the exclusive native My Cabinet branch."""
from unittest.mock import AsyncMock

import pytest

from common.exceptions import ParseError
from services.infoboards_service import editor_scope


BASE = "https://catalog.test"


def _link(board_id, *, title="Раздел", href=None):
    return {"href": href or f"kodeks://link/d?nd=1&infoboard={board_id}", "title": title}


def _widget(header, target=None, *links):
    return {"__typename": "LinksWidget", "header": header,
            "titleLink": _link(target) if target else None, "links": list(links)}


def _snapshot(monkeypatch, graph):
    read = AsyncMock(return_value={"board": {"many": [{"id": board_id, "widgets": widgets}
                                                       for board_id, widgets in graph.items()]}})
    monkeypatch.setattr(editor_scope, "_graphql", read)
    return read


@pytest.mark.asyncio
async def test_excludes_main_peer_editor_sibling_and_shared_descendants(monkeypatch):
    read = _snapshot(monkeypatch, {
        "R1": [_widget("Мой кабинет", "E1"), _widget("Инфостенд", "S1")],
        "R2": [_widget("Мой кабинет", "E2"), _widget("Общие разделы", "S2")],
        "R3": [_widget("Другой главный кабинет", "S3")],
        "E1": [_widget("Рабочие разделы", "A", _link("B"), _link("R1"), _link("R2"), _link("E2"),
                       _link("S1"), _link("S3"), _link("T"), _link("SHARED"))],
        "E2": [_widget("Рабочий раздел другого корня", "PEER", _link("SHARED"), _link("E1"))],
        "S1": [_widget("Справочная информация", "T")], "S2": [], "S3": [],
        "T": [_widget("Глубокий потомок инфостенда", "U")], "U": [],
        "A": [], "B": [], "SHARED": [_widget("Потомок общего раздела", "BOTH")], "BOTH": [], "PEER": [],
    })

    scopes = await editor_scope.resolve_editor_scopes(BASE, None, {}, {"R1", "R2"}, {"R3"})

    assert scopes == {
        "R1": editor_scope.EditorScope("E1", frozenset({"E1", "A", "B"})),
        "R2": editor_scope.EditorScope("E2", frozenset({"E2", "PEER"})),
    }
    read.assert_awaited_once_with(BASE, None, {}, editor_scope.QUERY_CASCADE_BOARDS, {})


@pytest.mark.asyncio
async def test_infers_unassigned_main_from_native_marker(monkeypatch):
    _snapshot(monkeypatch, {
        "R": [_widget("Мой кабинет", "E")],
        "E": [_widget("Переходы", "UNASSIGNED_MAIN", _link("UNASSIGNED_EDIT"))],
        "UNASSIGNED_MAIN": [_widget("Мой кабинет", "UNASSIGNED_EDIT")],
        "UNASSIGNED_EDIT": [_widget("Не назначено", "UNASSIGNED_CHILD")],
        "UNASSIGNED_CHILD": [],
    })
    scopes = await editor_scope.resolve_editor_scopes(BASE, None, {}, {"R"})
    assert scopes == {"R": editor_scope.EditorScope("E", frozenset({"E"}))}


@pytest.mark.asyncio
async def test_deep_cycle_and_native_back_system_external_links(monkeypatch):
    graph = {"R": [_widget("Мой кабинет", "P0")], "P12": [_widget("Цикл", "P1")]}
    graph.update({f"P{i}": [_widget("Раздел", f"P{i + 1}")] for i in range(12)})
    graph["P0"].extend([
        _widget("Справочная информация", "SYSTEM"), _widget("Инфостенд", "STAND"),
        _widget("Вернуться назад", "BACK"),
        _widget("Другие переходы", None, _link("SYSTEM2", title="Справочная информация"),
                _link("EXTERNAL", href="https://other.test/docs/?infoboard=EXTERNAL"),
                _link("LOCAL", href="/docs/?infoboard=LOCAL")),
    ])
    graph["LOCAL"] = []
    _snapshot(monkeypatch, graph)

    scopes = await editor_scope.resolve_editor_scopes(BASE, None, {}, {"R"})

    assert scopes["R"].editable_ids == frozenset({"LOCAL", *(f"P{i}" for i in range(13))})


@pytest.mark.asyncio
@pytest.mark.parametrize('root_widgets,extra,expected_message', [
    ([], {}, "exactly one"),
    ([_widget("Мой кабинет и справка", "E")], {"E": []}, "exactly one"),
    ([_widget("Мой кабинет", "E"), _widget("Мой кабинет", "E")], {"E": []}, "exactly one"),
    ([_widget("Мой кабинет", None, _link("E"))], {"E": []}, "no title link"),
    ([{**_widget("Мой кабинет", "E"), "titleLink": _link("E", href="https://foreign.test/?infoboard=E")}],
     {"E": []}, "no local target"),
    ([_widget("Мой кабинет", "E")], {}, "missing from the snapshot"),
    ([_widget("Мой кабинет", "R")], {}, "points to a main cabinet"),
    ([_widget("Мой кабинет", "E")], {"E": [_widget("Вложенный", "MISSING")]}, "missing from the snapshot"),
    ([_widget("Мой кабинет", "E"), _widget("Инфостенд", "S")], {"E": [], "S": [_widget("Общая ветка", "E")]},
     "shared or protected"),
])
async def test_unresolvable_or_protected_branch_fails_closed(monkeypatch, root_widgets, extra, expected_message):
    _snapshot(monkeypatch, {"R": root_widgets, **extra})

    with pytest.raises(ParseError, match=expected_message):
        await editor_scope.resolve_editor_scopes(BASE, None, {}, {"R"})


@pytest.mark.asyncio
async def test_empty_requests_do_not_read_catalog(monkeypatch):
    read = _snapshot(monkeypatch, {})
    assert await editor_scope.resolve_editor_scopes(BASE, None, {}, set(), {"R"}) == {}
    read.assert_not_awaited()
