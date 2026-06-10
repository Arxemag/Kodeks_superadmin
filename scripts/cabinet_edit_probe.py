"""
Пробник инструмента редактирования кабинета (без Kafka).

Логинится в каталог (login.asp с ADMIN_LOGIN/ADMIN_PASSWORD из .env), читает виджеты борда
и применяет editConfig. Пишет подробный лог В ФАЙЛ (--log-file) — его потом читает разработчик.

Источник base_url (взаимоисключающие):
  --reg 465302         base_url берётся из reg_services (как в проде); нужен --board-id или --link
  --base-url URL       прямой URL каталога; нужен --board-id или --link
  --link "...infoboard=PXXXX"  base_url и board-id берутся из ссылки

Примеры:
  # БЕЗОПАСНЫЙ полный автотест: читает -> бэкап -> round-trip -> удаляет всё -> проверяет -> восстанавливает
  python scripts/cabinet_edit_probe.py --reg 465302 --board-id P000M --self-test

  # только показать виджеты (read-only)
  python scripts/cabinet_edit_probe.py --reg 465302 --board-id P000M --list

  # удалить все виджеты / оставить по заголовкам / восстановить из файла
  python scripts/cabinet_edit_probe.py --base-url https://cabinet-03.kodeks.expert/ --board-id P000M --delete-all
  python scripts/cabinet_edit_probe.py --reg 465302 --board-id P000M --keep-headers "Трест;СМК"
  python scripts/cabinet_edit_probe.py --reg 465302 --board-id P000M --set-from backup.json
"""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
from pathlib import Path

import httpx

from common.config import get_settings
from common.exceptions import AuthError
from common.http import _default_headers
from common.logger import JsonFormatter, TraceIdFilter, get_logger, set_trace_id
from services.infoboards_service.cabinet_acl import get_boards
from services.infoboards_service.cabinet_edit import (
    BoardConfig,
    correct_cabinet,
    fetch_board,
    parse_infoboard_id,
    prune_widgets,
    set_widgets,
    widgets_to_input,
)


logger = get_logger("cabinet_edit_probe")

DEFAULT_LOG = str(Path(__file__).resolve().parent / "_cabinet_test.log")
DEFAULT_BACKUP = str(Path(__file__).resolve().parent / "_cabinet_backup.json")


def _parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Редактирование виджетов кабинета (тест)")
    # Хост (base_url): --reg (через reg_services) | --base-url | хост из --link.
    # board-id: --board-id | из --link. --reg/--base-url можно комбинировать с --link (host=reg, id=link).
    p.add_argument("--reg", help="reg из reg_services — даёт base_url (хост)")
    p.add_argument("--base-url", help="URL каталога напрямую (хост)")
    p.add_argument("--link", help="Ссылка на кабинет: даёт board-id (и хост, если нет --reg/--base-url)")
    p.add_argument("--board-id", help="id борда (если не задан --link)")
    p.add_argument("--correct", metavar="FILE",
                   help="JSON-payload correct_cabinet (link + questions[]) — почистить под-кабинеты по опроснику")
    p.add_argument("--list-boards", action="store_true",
                   help="Показать все кабинеты (board.many) на хосте и выйти (read-only)")
    p.add_argument("--self-test", action="store_true",
                   help="Безопасный автотест: read -> backup -> round-trip -> delete-all -> restore")
    p.add_argument("--list", action="store_true", help="Показать виджеты и выйти (read-only)")
    p.add_argument("--backup", metavar="FILE", help="Сохранить текущие виджеты (input) в файл")
    p.add_argument("--delete-all", action="store_true", help="Удалить ВСЕ виджеты (editConfig widgets=[])")
    p.add_argument("--keep-headers", metavar="H1;H2", help="Оставить только виджеты с этими header")
    p.add_argument("--remove-headers", metavar="H1;H2", help="Удалить виджеты с этими header")
    p.add_argument("--remove-uuids", metavar="U1;U2", help="Удалить виджеты с этими widgetUuid")
    p.add_argument("--no-keep-special", action="store_true", help="Не сохранять системные виджеты (isSpecial)")
    p.add_argument("--set-from", metavar="FILE", help="Заменить набор виджетов содержимым JSON-файла")
    p.add_argument("--dry-run", action="store_true", help="Не отправлять editConfig — только показать payload")
    p.add_argument("--insecure", action="store_true", help="Не проверять TLS-сертификат (внутренние хосты по VPN)")
    p.add_argument("--log-file", default=DEFAULT_LOG, help=f"Файл лога (по умолчанию {DEFAULT_LOG})")
    return p.parse_args()


