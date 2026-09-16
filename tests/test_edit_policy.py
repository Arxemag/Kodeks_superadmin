"""
Тесты маски редактируемости кабинетов (services.infoboards_service.edit_policy):
разбор ТЗ, наследование политики от родительского уровня, белый список при поиске.

Разбор проверяется на синтетических строках — без .xlsx и без проекта-конструктора,
чтобы тесты шли на сервере и в CI так же, как локально.
"""
from __future__ import annotations

import json

from services.infoboards_service.edit_policy import (
    EditPolicy,
    load_edit_policy,
    lookup_level_flag,
    parse_tz_rows,
    policy_from_dict,
)


EDITABLE = "Важно! ЦК подлежит редактированию пользователем"
LOCKED = "Важно! ЦК не подлежит редактированию пользователем"


def _notes(text: str) -> str:
    """Метка в ТЗ живёт внутри многострочного блока «Примечания:», а не отдельной ячейкой."""
    return f"Примечания:\n1 - возврат к предыдущему уровню.\n2 - подборка документов.\n{text}"


# Структура как в реальном ТЗ: корень, закрытая продуктовая ветка 1, открытая ветка 2.
TZ_ROWS = [
    ["Стартовый кабинет"],
    ['Блок "Материалы, рекомендованные экспертами"'],
    [_notes(EDITABLE)],
    ['Уровень 1 (ЦК "Материалы, рекомендованные экспертами для вас")'],
    [_notes(LOCKED)],
    ["Уровень 1.1 (Рекомендованные нормативные документы)"],
    [_notes(LOCKED)],
    ['Уровень 2 (ЦК "Мой кабинет")'],
    [_notes(EDITABLE)],
    ["Уровень 2.1. (Мои внутренние документы)"],
    [_notes(EDITABLE)],
]


def test_parse_tz_rows_reads_mask() -> None:
    """Корень и обе ветки разбираются с ожидаемыми флагами, предупреждений нет."""
    levels, warnings = parse_tz_rows(TZ_ROWS)
    flags = {lvl.code: lvl.editable for lvl in levels}
    assert flags == {"0": True, "1": False, "1.1": False, "2": True, "2.1": True}
    assert warnings == []


def test_parse_tz_rows_keeps_level_titles() -> None:
    """Название уровня берётся из скобок и схлопывается по пробелам."""
    levels, _ = parse_tz_rows(TZ_ROWS)
    titles = {lvl.code: lvl.title for lvl in levels}
    assert titles["2.1"] == "Мои внутренние документы"
    assert titles["0"] == "Стартовый кабинет"


def test_parse_tz_rows_warns_on_level_without_mark() -> None:
    """Уровень без метки не попадает в маску и отмечается предупреждением."""
    rows = [
        ["Уровень 1 (Без метки)"],
        ["Уровень 2 (С меткой)"],
        [_notes(EDITABLE)],
    ]
    levels, warnings = parse_tz_rows(rows)
    flags = {lvl.code: lvl.editable for lvl in levels}
    assert flags == {"2": True}
    assert any("уровень 1" in w for w in warnings)


def test_parse_tz_rows_warns_on_mark_before_any_level() -> None:
    """Метка до первого уровня — предупреждение, а не молчаливая привязка к чему попало."""
    levels, warnings = parse_tz_rows([[_notes(EDITABLE)], ["Уровень 1 (Раз)"], [_notes(LOCKED)]])
    assert {lvl.code: lvl.editable for lvl in levels} == {"1": False}
    assert any("до первого уровня" in w for w in warnings)


def test_parse_tz_rows_conflict_locks_level() -> None:
    """Противоречие внутри одного ТЗ разрешается в пользу запрета."""
    rows = [
        ["Уровень 2 (Мой кабинет)"],
        [_notes(EDITABLE)],
        ["Уровень 2 (Мой кабинет)"],
        [_notes(LOCKED)],
    ]
    levels, warnings = parse_tz_rows(rows)
    assert {lvl.code: lvl.editable for lvl in levels} == {"2": False}
    assert any("и как редактируемый" in w for w in warnings)


