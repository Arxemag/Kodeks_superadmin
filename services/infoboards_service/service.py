"""
Сервис инфобордов: получение ссылки на кабинет по reg и опционально title.

Инициализация: InfoboardsService создаётся в эндпоинте get_infoboard_link (app.py) с db и settings.
Обработка: get_link_by_reg_and_title получает base_url из БД, логинится через AuthService (админ),
делает GraphQL-запрос к каталогу, парсит список кабинетов; при переданном title возвращает один кабинет,
иначе — список. HTTP-клиент создаётся и закрывается с проверкой на None в finally.
"""
from __future__ import annotations

from dataclasses import dataclass
from json import JSONDecodeError
from typing import Any

import httpx
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from common.config import Settings
from common.exceptions import AuthError, NetworkError, ParseError
from common.http import create_http_client
from common.logger import get_logger
from services.auth_service.service import AuthService


logger = get_logger("infoboards.service")

# Запрос списка кабинетов (инфобордов) к GraphQL API каталога
_GRAPHQL_QUERY = """
query{
  board{
    many{
      id
      title
      author
      hiddenOnStartPage
    }
  }
}
"""


@dataclass(frozen=True)
class InfoboardsService:
    """Сервис инфобордов: сессия БД для reg_services и настройки (для AuthService)."""
    db: AsyncSession
    settings: Settings

    async def get_link_by_reg_and_title(self, reg: str, title: str | None) -> dict[str, Any]:
        """По reg — base_url и куки админа; GraphQL запрос к каталогу; разбор many; при title — один элемент с совпадающим title, иначе {"items": [...]}."""
        base_url = await self._get_base_url(reg)
        api_url = f"{base_url.rstrip('/')}/infoboard/graphql?context=docs"
        payload = {"query": _GRAPHQL_QUERY, "variables": None}
        auth_service = AuthService(db=self.db, settings=self.settings)
        cookies = await auth_service.login(reg=reg, name=None)

        logger.debug(f"infoboards query start reg={reg!r} title={title!r} url={api_url!r}")
        client: httpx.AsyncClient | None = None
        try:
            client = await create_http_client()
            response = await client.post(
                api_url,
                json=payload,
                cookies=cookies,
                headers={"Accept": "application/json"},
            )
        except Exception as e:
            raise NetworkError(code="INFOBOARDS_REQUEST_FAILED", message="Infoboards request failed") from e
        finally:
            if client is not None:
                await client.aclose()

        if response.status_code >= 500:
            raise NetworkError(code="INFOBOARDS_5XX", message="Infoboards temporary failure")
        if response.status_code >= 400:
            raise NetworkError(
                code="INFOBOARDS_4XX",
                message=f"Infoboards rejected request status={response.status_code}",
                http_status=502,
            )

        try:
            body = response.json()
        except (JSONDecodeError, ValueError) as e:
            snippet = (response.text or "")[:200]
            raise ParseError(
                code="INFOBOARDS_NON_JSON",
                message=f"Infoboards response is not JSON (status={response.status_code}, body={snippet!r})",
            ) from e
        many = (
            body.get("data", {})
            .get("board", {})
            .get("many", [])
        )
        if not isinstance(many, list):
            raise ParseError(code="INFOBOARDS_PARSE_ERROR", message="Invalid infoboards payload structure")

        items: list[dict[str, str]] = []
        for item in many:
            if not isinstance(item, dict):
                continue
            board_id = str(item.get("id", "")).strip()
            board_title = str(item.get("title", "")).strip()
            if not board_id or not board_title:
                continue
            link = f"{base_url.rstrip('/')}/docs/?nd=606025000&infoboard={board_id}"
            items.append({"id": board_id, "title": board_title, "link": link})

        if not items:
            raise ParseError(code="INFOBOARDS_PARSE_ERROR", message="No valid infoboards found in payload")

        if title is None or not title.strip():
            return {"items": items}

        wanted = _normalize_title(title)
        for entry in items:
            if _normalize_title(entry["title"]) == wanted:
                return entry
        raise ParseError(code="INFOBOARD_NOT_FOUND", message=f"Infoboard with title {title!r} not found")

    async def _get_base_url(self, reg: str) -> str:
        """Чтение base_url из reg_services по reg_number; при отсутствии — AuthError REG_NOT_FOUND."""
        res = await self.db.execute(
            text("SELECT base_url FROM reg_services WHERE reg_number = :reg"),
            {"reg": reg},
        )
        base_url = res.scalar_one_or_none()
        if not base_url:
            raise AuthError(code="REG_NOT_FOUND", message="reg отсутствует в БД", http_status=404)
        return str(base_url)


def _normalize_title(value: str) -> str:
    """Нормализация заголовка для сравнения: пробелы схлопнуты, регистр нижний."""
    return " ".join(value.strip().casefold().split())
