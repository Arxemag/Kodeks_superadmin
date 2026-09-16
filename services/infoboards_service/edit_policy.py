"""
Маска редактируемости кабинетов: какие борды каскада разрешено править пользователю.

Источник истины — ТЗ на цифровой кабинет (.xlsx). В листе «Наполнение кабинета» каждый
уровень сопровождается примечанием со строкой «Важно! ЦК подлежит редактированию
пользователем» либо «... не подлежит ...». Часть каскада (продуктовый контент,
рекомендованный экспертами) закрыта для правки всем, часть (внутренние документы
клиента) — открыта.

Связь «уровень ТЗ -> board_id каталога» даёт артефакт публикации конструктора
(resolved_blueprint.json: source_path + board_map). Сопоставлять по названию нельзя:
в каталоге борд называется «СМК\\n\\nМои внутренние документы», а уровень в ТЗ —
«Мои внутренние документы», плюс одно и то же название уровня в разных ТЗ может иметь
разный флаг. Поэтому связываем по коду уровня, а не по тексту.

Справочник cabinet_edit_policy.json генерируется руками (scripts/gen_edit_policy.py)
при публикации новых кабинетов и коммитится в репозиторий. Здесь — только чтение и поиск.

Правило белого списка: правка разрешена только бордам, явно помеченным в ТЗ и однозначно
сопоставленным. Всё остальное — только просмотр. Кабинеты в каталоге по умолчанию закрыты,
поэтому ошибка в сторону «не выдали прав» безопасна, а в обратную — нет.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from common.logger import get_logger


logger = get_logger("infoboards.edit_policy")

# Справочник лежит рядом с модулем, как ecology_questionnaire_map.json.
_POLICY_PATH = Path(__file__).with_name("cabinet_edit_policy.json")

# Заголовок уровня: «Уровень 2.1. (Мои внутренние документы)». Точка после номера
# необязательна, скобки в ТЗ есть всегда, но допускаем и запись без них.
_LEVEL_RE = re.compile(r"^Уровень\s+([\d.]+?)\.?\s*(?:\((.*)\))?\s*$", re.S)
# Корневой ЦК в ТЗ идёт без номера уровня, отдельной строкой.
_ROOT_RE = re.compile(r"^Стартовый\s+кабинет", re.I)
_ROOT_CODE = "0"
# Метка редактируемости внутри блока «Примечания:». Группа 1 непустая => «не подлежит».
_MARK_RE = re.compile(r"Важно!\s*ЦК\s*(не\s*)?подлежит\s+редактированию", re.I)


def _norm(value: str | None) -> str:
    """Схлопнуть пробелы и переводы строк; для сравнения текста из ячеек ТЗ."""
    return " ".join((value or "").split())


def _host(base_url: str) -> str:
    """Хост каталога из base_url: ключ справочника не зависит от схемы и хвостового слэша."""
    raw = (base_url or "").strip()
    if not raw:
        return ""
    parsed = urlparse(raw if "//" in raw else f"//{raw}")
    return (parsed.netloc or parsed.path).strip("/").casefold()


@dataclass(frozen=True)
class LevelPolicy:
    """Уровень ТЗ: код каскада, название и разрешена ли правка пользователем."""

    code: str
    title: str
    editable: bool


def parse_tz_rows(rows: list[list[str]]) -> tuple[list[LevelPolicy], list[str]]:
    """
    Разобрать строки листа «Наполнение кабинета» в маску редактируемости по уровням.

    Метка идёт ПОСЛЕ заголовка уровня (в блоке «Примечания:» этого уровня), поэтому
    разбор последовательный: запомнили текущий уровень — встретили метку — записали.

    Возвращает (уровни, предупреждения). Предупреждения — уровни без метки и метки без
    уровня; такие уровни в результат не попадают и останутся только на просмотр.
    """
    levels: dict[str, LevelPolicy] = {}
    warnings: list[str] = []
    current: tuple[str, str] | None = None
    marked = False

    def _close_current() -> None:
        if current is not None and not marked:
            warnings.append(f"уровень {current[0]} ({current[1][:60]}) без метки редактируемости")

    for row in rows:
        for raw in row:
            if not raw:
                continue
            value = _norm(raw)
            if not value:
                continue

            if _ROOT_RE.match(value):
                _close_current()
                current, marked = (_ROOT_CODE, "Стартовый кабинет"), False
                continue

            if value.lower().startswith("уровень"):
                match = _LEVEL_RE.match(value)
                if match:
                    _close_current()
                    current = (match.group(1).rstrip("."), _norm(match.group(2)))
                    marked = False
                    continue

            mark = _MARK_RE.search(raw)
            if not mark:
                continue
            if current is None:
                warnings.append("метка редактируемости встретилась до первого уровня")
                continue
            code, title = current
            editable = mark.group(1) is None
            previous = levels.get(code)
            if previous is not None and previous.editable != editable:
                # Противоречие внутри одного ТЗ — не гадаем, оставляем только просмотр.
                warnings.append(f"уровень {code} помечен и как редактируемый, и как нет")
                levels[code] = LevelPolicy(code=code, title=title, editable=False)
            else:
                levels[code] = LevelPolicy(code=code, title=title, editable=editable)
            marked = True

    _close_current()
    return list(levels.values()), warnings


def lookup_level_flag(level_flags: dict[str, bool], code: str) -> tuple[bool | None, str]:
    """
    Флаг редактируемости для кода борда, с наследованием от родительского уровня ТЗ.

    Конструктор размножает один шаблонный уровень ТЗ в несколько живых бордов, добавляя
    к коду ещё одну цифру: уровень 1.2.1 -> борды 1.2.1.1 … 1.2.1.16. Такого кода в ТЗ нет,
    поэтому поднимаемся к ближайшему известному предку — политика ветки распространяется
    на её потомков. Не нашли ни одного предка: (None, ""), борд остаётся только на просмотр.

    Возвращает (флаг, код уровня, от которого он взят).
    """
    parts = code.split(".")
    while parts:
        key = ".".join(parts)
        if key in level_flags:
            return level_flags[key], key
        parts.pop()
    return None, ""


@dataclass
class EditPolicy:
    """Справочник редактируемости: хост каталога -> board_id -> можно ли править."""

    by_host: dict[str, dict[str, bool]] = field(default_factory=dict)

    def is_editable(self, base_url: str, board_id: str) -> bool:
        """True только если борд явно помечен редактируемым в справочнике."""
        return self.by_host.get(_host(base_url), {}).get(board_id, False)

    def editable_boards(self, base_url: str, board_ids: list[str] | set[str]) -> set[str]:
        """Подмножество board_ids, которым разрешена правка. Пусто, если справочника нет."""
        known = self.by_host.get(_host(base_url), {})
        return {b for b in board_ids if known.get(b, False)}

    @property
    def is_empty(self) -> bool:
        return not any(self.by_host.values())


def policy_from_dict(data: dict[str, Any]) -> EditPolicy:
    """Собрать EditPolicy из разобранного JSON справочника."""
    by_host: dict[str, dict[str, bool]] = {}
    for host, cabinets in (data.get("hosts") or {}).items():
        boards: dict[str, bool] = {}
        for cabinet in (cabinets or {}).values():
            for board_id, editable in (cabinet.get("boards") or {}).items():
                # Один борд не может принадлежать двум каскадам; если вдруг —
                # запрет побеждает разрешение.
                boards[board_id] = bool(editable) and boards.get(board_id, True)
        by_host[_host(host)] = boards
    return EditPolicy(by_host=by_host)


def load_edit_policy(path: str | Path | None = None) -> EditPolicy:
    """
    Прочитать справочник. Отсутствие файла — не ошибка: возвращается пустая маска,
    и тогда правка не выдаётся никому (каталог по умолчанию закрыт, это безопасно).
    """
    target = Path(path or _POLICY_PATH)
    if not target.exists():
        logger.warning(
            "справочник редактируемости не найден (%s) — права на правку не выдаются", target,
        )
        return EditPolicy()
    try:
        data = json.loads(target.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as e:
        logger.error("справочник редактируемости не читается (%s): %r", target, e)
        return EditPolicy()
    policy = policy_from_dict(data)
    logger.info(
        "справочник редактируемости: хостов=%s, редактируемых бордов=%s",
        len(policy.by_host),
        sum(sum(1 for v in b.values() if v) for b in policy.by_host.values()),
    )
    return policy
