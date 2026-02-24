"""
Парсинг HTML-форм каталога: страница пользователя и страница групп.

Используется в UserService: parse_user_state — для страницы /users/usr (поля формы и выбранные группы),
parse_groups_catalog — для страницы /users/groups (ссылки и option с id и названием группы).
При ошибках разбора выбрасывается ParseError.
"""
from __future__ import annotations

import html
import re
from dataclasses import dataclass

from common.exceptions import ParseError


# Регулярки для извлечения input-полей, имён, значений, отмеченных grp и ссылок/option групп
_INPUT_RE = re.compile(r"<input\b[^>]*>", re.IGNORECASE)
_NAME_RE = re.compile(r'\bname\s*=\s*["\']([^"\']+)["\']', re.IGNORECASE)
_VALUE_RE = re.compile(r'\bvalue\s*=\s*["\']([^"\']*)["\']', re.IGNORECASE)
_CHECKED_RE = re.compile(r"\bchecked\b", re.IGNORECASE)
_GROUP_LINK_RE = re.compile(r"""<a\b[^>]*href=["'][^"']*grp\?grp=(\d+)[^"']*["'][^>]*>(.*?)</a>""", re.IGNORECASE | re.DOTALL)
_GROUP_OPTION_RE = re.compile(r"""<option\b[^>]*value=["']?(\d+)["']?[^>]*>(.*?)</option>""", re.IGNORECASE | re.DOTALL)
_TAG_RE = re.compile(r"<[^>]+>")

_TRACKED_FIELDS = {"uid", "psw", "name", "org", "pos", "mail", "telephon", "end", "id"}


@dataclass(frozen=True)
class UserState:
    """Результат разбора страницы пользователя: словарь полей формы и список id выбранных групп."""
    fields: dict[str, str]
    groups: list[int]


def parse_user_state(page_html: str) -> UserState:
    """Разбирает HTML страницы пользователя: извлекает поля из input (uid, psw, name, org, ...) и отмеченные grp; при отсутствии uid/psw — ParseError."""
    fields: dict[str, str] = {}
    groups: list[int] = []

    for tag in _INPUT_RE.findall(page_html):
        name_match = _NAME_RE.search(tag)
        if not name_match:
            continue
        name = name_match.group(1)
        value_match = _VALUE_RE.search(tag)
        value = html.unescape(value_match.group(1)) if value_match else ""

        if name in _TRACKED_FIELDS:
            fields[name] = value
            continue

        if name == "grp":
            if not _CHECKED_RE.search(tag):
                continue
            try:
                grp = int(value)
            except ValueError:
                raise ParseError(code="HTML_PARSE_ERROR", message=f"Invalid group value in HTML: {value!r}")
            groups.append(grp)

    if "uid" not in fields:
        raise ParseError(code="HTML_PARSE_ERROR", message="Field 'uid' was not found in user form")
    if "psw" not in fields:
        raise ParseError(code="HTML_PARSE_ERROR", message="Field 'psw' was not found in user form")

    groups = sorted(set(groups))
    return UserState(fields=fields, groups=groups)


def parse_groups_catalog(page_html: str) -> dict[str, int]:
    """Разбирает HTML страницы групп: ссылки вида grp?grp=ID и option; возвращает словарь название -> id группы."""
    groups: dict[str, int] = {}
    for grp_id_raw, title_html in _GROUP_LINK_RE.findall(page_html):
        title = html.unescape(_TAG_RE.sub("", title_html)).strip()
        if not title:
            continue
        try:
            grp_id = int(grp_id_raw)
        except ValueError:
            continue
        groups[title] = grp_id
    for grp_id_raw, title_html in _GROUP_OPTION_RE.findall(page_html):
        title = html.unescape(_TAG_RE.sub("", title_html)).strip()
        if not title:
            continue
        try:
            grp_id = int(grp_id_raw)
        except ValueError:
            continue
        groups[title] = grp_id
    return groups
