"""Serialize cooperating writers across processes sharing the integration database."""
from __future__ import annotations

import asyncio
from contextlib import AsyncExitStack, asynccontextmanager
from contextvars import ContextVar
import hashlib
from time import monotonic
from urllib.parse import urlsplit, urlunsplit

from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError

from common.config import get_settings
from common.db import get_db_session
from common.exceptions import NetworkError


# ContextVar values propagate to child tasks; owner identity prevents inheritance
# from granting a child access to a lock held by its parent task.
_held: ContextVar[tuple[tuple[int, asyncio.Task], ...]] = ContextVar("catalog_locks", default=())


def canonical_catalog_url(base_url: str) -> str:
    """Canonicalize known URL spelling, preserving the catalog path.

    DNS aliases and different reverse-proxy paths cannot be inferred here.
    They must use one configured canonical base URL to share a lock.
    """
    parts = urlsplit(base_url)
    scheme = parts.scheme.lower()
    if scheme not in {"http", "https"} or not parts.hostname:
        raise ValueError("Catalog URL must have an HTTP(S) scheme and hostname")
    hostname = parts.hostname.lower()
    host = f"[{hostname}]" if ":" in hostname else hostname
    port = parts.port
    if port is not None and (scheme, port) not in {("http", 80), ("https", 443)}:
        host += f":{port}"
    return urlunsplit((scheme, host, parts.path.rstrip("/") or "/", "", ""))


def _lock_key(base_url: str) -> int:
    digest = hashlib.sha256(canonical_catalog_url(base_url).encode("utf-8")).digest()
    return int.from_bytes(digest[:8], "big", signed=True)


@asynccontextmanager
async def catalog_operation(base_url: str):
    """Hold a transaction advisory lock on a dedicated DB session.

    Transaction cleanup releases the lock on success, error, or cancellation.
    This is cooperative serialization, not fencing of a timed-out external POST.
    """
    owner = asyncio.current_task()
    key = _lock_key(base_url)
    held = _held.get()
    if (key, owner) in held:
        yield
        return

    deadline = monotonic() + get_settings().CATALOG_LOCK_TIMEOUT
    while True:
        remaining = deadline - monotonic()
        if remaining <= 0:
            raise NetworkError(code="CATALOG_LOCK_TIMEOUT", message="Timed out waiting for the catalog database lock")
        body_error = None
        try:
            async with AsyncExitStack() as resources:
                # Both pool checkout and SQL are bounded, but the deadline does
                # not limit the protected business operation after acquisition.
                async with asyncio.timeout(remaining):
                    session = await resources.enter_async_context(get_db_session())
                    await resources.enter_async_context(session.begin())
                    result = await session.execute(
                        text("SELECT pg_try_advisory_xact_lock(:key)"), {"key": key},
                    )
                    acquired = bool(result.scalar_one())
                if acquired:
                    token = _held.set(held + ((key, owner),))
                    try:
                        yield
                    except BaseException as error:
                        body_error = error
                        raise
                    finally:
                        _held.reset(token)
                    return
            # A false try has closed transaction/session and returned the
            # connection before waiting. Waiters cannot occupy the entire pool.
        except SQLAlchemyError as error:
            if body_error is error:
                raise  # Preserve the business exception; it is not acquisition.
            raise NetworkError(code="CATALOG_LOCK_FAILED", message="Catalog database lock lifecycle failed") from error
        except TimeoutError as error:
            if body_error is error:
                raise
            raise NetworkError(code="CATALOG_LOCK_TIMEOUT", message="Timed out acquiring the catalog database lock") from error
        remaining = deadline - monotonic()
        if remaining <= 0:
            raise NetworkError(code="CATALOG_LOCK_TIMEOUT", message="Timed out waiting for the catalog database lock")
        await asyncio.sleep(min(0.1, remaining))
