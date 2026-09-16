"""Tests for services.infoboards_service (normalize_title and service with mocks)."""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from services.infoboards_service.service import InfoboardsService, _normalize_title


def test_normalize_title() -> None:
    assert _normalize_title("  Кабинет  СМК  ") == "кабинет смк"
    assert _normalize_title("One") == "one"
    assert _normalize_title("") == ""


@pytest.mark.asyncio
async def test_get_link_by_reg_and_title_returns_items_when_no_title() -> None:
    mock_session = MagicMock(spec=AsyncSession)
    mock_result = MagicMock()
    mock_result.scalar_one_or_none.return_value = "https://catalog.example/"
    mock_session.execute = AsyncMock(return_value=mock_result)

    with patch("services.infoboards_service.service.create_http_client") as create_client:
        mock_client = MagicMock()
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {
            "data": {
                "board": {
                    "many": [
                        {"id": "P001", "title": "Cabinet A", "author": "", "hiddenOnStartPage": False},
                    ]
                }
            }
        }
        mock_client.post = AsyncMock(return_value=mock_resp)
        mock_client.aclose = AsyncMock()
        create_client.return_value = mock_client

        with patch("services.infoboards_service.service.AuthService") as AuthServiceMock:
            auth_instance = MagicMock()
            auth_instance.login = AsyncMock(return_value={"session": "x"})
            AuthServiceMock.return_value = auth_instance

            service = InfoboardsService(db=mock_session, settings=MagicMock())
            data = await service.get_link_by_reg_and_title(reg="350832", title=None)

    assert "items" in data
    assert len(data["items"]) == 1
    assert data["items"][0]["id"] == "P001"
    assert data["items"][0]["title"] == "Cabinet A"
    assert "link" in data["items"][0]


@pytest.mark.asyncio
async def test_get_link_by_reg_and_title_returns_one_by_title() -> None:
    mock_session = MagicMock(spec=AsyncSession)
    mock_result = MagicMock()
    mock_result.scalar_one_or_none.return_value = "https://catalog.example/"
    mock_session.execute = AsyncMock(return_value=mock_result)

    with patch("services.infoboards_service.service.create_http_client") as create_client:
        mock_client = MagicMock()
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {
            "data": {
                "board": {
                    "many": [
                        {"id": "P001", "title": "Кабинет СМК", "author": "", "hiddenOnStartPage": False},
                    ]
                }
            }
        }
        mock_client.post = AsyncMock(return_value=mock_resp)
        mock_client.aclose = AsyncMock()
        create_client.return_value = mock_client

        with patch("services.infoboards_service.service.AuthService") as AuthServiceMock:
            auth_instance = MagicMock()
            auth_instance.login = AsyncMock(return_value={"session": "x"})
            AuthServiceMock.return_value = auth_instance

            service = InfoboardsService(db=mock_session, settings=MagicMock())
            data = await service.get_link_by_reg_and_title(reg="350832", title="Кабинет СМК")

    assert "items" not in data
    assert data["id"] == "P001"
    assert data["title"] == "Кабинет СМК"
    assert "link" in data


# --- Стартовая страница ТЭ: в списке только видимые кабинеты ---------------------------


def _service_with_boards(boards: list[dict]) -> tuple[InfoboardsService, object]:
    """InfoboardsService с заглушками БД, каталога и логина — отдаёт заданный board.many."""
    mock_session = MagicMock(spec=AsyncSession)
    mock_result = MagicMock()
    mock_result.scalar_one_or_none.return_value = "https://catalog.example/"
    mock_session.execute = AsyncMock(return_value=mock_result)

    client_patch = patch("services.infoboards_service.service.create_http_client")
    auth_patch = patch("services.infoboards_service.service.AuthService")

    create_client = client_patch.start()
    mock_client = MagicMock()
    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.json.return_value = {"data": {"board": {"many": boards}}}
    mock_client.post = AsyncMock(return_value=mock_resp)
    mock_client.aclose = AsyncMock()
    create_client.return_value = mock_client

    auth_mock = auth_patch.start()
    auth_instance = MagicMock()
    auth_instance.login = AsyncMock(return_value={"session": "x"})
    auth_mock.return_value = auth_instance

    service = InfoboardsService(db=mock_session, settings=MagicMock())
    return service, (client_patch, auth_patch)


# Каскад: корень виден на стартовой, под-кабинеты скрыты, у старого борда флага нет вовсе.
CASCADE = [
    {"id": "P001", "title": "Кабинет СМК", "author": "", "hiddenOnStartPage": False},
    {"id": "P002", "title": "СМК Рекомендованные материалы", "author": "", "hiddenOnStartPage": True},
    {"id": "P003", "title": "СМК Мои внутренние документы", "author": "", "hiddenOnStartPage": True},
    {"id": "P004", "title": "Старый кабинет без флага", "author": ""},
]


@pytest.mark.asyncio
async def test_list_hides_cascade_sub_cabinets() -> None:
    """Без title отдаются только кабинеты стартовой страницы; под-кабинеты каскада не перечисляются."""
    service, patches = _service_with_boards(CASCADE)
    try:
        data = await service.get_link_by_reg_and_title(reg="350832", title=None)
    finally:
        for p in patches:
            p.stop()
    assert [i["id"] for i in data["items"]] == ["P001", "P004"]


@pytest.mark.asyncio
async def test_list_include_hidden_returns_everything() -> None:
    """include_hidden=True — прежнее поведение, полный список."""
    service, patches = _service_with_boards(CASCADE)
    try:
        data = await service.get_link_by_reg_and_title(reg="350832", title=None, include_hidden=True)
    finally:
        for p in patches:
            p.stop()
    assert [i["id"] for i in data["items"]] == ["P001", "P002", "P003", "P004"]


@pytest.mark.asyncio
async def test_title_lookup_finds_hidden_board() -> None:
    """Поиск по title достаёт и скрытый под-кабинет: ссылка на него нужна независимо от плитки."""
    service, patches = _service_with_boards(CASCADE)
    try:
        data = await service.get_link_by_reg_and_title(reg="350832", title="СМК Мои внутренние документы")
    finally:
        for p in patches:
            p.stop()
    assert data["id"] == "P003"
    assert "infoboard=P003" in data["link"]


@pytest.mark.asyncio
async def test_all_hidden_returns_empty_list_without_error() -> None:
    """Скрыты все — отдаём пустой список, а не падаем и не показываем всё подряд."""
    boards = [{"id": "P002", "title": "Под-кабинет", "author": "", "hiddenOnStartPage": True}]
    service, patches = _service_with_boards(boards)
    try:
        data = await service.get_link_by_reg_and_title(reg="350832", title=None)
    finally:
        for p in patches:
            p.stop()
    assert data == {"items": []}
