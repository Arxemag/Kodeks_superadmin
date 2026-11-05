"""Resolve which pages can inherit editing rights from a main cabinet."""
from __future__ import annotations

from collections import Counter, deque
from dataclasses import dataclass
from urllib.parse import urlsplit

import httpx

from common.exceptions import ParseError
from services.infoboards_service.cabinet_cascade import (
    MAX_CASCADE_BOARDS, QUERY_CASCADE_BOARDS, _BACK_LABELS, _BOARD_ID, _children, _linked_board, _norm,
)
from services.infoboards_service.cabinet_edit import _graphql


_EDITOR_HEADER = "Мой кабинет"
_SYSTEM_HEADERS = {"справочная информация", "спарвочная информация", "инфостенд", "информационный стенд"}


@dataclass(frozen=True)
class EditorScope:
    branch_id: str
    editable_ids: frozenset[str]


def _invalid(message: str) -> ParseError:
    return ParseError(code="EDITOR_SCOPE_INVALID", message=message)


def _editor_widgets(widgets: object) -> list[dict]:
    if not isinstance(widgets, list):
        return []
    return [widget for widget in widgets if isinstance(widget, dict)
            and widget.get("__typename") == "LinksWidget" and widget.get("header") == _EDITOR_HEADER]


def _editor_branch(widgets: list[dict], root_id: str, hostname: str) -> str:
    if len(widgets) != 1:
        raise _invalid(f"Cabinet {root_id!r} must have exactly one {_EDITOR_HEADER!r} widget")
    link = widgets[0].get("titleLink")
    if not isinstance(link, dict):
        raise _invalid(f"Cabinet {root_id!r} has no title link for {_EDITOR_HEADER!r}")
    if _norm(link.get("about")) in _BACK_LABELS | _SYSTEM_HEADERS:
        raise _invalid(f"Cabinet {root_id!r} has a navigation link for {_EDITOR_HEADER!r}")
    branch_id = _linked_board(link.get("href"), hostname)
    if branch_id is None:
        raise _invalid(f"Cabinet {root_id!r} has no local target for {_EDITOR_HEADER!r}")
    return branch_id


def _without_system_links(widgets: list) -> list:
    """Let the shared cascade parser validate the shape of remaining links."""
    result = []
    for widget in widgets:
        if not isinstance(widget, dict) or widget.get("__typename") != "LinksWidget":
            result.append(widget)
            continue
        if _norm(widget.get("header")) in _SYSTEM_HEADERS:
            continue
        cleaned = dict(widget)
        title_link = cleaned.get("titleLink")
        if isinstance(title_link, dict) and _norm(title_link.get("about")) in _SYSTEM_HEADERS:
            cleaned["titleLink"] = None
        links = cleaned.get("links")
        if isinstance(links, list):
            cleaned["links"] = [link for link in links if not isinstance(link, dict)
                                or not any(_norm(link.get(label)) in _SYSTEM_HEADERS for label in ("title", "about"))]
        result.append(cleaned)
    return result


async def resolve_editor_scopes(
    base_url: str,
    http: httpx.AsyncClient,
    cookies: dict[str, str],
    roots: set[str],
    catalog_roots: set[str] | None = None,
) -> dict[str, EditorScope]:
    """Plan editing on exclusive branches entered through the main cabinet's title link."""
    if not roots:
        return {}
    known_roots = roots | (catalog_roots or set())
    if any(not isinstance(root, str) or not _BOARD_ID.fullmatch(root) for root in known_roots):
        raise _invalid("Invalid main cabinet ID")
    try:
        hostname = urlsplit(base_url).hostname
    except ValueError as error:
        raise _invalid("Invalid catalog base URL") from error
    if not hostname:
        raise _invalid("Catalog base URL has no hostname")

    data = await _graphql(base_url, http, cookies, QUERY_CASCADE_BOARDS, {})
    board = data.get("board") if isinstance(data, dict) else None
    if not isinstance(board, dict) or not isinstance(board.get("many"), list):
        raise _invalid("Invalid cabinet snapshot")
    snapshot = {}
    ids = set()
    for item in board["many"]:
        board_id = item.get("id") if isinstance(item, dict) else None
        if not isinstance(board_id, str) or not _BOARD_ID.fullmatch(board_id) or board_id.casefold() in ids:
            raise _invalid("Invalid or duplicate cabinet ID in snapshot")
        ids.add(board_id.casefold())
        snapshot[board_id] = item.get("widgets")

    def read_widgets(board_id: str) -> list:
        if board_id not in snapshot:
            raise _invalid(f"Cabinet {board_id!r} is missing from the snapshot")
        widgets = snapshot[board_id]
        if not isinstance(widgets, list):
            raise _invalid(f"Cabinet {board_id!r} has invalid widgets")
        return widgets

    # A peer's exact native marker protects its main and editor branch even if
    # this company has no assignment for that cabinet.
    markers = {board_id: widgets for board_id, raw in snapshot.items()
               if (widgets := _editor_widgets(raw))}
    known_roots.update(markers)
    branches = {}
    for root in sorted(known_roots):
        read_widgets(root)
        if root in roots or root in markers:
            branches[root] = _editor_branch(markers.get(root, []), root, hostname)
    if set(branches.values()) & known_roots:
        raise _invalid("An editor branch points to a main cabinet")

    adjacency: dict[str, tuple[set[str], set[str]]] = {}

    def closure(seeds: set[str], forbidden: set[str], *, protect_system: bool = False) -> set[str]:
        visited = set()
        pending = deque(sorted(seeds - forbidden))
        while pending:
            board_id = pending.popleft()
            if board_id in visited:
                continue
            if board_id not in adjacency:
                if len(adjacency) >= MAX_CASCADE_BOARDS:
                    raise _invalid(f"Editor graph exceeds {MAX_CASCADE_BOARDS} reachable cabinets")
                widgets = read_widgets(board_id)
                adjacency[board_id] = (_children(widgets, hostname),
                                       _children(_without_system_links(widgets), hostname))
            visited.add(board_id)
            local, editable = adjacency[board_id]
            pending.extend(sorted((local if protect_system else editable) - forbidden - visited))
        return visited

    sibling_entries = set()
    for root in known_roots:
        sibling_entries.update(_children(read_widgets(root), hostname) - {branches.get(root)})
    # A sibling's reference pages are also protected if an editor branch
    # happens to link to the same IDs through ordinary navigation.
    protected = closure(sibling_entries, known_roots, protect_system=True)

    entry_ids = set(branches.values())
    candidates = {root: closure({branch}, known_roots | protected | (entry_ids - {branch}))
                  for root, branch in branches.items()}
    uses = Counter(board_id for pages in candidates.values() for board_id in pages)
    scopes = {}
    for root in roots:
        branch = branches[root]
        editable = frozenset(board_id for board_id in candidates[root] if uses[board_id] == 1)
        if branch not in editable:
            raise _invalid(f"The editor branch for cabinet {root!r} is shared or protected")
        scopes[root] = EditorScope(branch, editable)
    return scopes
