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

from common.exceptions import ParseError
from services.users_service.html_parser import _Document, _ancestor, _disabled, _form_controls, _successful


def _read_form(html: str, *, required_names: set[str] | None = None, acl: bool = False):
    """Read one identifiable form using browser successful-control semantics.

    Repeated names and empty values are retained; unrecognised/ambiguous forms
    fail before any administrative write. A fragment is not a complete form.
    """
    root = _Document(html).root
    candidates = []
    for form in (node for node in root.walk() if node.tag == "form"):
        if _ancestor(form, "form") is not None:
            raise ParseError(code="ACL_FORM_INVALID", message="Nested administrative forms are unsupported")
        controls = _form_controls(root, form)
        names = {node.attrs.get("name") for node in controls}
        if required_names and not required_names <= names:
            continue
        if acl and not any((name or "").startswith("acl_infoboard_") for name in names):
            continue
        if any(node.tag == "input" and node.attrs.get("type", "").lower() == "file" and node.attrs.get("name") for node in controls):
            raise ParseError(code="ACL_FORM_INVALID", message="Administrative file controls require a confirmed multipart contract")
        candidates.append((form, controls))
    if len(candidates) != 1:
        raise ParseError(code="ACL_FORM_UNRECOGNIZED", message="A unique complete administrative form was not found; capture the live form before writing")
    return candidates[0]


def parse_form_pairs(html: str, *, required_names: set[str] | None = None, acl: bool = False, include_save: bool = False) -> list[tuple[str, str]]:
    form, controls = _read_form(html, required_names=required_names, acl=acl)
    pairs = _successful(controls)
    if include_save:
        # Submit only a real, identifiable Save control. Do not invent cmd=save.
        submitters = []
        for node in controls:
            kind = (node.attrs.get("type") or ("submit" if node.tag == "button" else "text")).lower()
            if node.tag not in {"input", "button"} or kind != "submit" or _disabled(node):
                continue
            label = node.attrs.get("value") or node.text()
            normalized_label = " ".join(label.casefold().split())
            if "сохранить" in normalized_label or "save" in normalized_label or normalized_label == "установить":
                submitters.append(node)
        if len(submitters) != 1:
            raise ParseError(code="ACL_SAVE_CONTROL_UNKNOWN", message="A unique actual Save submitter was not found in the ACL form")
        submitter = submitters[0]
        if submitter.attrs.get("name"):
            pairs.append((submitter.attrs["name"], submitter.attrs.get("value") or ""))
    if acl:
        acl_values(pairs)
    return pairs


def acl_control_names(html: str) -> set[str]:
    _, controls = _read_form(html, acl=True)
    return {node.attrs["name"] for node in controls if node.attrs.get("name") and not _disabled(node)}


def acl_checkbox_values(html: str) -> dict[str, str]:
    _, controls = _read_form(html, acl=True)
    return {
        node.attrs["name"]: node.attrs.get("value", "on")
        for node in controls
        if node.tag == "input" and node.attrs.get("name")
        and node.attrs.get("type", "").lower() in {"checkbox", "radio"}
        and not _disabled(node)
    }


def setup_form_pairs(html: str) -> list[tuple[str, str]]:
    """Use the actual settings submitter/option, preserving the entire form."""
    form, controls = _read_form(html, required_names={"setup2"})
    candidates = []
    for node in form.walk():
        if _disabled(node):
            continue
        if node.tag == "input" and node.attrs.get("name") == "setup2" and "настройки" in (node.attrs.get("value") or "").casefold():
            candidates.append(node.attrs.get("value") or "")
        if node.tag == "select" and node.attrs.get("name") == "setup2":
            candidates.extend(option.attrs.get("value", option.text()) for option in node.walk() if option.tag == "option" and "настройки сервисов" in option.text().casefold() and not _disabled(option))
    if len(candidates) != 1:
        raise ParseError(code="ACL_SETUP_CONTROL_UNKNOWN", message="The actual service-settings control was not identified")
    return [(name, value) for name, value in _successful(controls) if name != "setup2"] + [("setup2", candidates[0])]


def acl_values(pairs: list[tuple[str, str]]) -> dict[str, dict]:
    """Validate ACL JSON rather than interpreting malformed values as no rights."""
    result = {}
    for name, value in pairs:
        if not name.startswith("acl_infoboard_"):
            continue
        try:
            parsed = json.loads(value)
            if not isinstance(parsed, dict) or not isinstance(parsed.get("kw"), list):
                raise ValueError
            if any(not str(group).isdigit() for group in parsed["kw"]):
                raise ValueError
        except (ValueError, TypeError) as error:
            raise ParseError(code="ACL_FORM_INVALID", message=f"Invalid ACL field {name!r}") from error
        if name in result and result[name] != parsed:
            raise ParseError(code="ACL_FORM_INVALID", message=f"Conflicting repeated ACL field {name!r}")
        result[name] = parsed
    if not result:
        raise ParseError(code="ACL_FORM_UNRECOGNIZED", message="Administrative form contains no ACL fields")
    return result


