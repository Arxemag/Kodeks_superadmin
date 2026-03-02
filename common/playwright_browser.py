"""
Утилиты подключения Playwright к Chromium:
- удалённый браузер через CDP (CHROMIUM_WS_ENDPOINT),
- fallback на локальный launch() при отсутствии/недоступности endpoint.
"""
from __future__ import annotations

from contextlib import asynccontextmanager
from typing import Any

from common.config import get_settings


def _build_cdp_candidates(endpoint: str) -> list[str]:
    ep = endpoint.strip()
    if not ep:
        return []
    candidates: list[str] = [ep]
    if ep.startswith("ws://"):
        http_ep = "http://" + ep[len("ws://") :]
        if http_ep not in candidates:
            candidates.append(http_ep)
    elif ep.startswith("wss://"):
        https_ep = "https://" + ep[len("wss://") :]
        if https_ep not in candidates:
            candidates.append(https_ep)
    return candidates


@asynccontextmanager
async def open_chromium(playwright: Any, logger: Any = None):
    """
    Возвращает Playwright Browser:
    1) connect_over_cdp(CHROMIUM_WS_ENDPOINT), если endpoint задан;
    2) chromium.launch(headless=True) как fallback.
    """
    endpoint = get_settings().CHROMIUM_WS_ENDPOINT.strip()
    if endpoint:
        errors: list[str] = []
        for candidate in _build_cdp_candidates(endpoint):
            try:
                browser = await playwright.chromium.connect_over_cdp(candidate, timeout=30000)
                if logger:
                    logger.debug("Playwright connected to remote Chromium via CDP: %s", candidate)
                try:
                    yield browser
                finally:
                    await browser.close()
                return
            except Exception as e:  # pragma: no cover
                errors.append(f"{candidate}: {e!r}")
        if logger:
            logger.warning(
                "Remote Chromium unavailable (CHROMIUM_WS_ENDPOINT=%r). Fallback to local launch(). Errors: %s",
                endpoint,
                "; ".join(errors[:2]),
            )

    browser = await playwright.chromium.launch(headless=True)
    try:
        yield browser
    finally:
        await browser.close()