def _setup_file_logging(path: str) -> None:
    """Файловый handler (DEBUG, JSON) на логгеры пробника и cabinet_edit — чтобы прочитать лог потом."""
    fh = logging.FileHandler(path, mode="w", encoding="utf-8")
    fh.setLevel(logging.DEBUG)
    fh.setFormatter(JsonFormatter())
    fh.addFilter(TraceIdFilter())
    for name in ("cabinet_edit_probe", "infoboards.cabinet_edit"):
        lg = logging.getLogger(name)
        lg.setLevel(logging.DEBUG)
        lg.addHandler(fh)


def _split(arg: str | None) -> set[str]:
    return {x.strip() for x in (arg or "").split(";") if x.strip()}


def _sig(widgets: list[dict]) -> list[tuple[str, str]]:
    """Подпись набора виджетов для сравнения: (тип, header), отсортировано."""
    return sorted((str(w.get("__typename")), str(w.get("header") or "")) for w in widgets)


async def _resolve_base_url_by_reg(reg: str) -> str:
    """SELECT base_url FROM reg_services WHERE reg_number = reg (через settings.DB_URL)."""
    from sqlalchemy import text
    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

    s = get_settings()
    tbl = s.DB_TABLE_REG_SERVICES
    logger.info("resolve base_url by reg=%r via %s", reg, tbl)
    engine = create_async_engine(s.DB_URL, pool_size=1)
    try:
        sm = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
        async with sm() as session:
            res = await session.execute(
                text(f"SELECT base_url FROM {tbl} WHERE reg_number = :r"), {"r": reg}
            )
            val = res.scalar_one_or_none()
    finally:
        await engine.dispose()
    if not val:
        raise AuthError(code="REG_NOT_FOUND", message=f"reg {reg!r} не найден в {tbl}")
    return str(val).rstrip("/")


async def _admin_login(base_url: str, client: httpx.AsyncClient) -> dict[str, str]:
    """Прямой логин админа в каталог; вернуть cookies сессии."""
    s = get_settings()
    url = f"{base_url.rstrip('/')}/users/login.asp"
    logger.info("login POST %s user=%r", url, s.ADMIN_LOGIN)
    resp = await client.post(
        url,
        data={"user": s.ADMIN_LOGIN, "pass": s.ADMIN_PASSWORD, "path": "/admin"},
        headers={"Origin": base_url.rstrip("/"), "Referer": f"{base_url.rstrip('/')}/"},
    )
    logger.info("login -> HTTP %s", resp.status_code)
    if resp.status_code >= 400:
        raise AuthError(code="ADMIN_AUTH_FAILED", message=f"login status={resp.status_code} body={(resp.text or '')[:200]!r}")
    cookies = dict(client.cookies)
    logger.info("login ok, cookie_keys=%s", sorted(cookies.keys()))
    return cookies


def _log_widgets(prefix: str, board: BoardConfig) -> None:
    logger.info("%s: борд id=%r title=%r виджетов=%s", prefix, board.id, board.title, len(board.widgets))
    for i, w in enumerate(board.widgets, 1):
        logger.info(
            "  %s.%d type=%r header=%r isSpecial=%s uuid=%r",
            prefix, i, w.get("__typename"), w.get("header"), w.get("isSpecial"), w.get("widgetUuid"),
        )


