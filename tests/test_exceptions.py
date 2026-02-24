"""Tests for common.exceptions."""
from __future__ import annotations

import pytest

from common.exceptions import (
    AuthError,
    ConfigError,
    DatabaseError,
    NetworkError,
    ParseError,
    ServiceError,
)


def test_service_error_defaults() -> None:
    e = ServiceError()
    assert e.code == "SERVICE_ERROR"
    assert e.message == "Service error"
    assert e.http_status == 500
    assert str(e) == "Service error"


def test_service_error_custom() -> None:
    e = ServiceError(code="CUSTOM", message="msg", http_status=400)
    assert e.code == "CUSTOM"
    assert e.message == "msg"
    assert e.http_status == 400


def test_config_error() -> None:
    e = ConfigError(message="Bad config")
    assert e.code == "CONFIG_ERROR"
    assert e.http_status == 500


def test_database_error() -> None:
    e = DatabaseError(message="Connection failed")
    assert e.code == "DATABASE_ERROR"
    assert e.http_status == 500


def test_network_error() -> None:
    e = NetworkError(code="TIMEOUT", message="Timeout")
    assert e.code == "TIMEOUT"
    assert e.message == "Timeout"
    assert e.http_status == 502


def test_auth_error() -> None:
    e = AuthError(code="REG_NOT_FOUND", message="reg not in DB", http_status=404)
    assert e.code == "REG_NOT_FOUND"
    assert e.http_status == 404


def test_parse_error() -> None:
    e = ParseError(code="HTML_PARSE_ERROR", message="Missing uid")
    assert e.code == "HTML_PARSE_ERROR"
    assert e.http_status == 422
