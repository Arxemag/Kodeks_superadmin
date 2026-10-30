"""Access-логи сохраняются, одноразовый тикет не попадает в строку запроса."""
from __future__ import annotations

from copy import deepcopy
import io
import logging
from types import SimpleNamespace

import pytest
from uvicorn.logging import AccessFormatter

from common.sso_logging import SSOAccessLogFilter


def access_record(path):
    return logging.LogRecord("uvicorn.access", logging.INFO, "http_protocol.py", 1,
                             '%s - "%s %s HTTP/%s" %d',
                             ("127.0.0.1:1234", "GET", path, "1.1", 302), None)


@pytest.mark.parametrize("path", ["/api/expert/sso?ticket=private-ticket", "/api/expert/sso/?ticket=private-ticket&unexpected=private-data"])
def test_real_uvicorn_record_redacts_entire_sso_query(path):
    record = access_record(path)
    output = io.StringIO()
    handler = logging.StreamHandler(output)
    handler.setFormatter(AccessFormatter('%(client_addr)s - "%(request_line)s" %(status_code)s', use_colors=False))
    handler.addFilter(SSOAccessLogFilter())
    handler.handle(record)
    line = output.getvalue()
    assert "private-ticket" not in line and "private-data" not in line
    assert "[REDACTED]" in line
    assert "GET /api/expert/sso" in line and "HTTP/1.1" in line and "302" in line


@pytest.mark.parametrize("path", ["/api/expert/health", "/api/expert/sso", "/api/expert/sso/", "/api/expert/sso-ticket?other=keep", "/api/expert/sso-other?ticket=keep", "/other?ticket=keep"])
def test_unrelated_or_queryless_access_records_unchanged(path):
    record = access_record(path)
    before = record.args
    assert SSOAccessLogFilter().filter(record) is True
    assert record.args is before
    assert path in record.getMessage()


@pytest.mark.parametrize("args", [(), None, {"message": "ordinary"}, ("only",), ("client", "GET", None)])
def test_record_without_standard_access_args_does_not_fail(args):
    record = logging.LogRecord("uvicorn.access", logging.INFO, "file", 1, "ordinary", (), None)
    record.args = args
    assert SSOAccessLogFilter().filter(record) is True
    assert record.args is args


def test_filter_does_not_mutate_original_request_scope():
    scope = {"path": "/api/expert/sso", "query_string": b"ticket=private-ticket"}
    before = deepcopy(scope)
    record = access_record(scope["path"] + "?" + scope["query_string"].decode())
    SSOAccessLogFilter().filter(record)
    assert scope == before
    assert "private-ticket" not in record.getMessage()


def test_main_adds_filter_to_copy_of_uvicorn_access_configuration(monkeypatch):
    import main
    original = deepcopy(main.LOGGING_CONFIG)
    calls = []
    monkeypatch.setattr(main, "get_settings", lambda: SimpleNamespace(PORT=8123))
    monkeypatch.setattr(main.uvicorn, "run", lambda *args, **kwargs: calls.append((args, kwargs)))
    main.main()
    assert len(calls) == 1
    args, options = calls[0]
    assert args == ("services.auth_service.app:app",)
    assert options["port"] == 8123
    configured = options["log_config"]
    assert configured["filters"]["sso_query"]["()"] == "common.sso_logging.SSOAccessLogFilter"
    assert "sso_query" in configured["handlers"]["access"]["filters"]
    assert configured["loggers"]["uvicorn.access"]["handlers"] == original["loggers"]["uvicorn.access"]["handlers"]
    assert main.LOGGING_CONFIG == original
