"""Tests for services.users_service.html_parser."""
from __future__ import annotations

import pytest

from common.exceptions import ParseError
from services.users_service.html_parser import (
    UserState,
    parse_groups_catalog,
    parse_user_state,
)


def test_parse_user_state_minimal() -> None:
    html = '''
    <input name="uid" value="u1">
    <input name="psw" value="p1">
    '''
    state = parse_user_state(html)
    assert state.fields["uid"] == "u1"
    assert state.fields["psw"] == "p1"
    assert state.groups == []


def test_parse_user_state_with_groups() -> None:
    html = '''
    <input name="uid" value="u1">
    <input name="psw" value="p1">
    <input type="checkbox" name="grp" value="5" checked>
    <input type="checkbox" name="grp" value="3" checked>
    '''
    state = parse_user_state(html)
    assert sorted(state.groups) == [3, 5]


def test_parse_user_state_missing_uid_raises() -> None:
    html = '<input name="psw" value="p1">'
    with pytest.raises(ParseError) as exc_info:
        parse_user_state(html)
    assert "uid" in exc_info.value.message.lower() or "not found" in exc_info.value.message.lower()


def test_parse_user_state_missing_psw_raises() -> None:
    html = '<input name="uid" value="u1">'
    with pytest.raises(ParseError) as exc_info:
        parse_user_state(html)
    assert "psw" in exc_info.value.message.lower() or "not found" in exc_info.value.message.lower()


def test_parse_user_state_invalid_grp_value_raises() -> None:
    html = '''
    <input name="uid" value="u1">
    <input name="psw" value="p1">
    <input type="checkbox" name="grp" value="not_a_number" checked>
    '''
    with pytest.raises(ParseError):
        parse_user_state(html)


def test_parse_groups_catalog_empty() -> None:
    assert parse_groups_catalog("<html><body></body></html>") == {}


def test_parse_groups_catalog_links() -> None:
    html = '<a href="/users/grp?grp=10">Group Ten</a>'
    assert parse_groups_catalog(html) == {"Group Ten": 10}


def test_parse_groups_catalog_options() -> None:
    html = '<option value="20">Group Twenty</option>'
    assert parse_groups_catalog(html) == {"Group Twenty": 20}


def test_parse_groups_catalog_strips_tags_from_title() -> None:
    html = '<a href="/grp?grp=1"><b>Bold</b> Title</a>'
    result = parse_groups_catalog(html)
    assert 1 in result.values()
    title = list(result.keys())[0]
    assert "Bold" in title and "Title" in title
