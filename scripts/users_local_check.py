from __future__ import annotations

import asyncio
import html
from collections import defaultdict
from typing import Any

from services.users_service.dto import CreateUserDTO, UpdateUserDTO
from services.users_service.service import UserService


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

        # эмулируем поведение каталога: id по умолчанию = uid
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


async def main() -> None:
    catalog = StubCatalogClient()
    service = UserService(
        auth_client=StubAuthClient(),  # type: ignore[arg-type]
        catalog_client=catalog,  # type: ignore[arg-type]
        reg_resolver=StubRegResolver(),  # type: ignore[arg-type]
    )

    await service.handle_create(
        CreateUserDTO.model_validate(
            {
                "reg": "350832",
                "uid": "Tetpo",
                "psw": "123",
            }
        )
    )

    # повторное создание: не должно давать повторный POST при неизменных данных
    await service.handle_create(
        CreateUserDTO.model_validate(
            {
                "reg": "350832",
                "uid": "Tetpo",
                "psw": "123",
            }
        )
    )

    # частичное обновление: меняется только mail, grp не трогаем
    await service.handle_update(
        UpdateUserDTO.model_validate(
            {
                "reg": "350832",
                "uid": "Tetpo",
                "psw": "123",
                "mail": "user@example.com",
            }
        )
    )

    # обновление с grp: группы перезаписываются
    await service.handle_update(
        UpdateUserDTO.model_validate(
            {
                "reg": "350832",
                "uid": "Tetpo",
                "psw": "123",
                "grp": [6],
            }
        )
    )

    print("Smoke check OK")
    print(f"Total POST calls: {catalog.post_calls}")
    print(f"POST calls for Tetpo: {catalog.post_calls_by_uid['Tetpo']}")
    print("Final user state:")
    print(catalog.users["Tetpo"])


if __name__ == "__main__":
    asyncio.run(main())