def verify_acl_form(expected: list[tuple[str, str]], actual_html: str) -> None:
    """Confirm all submitted ACLs, including untouched boards, by a fresh read."""
    actual_pairs = parse_form_pairs(actual_html, acl=True)
    expected_acl = acl_values(expected)
    actual_acl = acl_values(actual_pairs)
    for name, value in expected_acl.items():
        actual = actual_acl.get(name)
        if actual is None or {int(g) for g in actual["kw"]} != {int(g) for g in value["kw"]} or {k: v for k, v in actual.items() if k != "kw"} != {k: v for k, v in value.items() if k != "kw"}:
            raise ParseError(code="ACL_WRITE_NOT_CONFIRMED", message=f"Fresh catalog form does not confirm ACL field {name!r}")
    expected_flags = {name for name, _ in expected if name.startswith("infoboard_")}
    actual_flags = {name for name, _ in actual_pairs if name.startswith("infoboard_")}
    if expected_flags != actual_flags:
        raise ParseError(code="ACL_WRITE_NOT_CONFIRMED", message="Fresh catalog form does not confirm board activation flags")


def acl_write_needed(expected: list[tuple[str, str]], current_html: str) -> bool:
    try:
        verify_acl_form(expected, current_html)
    except ParseError as error:
        if error.code == "ACL_WRITE_NOT_CONFIRMED":
            return True
        raise
    return False

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
    """Read existing ACL inputs independent of attribute order/quoting."""
    acl = {}
    for node in _Document(html).root.walk():
        name = node.attrs.get("name") or ""
        if node.tag != "input" or not name.startswith("acl_infoboard_") or _disabled(node):
            continue
        key = name.removeprefix("acl_infoboard_").lower()
        value = node.attrs.get("value") or ""
        if key in acl and acl[key] != value:
            raise ParseError(code="ACL_FORM_INVALID", message=f"Conflicting repeated ACL field {name!r}")
        acl[key] = value
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
    full_form: dict[str, str] | list[tuple[str, str]],
    acl_overrides: dict[str, list[int]],
    extra_board_ids: set[str] | None = None,
    *,
    available_controls: set[str] | None = None,
    checkbox_values: dict[str, str] | None = None,
    field_overrides: dict[str, list[int]] | None = None,
) -> list[tuple[str, str]]:
    """Overlay only existing ACL controls; preserve every other successful pair.

    extra_board_ids is retained for API compatibility, never used to synthesize
    controls absent from the live form. Unchecked controls can be enabled only
    when their existence was confirmed by the complete DOM parser.
    """
    pairs = list(full_form.items()) if isinstance(full_form, dict) else list(full_form)
    existing_acl = acl_values(pairs)
    controls = available_controls if available_controls is not None else {key for key, _ in pairs}
    overrides = {}
    desired_fields = {}
    for board, groups in acl_overrides.items():
        board = board.lower()
        desired_fields[f"acl_infoboard_{board}"] = groups
        desired_fields[f"acl_infoboard_{board}_view"] = groups
    desired_fields.update(field_overrides or {})
    for name, groups in desired_fields.items():
        if not name.startswith("acl_infoboard_"):
            raise ParseError(code="ACL_PLAN_INVALID", message=f"Invalid target ACL control: {name!r}")
        if any(not isinstance(group, int) or isinstance(group, bool) or group < 0 for group in groups):
            raise ParseError(code="ACL_PLAN_INVALID", message="ACL group IDs must be nonnegative integers")
        if name not in existing_acl:
            raise ParseError(code="ACL_BOARD_NOT_FOUND", message=f"Required board ACL control is absent: {name!r}")
        value = dict(existing_acl[name])
        value["kw"] = [str(group) for group in dict.fromkeys(groups)]
        overrides[name] = json.dumps(value, separators=(",", ":"))
        flag_name = name.removeprefix("acl_")
        if flag_name in controls:
            overrides[flag_name] = (checkbox_values or dict(pairs)).get(flag_name, "on") if groups else None
    result = [(name, overrides.get(name, value)) for name, value in pairs if name not in overrides or overrides[name] is not None]
    present = {name for name, _ in pairs}
    result.extend((name, value) for name, value in overrides.items() if name not in present and value is not None)
    return result
