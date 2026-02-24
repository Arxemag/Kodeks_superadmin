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