async def _self_test(base_url: str, client: httpx.AsyncClient, cookies: dict[str, str], board_id: str) -> None:
    """Безопасный полный цикл: read -> backup -> round-trip -> (если ок) delete -> restore. Итог в лог."""
    logger.info("=== SELF-TEST START board_id=%r ===", board_id)
    board = await fetch_board(base_url, client, cookies, board_id)
    _log_widgets("ORIG", board)
    orig_sig = _sig(board.widgets)
    backup_inputs = widgets_to_input(board.widgets)
    Path(DEFAULT_BACKUP).write_text(json.dumps(backup_inputs, ensure_ascii=False, indent=2), encoding="utf-8")
    logger.info("backup -> %s (%d виджет(ов))", DEFAULT_BACKUP, len(backup_inputs))

    # 1) Round-trip: переотправляем те же виджеты, проверяем, что борд не изменился
    logger.info("--- STEP 1: round-trip (re-apply current widgets) ---")
    await set_widgets(base_url, client, cookies, board, backup_inputs)
    b2 = await fetch_board(base_url, client, cookies, board_id)
    _log_widgets("AFTER-ROUNDTRIP", b2)
    roundtrip_ok = _sig(b2.widgets) == orig_sig
    logger.info("round-trip preserved widgets: %s", roundtrip_ok)

    if not roundtrip_ok:
        logger.error(
            "ROUND-TRIP FAILED: набор виджетов изменился (было %s, стало %s). "
            "Удаление НЕ выполняю. Пробую восстановить из бэкапа.",
            orig_sig, _sig(b2.widgets),
        )
        await set_widgets(base_url, client, cookies, board, backup_inputs)
        logger.info("=== SELF-TEST RESULT: FAIL (round-trip), бэкап в %s ===", DEFAULT_BACKUP)
        return

    # 2) Delete-all
    logger.info("--- STEP 2: delete-all (widgets=[]) ---")
    await set_widgets(base_url, client, cookies, board, [])
    b3 = await fetch_board(base_url, client, cookies, board_id)
    _log_widgets("AFTER-DELETE", b3)
    delete_ok = len(b3.widgets) == 0
    logger.info("delete-all -> виджетов осталось %s (ожидаем 0): %s", len(b3.widgets), delete_ok)

    # 3) Restore
    logger.info("--- STEP 3: restore from backup ---")
    await set_widgets(base_url, client, cookies, board, backup_inputs)
    b4 = await fetch_board(base_url, client, cookies, board_id)
    _log_widgets("AFTER-RESTORE", b4)
    restore_ok = _sig(b4.widgets) == orig_sig
    logger.info("restore -> совпадает с исходным: %s", restore_ok)

    overall = roundtrip_ok and delete_ok and restore_ok
    logger.info(
        "=== SELF-TEST RESULT: %s (round-trip=%s, delete=%s, restore=%s) ===",
        "PASS" if overall else "FAIL", roundtrip_ok, delete_ok, restore_ok,
    )


