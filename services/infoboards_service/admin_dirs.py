"""
Общая логика POST /admin/dirs: парсинг страницы, merge, сохранение всех полей.

Используется: add_cabinet_group.py, InitCompanyService._apply_acl.

Цепочка по Fiddler:
  1) POST /admin/dir с path, type, to, com, grps_*, action=save, setup2=Настройки сервисов, n=2
     → ответ содержит HTML с формой ACL (acl_infoboard_*, packprocess=on, …).
  2) Парсим эту форму, подставляем свои acl_infoboard_* для нужного кабинета.
  3) POST /admin/dirs с полной формой (все поля как в ответе п.1, только ACL изменён).

ВАЖНО: Передаём все настройки из ответа п.1 и только нужные поля меняем (acl_infoboard_*).
"""
from __future__ import annotations

import json
import re
from html.parser import HTMLParser

_ACL_INPUT_RE = re.compile(
    r'<input[^>]*name=["\']acl_infoboard_([^"\']+)["\'][^>]*value=["\']([^"\']*)["\']',
    re.I,
)
_ACL_INPUT_RE_ALT = re.compile(
    r'name=["\']acl_infoboard_([^"\']+)["\'][^>]*value=["\']([^"\']*)["\']',
    re.I,
)
# value='{"kw":["530"]}' — внутри есть ", поэтому [^"\']* обрывается. Отдельно value='...' и value="...".
_ACL_INPUT_VALUE_SQ = re.compile(
    r"name=['\"]acl_infoboard_([^\"']+)['\"][^>]*value='([^']*)'",
    re.I,
)
_ACL_INPUT_VALUE_DQ = re.compile(
    r'name=[\'"]acl_infoboard_([^"\']+)[\'"][^>]*value="([^"]*)"',
    re.I,
)

# Пустой ACL в формате каталога (валидный JSON, чтобы не ломать сервер).
_EMPTY_ACL_JSON = '{"kw":[]}'

# Резерв для базовых полей, если страница не содержит их.
# По Fiddler: packprocess=on, inspectionactuallinks=on; acl_* — JSON (пустой {"kw":[]} при отсутствии).
_BASE_PARAMS_FALLBACK = {
    "packprocess": "on",
    "acl_packprocess": _EMPTY_ACL_JSON,
    "acl_infoboardsedit": _EMPTY_ACL_JSON,
    "acl_inspectactuallinks": _EMPTY_ACL_JSON,
    "inspectionactuallinks": "on",
    "n": "2",
    "setup2": "",
    "set": "",
    "acl_unstoragero": "",
    "acl_unstorage": "",
    "acl_unstoragefull": "",
    "acl_unprint": "",
    "unprinttext": "Установлен административный запрет на выполнение операции печати.",
    "acl_unsave": "",
    "unsavetext": "Установлен административный запрет на выполнение операции сохранения в файл.",
    "acl_uncopy": "",
    "uncopytext": "Установлен административный запрет на выполнение операции копирования.",
    "uncopylength": "0",
    "desiredtab": "-1",
    "desiredtabas": "-1",
}


# Regex-резерв: name="X" value="Y" (порядок атрибутов может быть разным)
_INPUT_NAME_VALUE_RE = re.compile(
    r'name\s*=\s*["\']([^"\']+)["\'][^>]*?value\s*=\s*["\']([^"\']*)["\']',
    re.I,
)
_INPUT_VALUE_NAME_RE = re.compile(
    r'value\s*=\s*["\']([^"\']*)["\'][^>]*?name\s*=\s*["\']([^"\']+)["\']',
    re.I,
)


def parse_full_form_from_html(html: str) -> dict[str, str]:
    """
    Парсит ВСЕ input-поля из HTML страницы /admin/dirs.
    Сохраняет packprocess, acl_packprocess, acl_infoboardsedit, acl_inspectactuallinks,
    inspectionactuallinks, n и прочие системные поля.
    При пустом результате — regex fallback для страниц с нестандартной структурой.
    """
    class FormParser(HTMLParser):
        def __init__(self) -> None:
            super().__init__()
            self.fields: dict[str, str] = {}

        def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
            if tag.lower() != "input":
                return
            attrd = dict((k.lower(), (v or "")) for k, v in attrs)
            name = attrd.get("name")
            if not name:
                return
            itype = attrd.get("type", "text").lower()
            value = (attrd.get("value") or "").replace("&quot;", '"')
            checked = "checked" in attrd
            if itype in ("checkbox", "radio"):
                if checked:
                    self.fields[name] = value or "on"
            else:
                self.fields[name] = value

    parser = FormParser()
    try:
        parser.feed(html)
    except Exception:
        pass
    if parser.fields:
        return parser.fields

    fields: dict[str, str] = {}
    for m in _INPUT_NAME_VALUE_RE.finditer(html):
        fields[m.group(1)] = (m.group(2) or "").replace("&quot;", '"')
    for m in _INPUT_VALUE_NAME_RE.finditer(html):
        name = m.group(2)
        if name not in fields:
            fields[name] = (m.group(1) or "").replace("&quot;", '"')
    return fields


