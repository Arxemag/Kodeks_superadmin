"""
Сгенерировать справочник редактируемости кабинетов (cabinet_edit_policy.json) из ТЗ
и артефактов публикации конструктора.

Зачем: главному пользователю правка выдаётся не на весь каскад, а только на разрешённые
узлы. Что разрешено — записано в ТЗ («Важно! ЦК подлежит редактированию пользователем»),
а какой уровень какому борду соответствует — в артефакте публикации конструктора
(resolved_blueprint.json: source_path + board_map). Скрипт сшивает одно с другим.

Запускается РУКАМИ при публикации новых кабинетов; результат коммитится в репозиторий.
Воркеру нужен только готовый JSON — ни конструктор, ни ТЗ на сервере не требуются.

Запуск:
  python scripts/gen_edit_policy.py --constructor F:/Consructor_infoboards
  python scripts/gen_edit_policy.py --host cabinet-03.kodeks.expert --check
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from datetime import date
from pathlib import Path
from typing import Any

from services.infoboards_service.edit_policy import lookup_level_flag, parse_tz_rows

DEFAULT_CONSTRUCTOR = Path("F:/Consructor_infoboards")
DEFAULT_OUT = Path(__file__).resolve().parents[1] / "services" / "infoboards_service" / "cabinet_edit_policy.json"

# «ТЗ_ЦК СМК_23.08.2026 (1) (1).xlsx» и «ТЗ_ЦК СМК_23.08.2026.xlsx» — один и тот же ТЗ:
# копии из браузера отличаются только хвостом. Для сопоставления его срезаем.
_COPY_SUFFIX_RE = re.compile(r"(?:\s*\(\d+\))+(?=\.xlsx$)", re.I)


def _tz_key(name: str) -> str:
    """Имя файла ТЗ без хвоста копии и регистра — ключ для поиска в каталоге ТЗ."""
    return _COPY_SUFFIX_RE.sub("", Path(name).name).casefold()


def _load_xlsx_reader(constructor: Path):
    """
    Взять читалку XLSX из проекта-конструктора: она на stdlib и умеет файлы,
    которые openpyxl отдаёт пустыми. Свой парсер не пишем.
    """
    if not constructor.exists():
        raise SystemExit(
            f"Не найден проект-конструктор: {constructor}\n"
            f"Укажите путь через --constructor (нужен только для генерации справочника)."
        )
    sys.path.insert(0, str(constructor))
    try:
        from cabinet_constructor.xlsx_reader import read_xlsx  # type: ignore[import-not-found]
    except ImportError as e:
        raise SystemExit(f"Не импортируется cabinet_constructor.xlsx_reader из {constructor}: {e}") from e
    return read_xlsx


def _index_tz(tz_dir: Path) -> dict[str, Path]:
    """Каталог ТЗ: нормализованное имя файла -> путь."""
    return {_tz_key(p.name): p for p in sorted(tz_dir.glob("*.xlsx")) if not p.name.startswith("~$")}


def _levels_from_tz(read_xlsx, path: Path) -> tuple[dict[str, bool], list[str]]:
    """Маска «код уровня -> можно править» по одному ТЗ."""
    sheets = read_xlsx(path)
    filling = next((s for s in sheets if "аполнение" in s.name.lower()), None)
    if filling is None:
        return {}, [f"{path.name}: нет листа «Наполнение кабинета» (листы: {[s.name for s in sheets]})"]
    levels, warnings = parse_tz_rows(filling.rows)
    return {lvl.code: lvl.editable for lvl in levels}, [f"{path.name}: {w}" for w in warnings]


def _collect(constructor: Path, tz_dir: Path, only_host: str | None) -> tuple[dict[str, Any], list[str]]:
    """Обойти артефакты публикации и собрать справочник по хостам и кабинетам."""
    read_xlsx = _load_xlsx_reader(constructor)
    tz_index = _index_tz(tz_dir)
    if not tz_index:
        raise SystemExit(f"В каталоге ТЗ нет файлов .xlsx: {tz_dir}")

    publish_root = constructor / "runtime" / "publish"
    if not publish_root.exists():
        raise SystemExit(f"Нет артефактов публикации: {publish_root}")

    hosts: dict[str, dict[str, Any]] = {}
    warnings: list[str] = []
    tz_cache: dict[Path, tuple[dict[str, bool], list[str]]] = {}

    for blueprint_path in sorted(publish_root.rglob("resolved_blueprint.json")):
        host = blueprint_path.relative_to(publish_root).parts[0]
        if only_host and host.casefold() != only_host.casefold():
            continue
        try:
            data = json.loads(blueprint_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as e:
            warnings.append(f"{blueprint_path}: не читается ({e})")
            continue

        blueprint = data.get("blueprint") or {}
        board_map: dict[str, str] = data.get("board_map") or {}
        # В части артефактов имя кабинета записано с переводами строк («Кабинет\nВет.врач»):
        # без схлопывания один и тот же кабинет попадёт в справочник дважды.
        cabinet_name = " ".join(str(blueprint.get("cabinet_name") or "").split())
        source_path = str(blueprint.get("source_path") or "")
        if not board_map or not cabinet_name or not source_path:
            warnings.append(f"{blueprint_path}: неполный артефакт (нет board_map/cabinet_name/source_path)")
            continue

        tz_path = tz_index.get(_tz_key(source_path))
        if tz_path is None:
            warnings.append(f"{cabinet_name} ({host}): ТЗ {Path(source_path).name!r} не найден в {tz_dir}")
            continue

        if tz_path not in tz_cache:
            tz_cache[tz_path] = _levels_from_tz(read_xlsx, tz_path)
        level_flags, tz_warnings = tz_cache[tz_path]
        warnings.extend(tz_warnings)

        boards: dict[str, bool] = {}
        missing: list[str] = []
        inherited = 0
        for code, board_id in board_map.items():
            if not board_id:
                continue
            flag, matched = lookup_level_flag(level_flags, code)
            if flag is None:
                # Ни самого уровня, ни предка в ТЗ нет — по белому списку только просмотр.
                missing.append(code)
                boards[board_id] = False
                continue
            if matched != code:
                inherited += 1
            boards[board_id] = flag
        if missing:
            warnings.append(
                f"{cabinet_name} ({host}): нет метки в ТЗ для уровней {sorted(missing)[:8]} — только просмотр"
            )

        entry = {
            "tz": tz_path.name,
            "root": board_map.get("0", ""),
            "source": str(blueprint_path.parent.relative_to(publish_root)).replace("\\", "/"),
            "published_at": blueprint_path.stat().st_mtime,
            "inherited_levels": inherited,
            "boards": boards,
        }
        previous = hosts.setdefault(host, {}).get(cabinet_name)
        if previous is not None:
            # Один кабинет публиковали несколько раз — берём САМУЮ СВЕЖУЮ публикацию
            # по времени артефакта, а не последнюю в порядке обхода каталогов.
            if previous["published_at"] >= entry["published_at"]:
                continue
            if previous["boards"] != entry["boards"]:
                warnings.append(
                    f"{cabinet_name} ({host}): несколько публикаций с разной маской, "
                    f"берём свежую ({entry['source']})"
                )
        hosts.setdefault(host, {})[cabinet_name] = entry

    policy = {
        "_note": (
            "Маска редактируемости кабинетов. Источник — ТЗ (метка «Важно! ЦК подлежит "
            "редактированию пользователем»), связь уровня с board_id — из resolved_blueprint.json "
            "конструктора. Генерируется scripts/gen_edit_policy.py, правится только через него. "
            "Белый список: true = правка разрешена, всё остальное — только просмотр."
        ),
        "generated_at": date.today().isoformat(),
        "hosts": hosts,
    }
    return policy, warnings


def main() -> None:
    parser = argparse.ArgumentParser(description="Справочник редактируемости кабинетов из ТЗ")
    parser.add_argument("--constructor", type=Path, default=DEFAULT_CONSTRUCTOR, help="Путь к проекту-конструктору")
    parser.add_argument("--tz-dir", type=Path, default=None, help="Каталог с ТЗ (по умолчанию <constructor>/TZ)")
    parser.add_argument("--host", default=None, help="Собрать только по одному хосту каталога")
    parser.add_argument("--out", type=Path, default=DEFAULT_OUT, help="Куда записать JSON")
    parser.add_argument("--check", action="store_true", help="Только показать результат, не записывать файл")
    args = parser.parse_args()

    tz_dir = args.tz_dir or (args.constructor / "TZ")
    policy, warnings = _collect(args.constructor, tz_dir, args.host)

    hosts = policy["hosts"]
    total_boards = sum(len(c["boards"]) for cabs in hosts.values() for c in cabs.values())
    total_editable = sum(
        sum(1 for v in c["boards"].values() if v) for cabs in hosts.values() for c in cabs.values()
    )
    print(f"Хостов: {len(hosts)}, кабинетов: {sum(len(c) for c in hosts.values())}")
    for host, cabinets in sorted(hosts.items()):
        for name, entry in sorted(cabinets.items()):
            editable = sum(1 for v in entry["boards"].values() if v)
            print(f"  {host:<28} {name:<32} бордов={len(entry['boards']):>3} правка={editable:>3}  ({entry['tz']})")
    print(f"Итого бордов: {total_boards}, из них редактируемых: {total_editable}")

    if warnings:
        # Предупреждений бывают сотни (старые хосты, ТЗ не из текущего каталога) —
        # группируем по типу, иначе полезное тонет в списке.
        groups: dict[str, list[str]] = {}
        for w in warnings:
            if "не найден в" in w:
                key = "ТЗ не найден в каталоге ТЗ"
            elif "нет метки в ТЗ" in w:
                key = "уровни без метки редактируемости (остались на просмотр)"
            elif "без метки редактируемости" in w:
                key = "уровни ТЗ без метки"
            elif "неполный артефакт" in w:
                key = "неполные артефакты публикации"
            else:
                key = "прочее"
            groups.setdefault(key, []).append(w)
        print(f"\nПредупреждения ({len(warnings)}):")
        for key, items in sorted(groups.items(), key=lambda kv: -len(kv[1])):
            print(f"  [{len(items)}] {key}")
            for item in items[:3]:
                print(f"       - {item}")
            if len(items) > 3:
                print(f"       ... ещё {len(items) - 3}")

    if args.check:
        print("\n--check: файл не записан.")
        return
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(policy, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"\nЗаписано: {args.out}")


if __name__ == "__main__":
    main()
