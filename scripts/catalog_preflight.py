"""Read-only checks of the live catalog contract, with a value-free JSON report.

Run from the repository root: python -m scripts.catalog_preflight --reg REG
Only login and a GraphQL query use POST. This command never submits save/setup
forms, creates entities, changes ACL, or edits widgets. JS-only ACL forms remain
UNVERIFIED until their browser contract can be checked separately.
"""
from __future__ import annotations

import argparse
import asyncio
from collections import Counter
from datetime import datetime, timezone
from html.parser import HTMLParser
import json
import logging
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import httpx

from common.config import get_settings
from common.exceptions import ParseError
from common.http import _default_headers
from services.auth_service.service import AuthService
from services.infoboards_service.admin_dirs import parse_form_pairs
from services.infoboards_service.cabinet_acl import GRAPHQL_BOARDS
from services.users_service.catalog_client import CatalogClient, _check_response
from services.users_service.html_parser import (
    group_index, normalize_title, parse_group_creation_form,
    parse_groups_catalog, parse_user_state,
)
from services.users_service.reg_resolver import RegResolver


def form_schema(pairs: list[tuple[str, str]]) -> dict:
    """Return control names/counts only, never values, HTML, or cookies."""
    counts = Counter(name for name, _ in pairs)
    return {"control_count": len(pairs), "control_names": sorted(counts),
            "repeated_controls": sorted(name for name, count in counts.items() if count > 1)}


