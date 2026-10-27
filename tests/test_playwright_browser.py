from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from common.playwright_browser import open_chromium


@pytest.mark.asyncio
async def test_error_in_remote_browser_operation_is_not_retried(monkeypatch):
    monkeypatch.setattr('common.playwright_browser.get_settings', lambda: SimpleNamespace(CHROMIUM_WS_ENDPOINT='ws://browser:3000'))
    browser = SimpleNamespace(close=AsyncMock())
    chromium = SimpleNamespace(connect_over_cdp=AsyncMock(return_value=browser), launch=AsyncMock())
    failure = ValueError('form save failed')
    with pytest.raises(ValueError) as caught:
        async with open_chromium(SimpleNamespace(chromium=chromium)):
            raise failure
    assert caught.value is failure
    chromium.connect_over_cdp.assert_awaited_once()
    chromium.launch.assert_not_awaited()
    browser.close.assert_awaited_once()