async def _run() -> None:
    args = _parse_args()
    _setup_file_logging(args.log_file)
    set_trace_id("cabinet-edit-test")
    logger.info("log file: %s", args.log_file)

    # board-id: из --board-id, иначе из --link (для --list-boards не обязателен)
    board_id = args.board_id or (parse_infoboard_id(args.link) if args.link else None)

    # host (base_url): приоритет --reg, затем --base-url, затем хост из --link
    if args.reg:
        base_url = await _resolve_base_url_by_reg(args.reg)
        host_from = "reg"
    elif args.base_url:
        base_url = args.base_url.rstrip("/")
        host_from = "base-url"
    elif args.link:
        base_url = args.link.split("/docs/")[0].split("/infoboard/")[0].rstrip("/")
        host_from = "link"
    else:
        raise SystemExit("Нужен хост: задайте --reg, --base-url или --link")
    logger.info("base_url=%r board_id=%r (host_from=%s)", base_url, board_id, host_from)
    if not board_id and not args.list_boards and not args.correct:
        raise SystemExit("Нужен board-id: задайте --board-id или --link")

    s = get_settings()
    async with httpx.AsyncClient(
        timeout=s.HTTP_TIMEOUT, follow_redirects=True, headers=_default_headers(), verify=not args.insecure
    ) as client:
        cookies = await _admin_login(base_url, client)

        if args.list_boards:
            boards = await get_boards(base_url, client, cookies)
            logger.info("board.many -> %s кабинет(ов): %s", len(boards), boards)
            print(f"Кабинеты на {base_url} ({len(boards)}):")
            for title, bid in sorted(boards.items(), key=lambda x: x[0].lower()):
                print(f"  {bid}: {title}")
            print(f"Лог: {args.log_file}")
            return

        if args.correct:
            payload = json.loads(Path(args.correct).read_text(encoding="utf-8"))
            parent_link = payload.get("link") or args.link
            questions = payload.get("questions") or []
            plans = await correct_cabinet(
                base_url, client, cookies, parent_link, questions, dry_run=args.dry_run
            )
            print(f"\n=== correct_cabinet {'(DRY-RUN)' if args.dry_run else '(APPLIED)'} ===")
            for pl in plans:
                print(f"\n[{pl.action}] Q={pl.question[:80]!r}  sub={pl.sub_id}")
                if pl.action == "prune":
                    print(f"    ОСТАВИТЬ ({len(pl.keep_headers)}): {pl.keep_headers}")
                    print(f"    УДАЛИТЬ  ({len(pl.delete_headers)}): {pl.delete_headers}")
            print(f"\nЛог: {args.log_file}")
            return

        if args.self_test:
            await _self_test(base_url, client, cookies, board_id)
            print(f"self-test завершён, лог: {args.log_file}")
            return

        board = await fetch_board(base_url, client, cookies, board_id)
        _log_widgets("CURRENT", board)

        if args.backup:
            Path(args.backup).write_text(
                json.dumps(widgets_to_input(board.widgets), ensure_ascii=False, indent=2), encoding="utf-8"
            )
            logger.info("backup -> %s", args.backup)

        if args.list:
            print(f"Готово (read-only). Лог: {args.log_file}")
            return

        if args.set_from:
            new_inputs = json.loads(Path(args.set_from).read_text(encoding="utf-8"))
            if not isinstance(new_inputs, list):
                raise SystemExit("--set-from: файл должен содержать JSON-массив виджетов")
            action = f"set-from {args.set_from} ({len(new_inputs)})"
        elif args.delete_all:
            new_inputs, action = [], "delete-all"
        elif args.keep_headers or args.remove_headers or args.remove_uuids:
            kept = prune_widgets(
                board.widgets,
                keep_headers=_split(args.keep_headers) or None,
                remove_headers=_split(args.remove_headers) or None,
                remove_uuids=_split(args.remove_uuids) or None,
                keep_special=not args.no_keep_special,
            )
            new_inputs, action = widgets_to_input(kept), f"prune -> {len(kept)}"
        else:
            print("Действие не задано (--self-test/--list/--delete-all/--keep-headers/--set-from).")
            return

        logger.info("action=%s payload_widgets=%s", action, json.dumps(new_inputs, ensure_ascii=False)[:2000])
        if args.dry_run:
            logger.info("dry-run: editConfig НЕ отправлен")
            print(f"dry-run. Лог: {args.log_file}")
            return

        await set_widgets(base_url, client, cookies, board, new_inputs)
        b2 = await fetch_board(base_url, client, cookies, board_id)
        _log_widgets("AFTER", b2)
        print(f"Готово ({action}). Лог: {args.log_file}")


def main() -> None:
    asyncio.run(_run())


if __name__ == "__main__":
    main()
