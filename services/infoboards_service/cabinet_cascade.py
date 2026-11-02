"""Resolve complete cabinet navigation cascades before any ACL writes.

Unlike the questionnaire crawler, this reader never returns a truncated graph.
Links to another assigned root are navigation, not an inherited entitlement.
"""
from __future__ import annotations

import re
from collections import deque
from collections.abc import Iterable
from urllib.parse import parse_qs, urlsplit

import httpx

from common.exceptions import ParseError
from services.infoboards_service.cabinet_edit import _graphql


MAX_CASCADE_BOARDS = 1000
QUERY_CASCADE_BOARDS = """
query CabinetCascade {
  board {
    many {
      id
      widgets {
        __typename
        ... on LinksWidget {
          header
          titleLink { href about }
          links { href title about }
        }
      }
    }
  }
}
"""


def _norm(value: object) -> str:
    return " ".join(str(value or "").split()).casefold().replace("ё", "е").rstrip(" .!«»")


_BACK_LABELS = {
    "вернуться назад", "назад", "на главную", "вернуться на главную",
    "вернуться в главное меню", "вернуться в главный кабинет",
    "вернуться в кабинет", "в главное меню", "вернуться в итрхаб",
}
_BOARD_ID = re.compile(r"[A-Za-z0-9_-]+\Z")


def _linked_board(href: object, hostname: str) -> str | None:
    if href is None or href == "":
        return None
    if not isinstance(href, str):
        raise ParseError(code="CABINET_CASCADE_INVALID", message="Cabinet link href is not text")
    try:
        parsed = urlsplit(href.strip())
        if parsed.scheme.casefold() == "kodeks":
            if parsed.netloc.casefold() != "link" or parsed.path.rstrip("/") != "/d":
                return None
        elif parsed.scheme:
            if parsed.scheme.casefold() not in {"http", "https"} or parsed.hostname != hostname:
                return None
        elif parsed.netloc and parsed.hostname != hostname:
            return None
        values = parse_qs(parsed.query, keep_blank_values=True).get("infoboard")
    except ValueError as error:
        raise ParseError(code="CABINET_CASCADE_INVALID", message="Invalid cabinet navigation URL") from error
    if values is None:
        return None
    if len(values) != 1 or not _BOARD_ID.fullmatch(values[0]):
        raise ParseError(code="CABINET_CASCADE_INVALID", message="Ambiguous or invalid infoboard link ID")
    return values[0]


def _children(widgets: object, hostname: str) -> set[str]:
    if not isinstance(widgets, list):
        raise ParseError(code="CABINET_CASCADE_INVALID", message="Cabinet widgets are not a list")
    children: set[str] = set()
    for widget in widgets:
        if not isinstance(widget, dict):
            raise ParseError(code="CABINET_CASCADE_INVALID", message="Invalid cabinet widget")
        if widget.get("__typename") != "LinksWidget" or _norm(widget.get("header")) in _BACK_LABELS:
            continue
        title_link = widget.get("titleLink")
        links = widget.get("links")
        if title_link is not None and not isinstance(title_link, dict):
            raise ParseError(code="CABINET_CASCADE_INVALID", message="Invalid cabinet title link")
        if links is not None and not isinstance(links, list):
            raise ParseError(code="CABINET_CASCADE_INVALID", message="Invalid cabinet links list")
        for link in ([title_link] if title_link else []) + (links or []):
            if not isinstance(link, dict):
                raise ParseError(code="CABINET_CASCADE_INVALID", message="Invalid cabinet navigation link")
            if any(_norm(link.get(label)) in _BACK_LABELS for label in ("title", "about")):
                continue
            child = _linked_board(link.get("href"), hostname)
            if child is not None:
                children.add(child)
    return children


async def resolve_cabinet_cascades(
    base_url: str,
    client: httpx.AsyncClient,
    cookies: dict[str, str],
    roots: Iterable[str],
) -> dict[str, set[str]]:
    """Return root -> all reachable cabinet IDs, including the root itself.

    One query reads a coherent navigation snapshot for all boards. Only reachable
    widgets are validated and counted against the global traversal limit.
    Missing boards and malformed relevant configurations fail the entire read.
    """
    if isinstance(roots, (str, bytes)):
        raise ParseError(code="CABINET_CASCADE_INVALID", message="Cabinet roots must be a collection")
    root_ids: list[str] = []
    for root in roots:
        if not isinstance(root, str) or not _BOARD_ID.fullmatch(root):
            raise ParseError(code="CABINET_CASCADE_INVALID", message="Invalid cabinet root ID")
        if root not in root_ids:
            root_ids.append(root)
    try:
        hostname = urlsplit(base_url).hostname
    except ValueError as error:
        raise ParseError(code="CABINET_CASCADE_INVALID", message="Invalid catalog base URL") from error
    if not hostname:
        raise ParseError(code="CABINET_CASCADE_INVALID", message="Catalog base URL has no hostname")
    if not root_ids:
        return {}
    data = await _graphql(base_url, client, cookies, QUERY_CASCADE_BOARDS, {})
    board_container = data.get("board")
    if not isinstance(board_container, dict) or not isinstance(board_container.get("many"), list):
        raise ParseError(code="CABINET_CASCADE_INVALID", message="Invalid cabinet snapshot response")
    snapshot: dict[str, dict] = {}
    for board in board_container["many"]:
        if not isinstance(board, dict) or not isinstance(board.get("id"), str) or not _BOARD_ID.fullmatch(board["id"]):
            raise ParseError(code="CABINET_CASCADE_INVALID", message="Invalid cabinet ID in snapshot")
        board_id = board["id"]
        if board_id in snapshot:
            raise ParseError(code="CABINET_CASCADE_INVALID", message=f"Duplicate cabinet ID in snapshot: {board_id!r}")
        snapshot[board_id] = board
    root_set = set(root_ids)
    adjacency: dict[str, set[str]] = {}
    cascades: dict[str, set[str]] = {}
    for root in root_ids:
        visited: set[str] = set()
        pending = deque([root])
        while pending:
            board_id = pending.popleft()
            if board_id in visited:
                continue
            if board_id not in adjacency:
                if len(adjacency) >= MAX_CASCADE_BOARDS:
                    raise ParseError(code="CABINET_CASCADE_LIMIT", message=f"Cabinet cascade exceeds {MAX_CASCADE_BOARDS} boards")
                board = snapshot.get(board_id)
                if board is None:
                    raise ParseError(code="BOARD_NOT_FOUND", message=f"Cascade cabinet {board_id!r} was not found")
                adjacency[board_id] = _children(board.get("widgets"), hostname) - root_set
            visited.add(board_id)
            pending.extend(sorted(adjacency[board_id] - visited))
        cascades[root] = visited
    return cascades
