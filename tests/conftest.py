"""
Shared pytest fixtures and stub implementations for tests.
"""
from __future__ import annotations

import asyncio
import html
from collections import defaultdict
from typing import Any

import pytest

from services.users_service.service import UserService


def pytest_configure(config: pytest.Config) -> None:
    config.addinivalue_line("markers", "asyncio: mark test as async (pytest-asyncio).")


@pytest.fixture(scope="session")
def event_loop_policy():
    return asyncio.DefaultEventLoopPolicy()


# ----- Stubs for UserService (no real Auth, DB, or Catalog) -----


class StubAuthClient:
    async def get_admin_cookies(self, reg: str) -> dict[str, str]:
        return {"Auth": f"stub-{reg}"}


class StubRegResolver:
    async def resolve_base_url(self, reg: str) -> str:
        return f"https://catalog-{reg}.local"


class StubCatalogClient:
    def __init__(self) -> None:
        self.users: dict[str, dict[str, Any]] = {}
        self.post_calls = 0
        self.post_calls_by_uid: dict[str, int] = defaultdict(int)

    async def get_user_page(self, base_url: str, uid: str, cookies: dict[str, str]) -> str | None:
        row = self.users.get(uid)
        if row is None:
            return None
        return self._render_user_form(row)

    async def post_user(self, base_url: str, form_data: list[tuple[str, str]], cookies: dict[str, str]) -> None:
        self.post_calls += 1
        parsed = self._parse_form_data(form_data)
        uid = parsed["uid"]
        self.post_calls_by_uid[uid] += 1
        self.users[uid] = parsed

    async def get_groups_page(self, base_url: str, cookies: dict[str, str]) -> str:
        groups_map = getattr(self, "_groups_map", {})
        if not groups_map:
            return getattr(self, "_groups_html", "<html><body></body></html>")
        parts = [f'<a href="grp?grp={gid}">{html.escape(title)}</a>' for title, gid in groups_map.items()]
        return "<html><body>" + "".join(parts) + "</body></html>"

    async def create_group(self, base_url: str, title: str, cookies: dict[str, str]) -> None:
        groups_map = getattr(self, "_groups_map", None)
        if groups_map is None:
            self._groups_map = {}
            groups_map = self._groups_map
        if title not in groups_map:
            groups_map[title] = max(groups_map.values(), default=0) + 1

    @staticmethod
    def _parse_form_data(form_data: list[tuple[str, str]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        groups: list[int] = []
        for key, value in form_data:
            if key == "grp":
                groups.append(int(value))
                continue
            result[key] = value
        if groups:
            result["grp"] = sorted(set(groups))
        else:
            result["grp"] = []
        if "id" not in result:
            result["id"] = result["uid"]
        return result

    @staticmethod
    def _render_user_form(user: dict[str, Any]) -> str:
        def inp(name: str, value: str) -> str:
            safe = html.escape(value, quote=True)
            return f'<input name="{name}" value="{safe}">'

        fields = [
            inp("uid", str(user.get("uid", ""))),
            inp("psw", str(user.get("psw", ""))),
            inp("name", str(user.get("name", ""))),
            inp("org", str(user.get("org", ""))),
            inp("pos", str(user.get("pos", ""))),
            inp("mail", str(user.get("mail", ""))),
            inp("telephon", str(user.get("telephon", ""))),
            inp("end", str(user.get("end", ""))),
            inp("id", str(user.get("id", user.get("uid", "")))),
        ]
        for grp in user.get("grp", []):
            fields.append(f'<input type="checkbox" name="grp" value="{grp}" checked>')
        return "<html><body>" + "".join(fields) + "</body></html>"


@pytest.fixture
def stub_auth_client() -> StubAuthClient:
    return StubAuthClient()


@pytest.fixture
def stub_reg_resolver() -> StubRegResolver:
    return StubRegResolver()


@pytest.fixture
def stub_catalog_client() -> StubCatalogClient:
    return StubCatalogClient()


@pytest.fixture
def user_service(stub_auth_client: StubAuthClient, stub_reg_resolver: StubRegResolver, stub_catalog_client: StubCatalogClient) -> UserService:
    return UserService(
        auth_client=stub_auth_client,  # type: ignore[arg-type]
        catalog_client=stub_catalog_client,  # type: ignore[arg-type]
        reg_resolver=stub_reg_resolver,
    )
