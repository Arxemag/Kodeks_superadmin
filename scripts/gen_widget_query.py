"""
Сгенерировать GraphQL-запрос QUERY_BOARD_ONE из живой схемы каталога (интроспекция).

Зачем: когда в каталоге появляется новый тип виджета, перегенерировать блок `widgets { ... }`
для services/infoboards_service/cabinet_edit.py — чтобы чтение/round-trip работали на любом кабинете.

Логика: интроспекция -> для каждого члена union `Widget` берём поля, чьи имена есть в парном
типе `XxxWidgetConfig` (это редактируемый = round-trippable набор; вычисляемые поля так отсекаются).
Если Config-типа нет (KndCountersWidget, ControlWidget) — берём runtime-поля минус SKIP.

Запуск:
  python scripts/gen_widget_query.py --base-url https://cabinet-03.kodeks.expert/ --insecure
"""
from __future__ import annotations

import argparse
import asyncio

import httpx

from common.config import get_settings
from common.http import _default_headers
from common.logger import get_logger
from services.infoboards_service.cabinet_edit import _graphql

logger = get_logger("gen_widget_query")

# Вычисляемые/«живые» поля — не читаем (их нет во входном ConfigInput.widgets).
SKIP = {"documents", "getEvents", "getOffers", "getUrlByQuickFilters", "countDocs", "widgetData", "getDocs", "docs"}
SCALARS = {"SCALAR", "ENUM"}

INTROSPECT = """
query I {
  __schema { types {
    kind name
    possibleTypes { name }
    fields(includeDeprecated: true) { name type { kind name ofType { kind name ofType { kind name ofType { kind name } } } } }
  } }
}
"""


def _leaf(t):
    while t:
        if t.get("name"):
            return t.get("kind"), t["name"]
        t = t.get("ofType")
    return None, None


def build(schema: dict) -> str:
    def fields_of(tname):
        return schema.get(tname, {}).get("fields") or []

    def expand(tname, depth, seen):
        t = schema.get(tname, {})
        if t.get("kind") in ("UNION", "INTERFACE"):
            parts = ["__typename"]
            for pt in t.get("possibleTypes", []):
                if pt not in seen:
                    parts.append(f"... on {pt} {{ {expand(pt, depth - 1, seen | {pt})} }}")
            return " ".join(parts)
        if depth <= 0:
            return "__typename"
        parts = []
        for f in fields_of(tname):
            if f["n"] in SKIP:
                continue
            fk, fn = f["t"]
            if fk in SCALARS:
                parts.append(f["n"])
            elif fk in ("OBJECT", "INTERFACE", "UNION") and fn and fn not in seen:
                sub = expand(fn, depth - 1, seen | {fn})
                if sub:
                    parts.append(f"{f['n']} {{ {sub} }}")
        return " ".join(parts) or "__typename"

    def fragment(wt):
        runtime = {f["n"]: f["t"] for f in fields_of(wt)}
        cfg = wt + "Config"
        if schema.get(cfg, {}).get("fields"):
            cfg_names = {f["n"] for f in fields_of(cfg)}
            allowed = [n for n in runtime if n in cfg_names and n not in SKIP]
        else:
            allowed = [n for n in runtime if n not in SKIP]
        parts = []
        for n in allowed:
            fk, fn = runtime[n]
            if fk in SCALARS:
                parts.append(n)
            elif fk in ("OBJECT", "INTERFACE", "UNION") and fn:
                sub = expand(fn, 4, {wt, fn})
                if sub:
                    parts.append(f"{n} {{ {sub} }}")
        return f"        ... on {wt} {{ {' '.join(parts) or '__typename'} }}"

    members = schema["Widget"]["possibleTypes"]
    frags = "\n".join(fragment(wt) for wt in members)
    return (
        "QUERY_BOARD_ONE = \"\"\"\n"
        "query QueryBoardOne($id: String!) {\n"
        "  board {\n    one(id: $id) {\n      id\n      author\n      title\n"
        "      searchString { header palette { colorWidget colorText colorIcon colorInput colorButton colorButtonText } }\n"
        "      widgets {\n        __typename\n" + frags + "\n      }\n    }\n  }\n}\n\"\"\"\n"
    )


async def _run() -> None:
    p = argparse.ArgumentParser(description="Генерация QUERY_BOARD_ONE из схемы каталога")
    p.add_argument("--base-url", required=True, help="URL каталога, напр. https://cabinet-03.kodeks.expert/")
    p.add_argument("--insecure", action="store_true", help="Не проверять TLS (внутренние хосты по VPN)")
    args = p.parse_args()
    base = args.base_url.rstrip("/")
    s = get_settings()
    async with httpx.AsyncClient(timeout=30, follow_redirects=True, headers=_default_headers(), verify=not args.insecure) as client:
        await client.post(
            f"{base}/users/login.asp",
            data={"user": s.ADMIN_LOGIN, "pass": s.ADMIN_PASSWORD, "path": "/admin"},
            headers={"Origin": base, "Referer": f"{base}/"},
        )
        cookies = dict(client.cookies)
        data = await _graphql(base, client, cookies, INTROSPECT, {})
        schema = {t["name"]: t for t in data["__schema"]["types"] if t.get("name") and not t["name"].startswith("__")}
        # нормализовать поля к виду {"n": имя, "t": (kind, name)} и possibleTypes к списку имён
        for t in schema.values():
            t["fields"] = [{"n": f["name"], "t": _leaf(f["type"])} for f in (t.get("fields") or [])]
            if t.get("possibleTypes"):
                t["possibleTypes"] = [x["name"] for x in t["possibleTypes"]]
        print(build(schema))


def main() -> None:
    asyncio.run(_run())


if __name__ == "__main__":
    main()