# --- Наследование политики размноженными бордами ---------------------------------------


def test_lookup_level_exact_match() -> None:
    assert lookup_level_flag({"2.1": True}, "2.1") == (True, "2.1")


def test_lookup_level_inherits_from_parent() -> None:
    """Конструктор размножает уровень 1.2.1 в борды 1.2.1.N — политика наследуется."""
    flags = {"1": False, "1.2.1": True}
    assert lookup_level_flag(flags, "1.2.1.16") == (True, "1.2.1")


def test_lookup_level_inherits_nearest_ancestor() -> None:
    """Берётся ближайший предок, а не самый верхний: ветка может переопределять родителя."""
    flags = {"1": True, "1.2": False}
    assert lookup_level_flag(flags, "1.2.3.4") == (False, "1.2")


def test_lookup_level_unknown_branch() -> None:
    """Нет ни уровня, ни предка — решения нет, борд останется на просмотр."""
    assert lookup_level_flag({"2": True}, "9.1") == (None, "")


# --- Поиск по справочнику ---------------------------------------------------------------


POLICY_DICT = {
    "hosts": {
        "cabinet-03.kodeks.expert": {
            "Кабинет СМК": {"tz": "ТЗ.xlsx", "root": "P017E", "boards": {"P017E": True, "P017F": False}},
        }
    }
}


def test_policy_lookup_is_whitelist() -> None:
    """Разрешено только явно помеченное; незнакомый борд — только просмотр."""
    policy = policy_from_dict(POLICY_DICT)
    assert policy.is_editable("https://cabinet-03.kodeks.expert", "P017E") is True
    assert policy.is_editable("https://cabinet-03.kodeks.expert", "P017F") is False
    assert policy.is_editable("https://cabinet-03.kodeks.expert", "P999Z") is False


def test_policy_ignores_scheme_and_trailing_slash() -> None:
    """base_url из reg_services приходит в разном виде — ключ берётся по хосту."""
    policy = policy_from_dict(POLICY_DICT)
    for base_url in (
        "https://cabinet-03.kodeks.expert",
        "https://cabinet-03.kodeks.expert/",
        "cabinet-03.kodeks.expert",
    ):
        assert policy.is_editable(base_url, "P017E") is True


def test_policy_unknown_host_grants_nothing() -> None:
    """Хост не в справочнике — прав на правку нет ни у кого."""
    policy = policy_from_dict(POLICY_DICT)
    assert policy.editable_boards("https://cabinet-07.kodeks.expert", ["P017E"]) == set()


def test_policy_editable_boards_filters_cascade() -> None:
    """Из каскада отбираются только редактируемые борды."""
    policy = policy_from_dict(POLICY_DICT)
    assert policy.editable_boards("https://cabinet-03.kodeks.expert", ["P017E", "P017F", "P100A"]) == {"P017E"}


def test_missing_policy_file_is_not_fatal(tmp_path) -> None:
    """Справочника нет — воркер не падает, просто никому не выдаёт правку."""
    policy = load_edit_policy(tmp_path / "нет-такого.json")
    assert policy.is_empty
    assert policy.editable_boards("https://cabinet-03.kodeks.expert", ["P017E"]) == set()


def test_broken_policy_file_is_not_fatal(tmp_path) -> None:
    """Битый JSON тоже не роняет обработку — маска считается пустой."""
    broken = tmp_path / "policy.json"
    broken.write_text("{ это не json", encoding="utf-8")
    assert load_edit_policy(broken).is_empty


def test_policy_roundtrip_from_file(tmp_path) -> None:
    """Справочник читается из файла ровно в том формате, что пишет генератор."""
    path = tmp_path / "policy.json"
    path.write_text(json.dumps(POLICY_DICT, ensure_ascii=False), encoding="utf-8")
    policy = load_edit_policy(path)
    assert policy.is_editable("https://cabinet-03.kodeks.expert", "P017E") is True


def test_empty_policy_is_empty() -> None:
    assert EditPolicy().is_empty