def parse_acl_from_html(html: str) -> dict[str, str]:
    """Парсит acl_infoboard_* из HTML. Ключ lowercase, value с заменой &quot;."""
    acl: dict[str, str] = {}
    for m in _ACL_INPUT_RE.finditer(html):
        acl[m.group(1).lower()] = (m.group(2) or "").replace("&quot;", '"')
    for m in _ACL_INPUT_RE_ALT.finditer(html):
        k = m.group(1).lower()
        if k not in acl:
            acl[k] = (m.group(2) or "").replace("&quot;", '"')
    for m in _ACL_INPUT_VALUE_SQ.finditer(html):
        k = m.group(1).lower()
        if k not in acl:
            acl[k] = (m.group(2) or "").replace("&quot;", '"')
    for m in _ACL_INPUT_VALUE_DQ.finditer(html):
        k = m.group(1).lower()
        if k not in acl:
            acl[k] = (m.group(2) or "").replace("&quot;", '"')
    return acl


def acl_to_json(group_ids: list[int]) -> str:
    """Формат каталога: {"kw":["31","32"]} без пробелов."""
    return json.dumps({"kw": [str(g) for g in group_ids]}, separators=(",", ":"))


def parse_acl_to_group_ids(acl_json: str) -> list[int]:
    """Парсит ACL JSON в список group_id."""
    try:
        data = json.loads(acl_json) if acl_json else {"kw": []}
    except json.JSONDecodeError:
        return []
    kw = data.get("kw") or []
    return [int(k) for k in kw if str(k).isdigit()]


def parse_grps_group_ids_from_html(html: str) -> list[int]:
    """
    Из ответа GET /admin/dir?n=2 извлекает ID групп из полей grps_1, grps_3, grps_5, …
    value='{"kw":["1","4","8",...]}' → [1, 4, 8, …]. Без дубликатов, по возрастанию.
    """
    form = parse_full_form_from_html(html)
    ids: set[int] = set()
    for key, val in form.items():
        if key.startswith("grps_") and val:
            ids.update(parse_acl_to_group_ids(val))
    return sorted(ids)


def add_group_to_acl_json(acl_json: str, group_id: int) -> list[int]:
    """Добавляет group_id в существующий ACL. Возвращает новый список group_ids."""
    ids = parse_acl_to_group_ids(acl_json)
    if group_id not in ids:
        ids = ids + [group_id]
    return ids


def build_admin_dirs_form(
    full_form: dict[str, str],
    acl_overrides: dict[str, list[int]],
    extra_board_ids: set[str] | None = None,
) -> list[tuple[str, str]]:
    """
    Строит форму для POST /admin/dirs на основе full_form со страницы.

    Сохраняет packprocess, acl_packprocess, acl_infoboardsedit, acl_inspectactuallinks,
    n и др. Меняет только acl_infoboard_* и infoboard_* для кабинетов из acl_overrides.

    full_form: результат parse_full_form_from_html(html)
    acl_overrides: board_key -> [group_ids] — наши изменения
    extra_board_ids: board_id из GraphQL (если нет на странице)
    """
    form: dict[str, str] = dict(full_form)
    for k, v in _BASE_PARAMS_FALLBACK.items():
        if k not in form:
            form[k] = v
    form["cmd"] = "save"

    all_keys: set[str] = set()
    for k in form:
        if k.startswith("acl_infoboard_"):
            base = k.rsplit("_view", 1)[0] if k.endswith("_view") else k
            base = base.replace("acl_infoboard_", "")
            if base:
                all_keys.add(base.lower())
    for k in acl_overrides:
        all_keys.add(k.lower())
    if extra_board_ids:
        all_keys.update(bid.lower() for bid in extra_board_ids)

    for bk in sorted(all_keys):
        if not bk or bk.endswith("_view"):
            continue
        vk = f"{bk}_view"
        group_ids = acl_overrides.get(bk) or acl_overrides.get(bk.upper())
        if group_ids is not None:
            acl_val = acl_val_view = acl_to_json(group_ids)
        else:
            acl_val = form.get(f"acl_infoboard_{bk}", "") or _EMPTY_ACL_JSON
            acl_val_view = form.get(f"acl_infoboard_{vk}", "") or acl_val

        form[f"acl_infoboard_{bk}"] = acl_val
        form[f"acl_infoboard_{vk}"] = acl_val_view
        # infoboard_X=on только при непустом ACL (по Fiddler без групп чекбокс не ставится).
        has_groups = bool(parse_acl_to_group_ids(acl_val) or parse_acl_to_group_ids(acl_val_view))
        if has_groups:
            form[f"infoboard_{bk}"] = "on"
            form[f"infoboard_{vk}"] = "on"

    return list(form.items())