class _DocsLinks(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.links = []
        self._target = None
        self._text = []

    def handle_starttag(self, tag, attrs):
        if tag == "a":
            self._target = dict(attrs).get("href", "")
            self._text = []

    def handle_data(self, data):
        if self._target is not None:
            self._text.append(data)

    def handle_endtag(self, tag):
        if tag == "a" and self._target is not None:
            target = urlsplit(self._target)
            query = parse_qs(target.query)
            if target.path.rstrip("/").endswith("admin/dir") and "n" in query:
                if "".join(self._text).strip().casefold().rstrip("/") == "/docs":
                    self.links.extend(query["n"])
            self._target = None


def docs_directory_number(html: str) -> str:
    parser = _DocsLinks()
    parser.feed(html)
    numbers = set(parser.links)
    if len(numbers) != 1 or not next(iter(numbers)).isdigit():
        raise ParseError(code="DOCS_DIRECTORY_UNVERIFIED", message="A unique docs directory was not found")
    return numbers.pop()


def _failure(name, error):
    # Do not include str(error): errors can contain HTML, passwords or DB URLs.
    code = getattr(error, "code", "CHECK_FAILED")
    return {"name": name, "status": "UNVERIFIED", "error_code": str(code),
            "error_type": type(error).__name__}


async def check_catalog(base_url: str, http_client: httpx.AsyncClient,
                        cookies: dict[str, str], *, user: str | None = None) -> list[dict]:
    """Inspect mandatory read contracts independently; never save a form."""
    catalog = CatalogClient(http_client)
    checks = []
    groups_page = None
    try:
        groups_page = await catalog.get_groups_page(base_url, cookies)
        groups = group_index(parse_groups_catalog(groups_page))
        checks.append({"name": "groups", "status": "PASS", "group_count": len(groups)})
    except Exception as error:
        checks.append(_failure("groups", error))
    try:
        if groups_page is None:
            raise ParseError(code="GROUP_FORM_UNVERIFIED", message="Groups page was not read")
        creation_page = await catalog.get_group_create_page(base_url, cookies)
        pairs = parse_group_creation_form(creation_page)
        checks.append({"name": "group_creation_form", "status": "PASS", **form_schema(pairs)})
    except Exception as error:
        checks.append(_failure("group_creation_form", error))
    try:
        page = await catalog.get_user_create_page(base_url, cookies)
        state = parse_user_state(page)
        if not state.is_empty_form:
            raise ParseError(code="USER_CREATE_FORM_INVALID", message="Expected empty creation form")
        checks.append({"name": "user_creation_form", "status": "PASS", **form_schema(state.form_data)})
    except Exception as error:
        checks.append(_failure("user_creation_form", error))
    if user:
        try:
            page = await catalog.get_user_page(base_url, user, cookies)
            if page is None:
                raise ParseError(code="USER_NOT_FOUND", message="Requested test user is absent")
            state = parse_user_state(page)
            checks.append({"name": "existing_user_form", "status": "PASS", **form_schema(state.form_data)})
        except Exception as error:
            checks.append(_failure("existing_user_form", error))
    try:
        response = await http_client.post(
            f"{base_url.rstrip('/')}/infoboard/graphql?context=docs",
            json={"query": GRAPHQL_BOARDS, "variables": None}, cookies=cookies,
            headers={"Accept": "application/json"},
        )
        _check_response(response)
        body = response.json()
        if not isinstance(body, dict) or body.get("errors"):
            raise ParseError(code="BOARDS_RESPONSE_INVALID", message="GraphQL query failed")
        boards = body.get("data", {}).get("board", {}).get("many")
        if not isinstance(boards, list):
            raise ParseError(code="BOARDS_RESPONSE_INVALID", message="Board list was not found")
        names = {}
        for board in boards:
            if not isinstance(board, dict) or not board.get("id") or not board.get("title"):
                raise ParseError(code="BOARDS_RESPONSE_INVALID", message="Incomplete board entry")
            key = normalize_title(str(board["title"]))
            if key in names and names[key] != board["id"]:
                raise ParseError(code="AMBIGUOUS_BOARD", message="Several IDs match one board title")
            names[key] = board["id"]
        checks.append({"name": "boards", "status": "PASS", "board_count": len(names)})
    except Exception as error:
        checks.append(_failure("boards", error))
    try:
        response = await http_client.get(f"{base_url.rstrip('/')}/admin/dirs", cookies=cookies)
        _check_response(response)
        directory = docs_directory_number(response.text)
        response = await http_client.get(
            f"{base_url.rstrip('/')}/admin/dir", params={"n": directory}, cookies=cookies,
        )
        _check_response(response)
        # Deliberately do not POST setup2 or click anything: GET-only inspection.
        pairs = parse_form_pairs(response.text, acl=True)
        checks.append({"name": "acl_form_get", "status": "PASS", **form_schema(pairs)})
    except Exception as error:
        checks.append(_failure("acl_form_get", error))
    return checks


async def run_preflight(reg: str, *, user: str | None = None) -> dict:
    report = {"format_version": 1, "read_only": True,
              "checked_at": datetime.now(timezone.utc).isoformat(), "reg": reg,
              "checks": [], "ok": False}
    resolver = RegResolver()
    try:
        settings = get_settings()
        await resolver.startup()
        base_url = await resolver.resolve_base_url(reg)
        async with resolver.with_session() as session:
            cookies = await AuthService(db=session, settings=settings).login(reg=reg, name=None)
        async with httpx.AsyncClient(timeout=settings.HTTP_TIMEOUT, follow_redirects=True,
                                     headers=_default_headers()) as http_client:
            report["checks"] = await check_catalog(base_url, http_client, cookies, user=user)
    except Exception as error:
        report["checks"].append(_failure("connection_and_login", error))
    finally:
        try:
            await resolver.shutdown()
        except Exception as error:
            report["checks"].append(_failure("database_cleanup", error))
    report["ok"] = bool(report["checks"]) and all(c["status"] == "PASS" for c in report["checks"])
    return report


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Read-only catalog contract checks; no save forms are sent")
    parser.add_argument("--reg", required=True)
    parser.add_argument("--user", help="Optional existing test login/internal catalog ID to read")
    parser.add_argument("--output", type=Path, help="Write a value-free JSON report; stdout always contains the report")
    args = parser.parse_args(argv)
    # Library DEBUG logs can contain raw catalog values. CLI emits only the report.
    previous_disable = logging.root.manager.disable
    logging.disable(logging.CRITICAL)
    try:
        report = asyncio.run(run_preflight(args.reg, user=args.user))
    finally:
        logging.disable(previous_disable)
    encoded = json.dumps(report, ensure_ascii=False, indent=2)
    if args.output:
        args.output.write_text(encoded + "\n", encoding="utf-8")
    print(encoded)
    return 0 if report["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
