"""
Инструмент редактирования виджетов кабинета (инфоборда) через GraphQL editConfig.

Кабинет предсоздан со всеми виджетами; задача «уточнения» — оставить только нужные.
Примитив подтверждён реальной капчей Fiddler:

  - чтение:  POST {base}/infoboard/graphql?context=docs   query QueryBoardOne(id)
  - запись:  POST {base}/infoboard/graphql?context=docs   mutation EditConfigWidgets(ConfigInput)

ВАЖНО: `ConfigInput.widgets` — это JSON-СТРОКА массива виджетов (не вложенный массив),
и editConfig делает ПОЛНУЮ ЗАМЕНУ набора виджетов борда. Поэтому:
  - удалить один виджет  = отправить editConfig со списком БЕЗ него;
  - удалить все          = widgets = "[]";
  - оставить подмножество = отправить только нужные виджеты.

Модуль не зависит от Kafka и БД: ему нужны base_url, board_id и cookies (админ-сессия каталога).
"""
from __future__ import annotations

import json
import re
from collections import deque
from dataclasses import dataclass, field
from json import JSONDecodeError
from pathlib import Path
from typing import Any

import httpx

from common.exceptions import NetworkError, ParseError
from common.logger import get_logger


logger = get_logger("infoboards.cabinet_edit")

GRAPHQL_PATH = "/infoboard/graphql?context=docs"

# Поля, которые каталог ОТДАЁТ в board.one(widgets), но НЕ принимает обратно
# в ConfigInput.widgets (вычисляемые / только на чтение). Срезаем перед editConfig.
_COMPUTED_WIDGET_FIELDS = frozenset(
    {
        "__typename",
        "documents",
        "getEvents",
        "getOffers",
        "getUrlByQuickFilters",
        "countDocs",
        "widgetData",
    }
)

# Чтение конфигурации борда. Набор фрагментов покрывает типы виджетов, встреченные
# в капче; при необходимости расширяется (новый `... on XxxWidget { ... }`).
QUERY_BOARD_ONE = """
query QueryBoardOne($id: String!) {
  board {
    one(id: $id) {
      id
      author
      title
      hiddenOnStartPage
      searchString {
        header
        palette {
          colorWidget
          colorText
          colorIcon
          colorInput
          colorButton
          colorButtonText
        }
      }
      widgets {
        __typename
        ... on DocListWidget { widgetUuid position isSpecial header description height counter limit palette { colorWidget colorText colorLink } }
        ... on LinksWidget { widgetUuid position isSpecial header height description isTitleLink titleLink { href about } isButtonCreateForm links { href title about notForChange } palette { colorWidget colorText colorLink } }
        ... on PluginDocListWidget { widgetUuid position isSpecial header description height counter listId { limit serviceId listId } limit palette { colorWidget colorText colorLink } }
        ... on ClassifierWidget { widgetUuid isSpecial position header description height typeClassifier { typeName id attrTab } palette { colorWidget colorText colorLink } }
        ... on KndCountersWidget { widgetUuid isSpecial position header description height isDevelop isDiscuss isApprove isPublish isShowDiagram isButtonNewProject palette { colorWidget colorText colorLink colorCells colorButton colorPie { colorLogo colorDevelop colorDiscuss colorApprove colorAprrove colorPublish colorTotal } } }
        ... on ControlWidget { widgetUuid isSpecial position header description isShowDiagram isShowLink palette { colorWidget colorCells colorText colorLink colorPie { colorTotal colorCheck } } }
        ... on ListFromDocWidget { widgetUuid position isSpecial header description height counter list { limit doc holder form sort } limit palette { colorWidget colorText colorLink } }
        ... on DocAreaWidget { widgetUuid position isSpecial header description height counter area { limit area conditions { attr values mode } } palette { colorWidget colorText colorLink } }
        ... on OndWidget { widgetUuid isSpecial position header height description palette { colorCells colorWidget colorText colorButton colorPie { colorCreatedByMe colorForMyReview colorForMyExpertise colorExpertiseRequired colorExpertiseCarriedOut colorTotal colorLogo } } isShowDiagram isCreatedByMe isForMyReview isForMyExpertise isExpertiseRequired isExpertiseCarriedOut isButtonCreateDiscussion isOndWidget }
        ... on OffersWidget { widgetUuid position isSpecial header description height isALL isCREATED isONTIME isINDISCUSSION isDECLINED isShowDiagram isButtonCreateOffer isOffersWidget palette { colorCells colorWidget colorText colorButton colorPie { colorALL colorCREATED colorONTIME colorINDISCUSSION colorDECLINED colorLogo } } }
        ... on EventsWidget { widgetUuid isSpecial position header height description palette { colorWidget colorText colorButton } isShowDiagram isALL isAPPROVED isCLOSEDWITHSTAGEVIOLATIONS isCLOSEDOVERDUE isVIOLATION isCOMPLETED isOVERDUE isCREATED isPLANNED isONTIME isButtonNewProject isEventsWidget isCreated isPlanned isOnTime isOverdue isCompleted isViolation isClosedOverdue isClosedWithStageViolations isTotal }
      }
    }
  }
}
"""

# Запись конфигурации. Тело ответа выбираем минимальным — нужен факт успеха и id.
MUTATION_EDIT_CONFIG = """
mutation EditConfigWidgets($ConfigInput: ConfigInput) {
  editConfig(input: $ConfigInput) {
    id
    title
  }
}
"""


@dataclass
class BoardConfig:
    """Состояние борда, достаточное для round-trip через editConfig."""

    id: str
    author: str
    title: str
    search_string: dict[str, Any] | None
    widgets: list[dict[str, Any]] = field(default_factory=list)
    # Видимость на стартовой странице ТЭ. editConfig — полная замена конфига, поэтому
    # флаг нужно читать и возвращать обратно, иначе под-кабинеты всплывут на главной.
    hidden_on_start_page: bool | None = None


def parse_infoboard_id(link: str) -> str:
    """Достаёт id инфоборда (кабинета) из ссылки вида .../docs/?nd=...&infoboard=P000M."""
    m = re.search(r"[?&]infoboard=([^&#\s]+)", link)
    if not m:
        raise ParseError(
            code="CABINET_LINK_NO_INFOBOARD",
            message=f"в ссылке нет параметра infoboard: {link!r}",
        )
    return m.group(1).strip()


def _op_signature(query: str) -> str:
    """Короткая подпись операции для логов, напр. 'mutation EditConfigWidgets'."""
    m = re.search(r"(mutation|query)\s+(\w+)", query)
    return f"{m.group(1)} {m.group(2)}" if m else "graphql"


async def _graphql(
    base_url: str,
    client: httpx.AsyncClient,
    cookies: dict[str, str],
    query: str,
    variables: dict[str, Any],
) -> dict[str, Any]:
    """Выполнить GraphQL-запрос к каталогу; вернуть data. При HTTP-ошибке/errors — исключение."""
    base = base_url.rstrip("/")
    url = f"{base}{GRAPHQL_PATH}"
    op = _op_signature(query)
    logger.info("GraphQL POST %s op=%r cookie_keys=%s", url, op, sorted(cookies.keys()))
    logger.debug("GraphQL request variables: %s", json.dumps(variables, ensure_ascii=False)[:2000])
    try:
        resp = await client.post(
            url,
            json={"query": query, "variables": variables},
            cookies=cookies,
            headers={
                "Accept": "application/json",
                "Content-Type": "application/json",
                "Origin": base,
                "Referer": f"{base}/docs/?frame=left",
            },
        )
    except (httpx.TimeoutException, httpx.NetworkError) as e:
        logger.warning("GraphQL %s request failed: %r", op, e)
        raise NetworkError(code="GRAPHQL_REQUEST_FAILED", message=f"GraphQL request failed: {e!r}") from e

    logger.info("GraphQL %s -> HTTP %s (body_len=%s)", op, resp.status_code, len(resp.text or ""))
    logger.debug("GraphQL %s response body: %s", op, (resp.text or "")[:2000])
    if resp.status_code >= 500:
        raise NetworkError(code="GRAPHQL_5XX", message=f"GraphQL status={resp.status_code} body={(resp.text or '')[:300]!r}")
    if resp.status_code >= 400:
        raise NetworkError(
            code="GRAPHQL_4XX",
            message=f"GraphQL status={resp.status_code} body={(resp.text or '')[:300]!r}",
            http_status=502,
        )
    try:
        body = resp.json()
    except (JSONDecodeError, ValueError) as e:
        raise ParseError(
            code="GRAPHQL_NON_JSON",
            message=f"GraphQL ответ не JSON: {(resp.text or '')[:300]!r}",
        ) from e
    if body.get("errors"):
        raise NetworkError(
            code="GRAPHQL_ERRORS",
            message=f"GraphQL errors: {json.dumps(body['errors'], ensure_ascii=False)[:500]}",
        )
    return body.get("data") or {}


async def fetch_board(
    base_url: str,
    client: httpx.AsyncClient,
    cookies: dict[str, str],
    board_id: str,
) -> BoardConfig:
    """Прочитать борд (id, author, title, searchString, widgets) через board.one(id)."""
    logger.debug("fetch_board base_url=%r board_id=%r", base_url, board_id)
    data = await _graphql(base_url, client, cookies, QUERY_BOARD_ONE, {"id": board_id})
    one = ((data.get("board") or {}).get("one")) or None
    if not one:
        raise ParseError(code="BOARD_NOT_FOUND", message=f"board id={board_id!r} не найден")
    widgets = one.get("widgets") or []
    logger.debug(
        "fetch_board -> id=%r title=%r widgets=%s",
        one.get("id"), one.get("title"),
        [(w.get("__typename"), w.get("header"), w.get("widgetUuid")) for w in widgets],
    )
    return BoardConfig(
        id=str(one.get("id") or board_id),
        author=str(one.get("author") or ""),
        title=str(one.get("title") or ""),
        search_string=one.get("searchString"),
        widgets=list(widgets),
        hidden_on_start_page=one.get("hiddenOnStartPage"),
    )


def widget_to_input(widget: dict[str, Any]) -> dict[str, Any]:
    """Преобразовать виджет из board.one в объект для ConfigInput.widgets (срез вычисляемых полей и None)."""
    return {
        k: v
        for k, v in widget.items()
        if k not in _COMPUTED_WIDGET_FIELDS and v is not None
    }


def widgets_to_input(widgets: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """widget_to_input для списка."""
    return [widget_to_input(w) for w in widgets]


async def set_widgets(
    base_url: str,
    client: httpx.AsyncClient,
    cookies: dict[str, str],
    board: BoardConfig,
    widget_inputs: list[dict[str, Any]],
) -> None:
    """
    Полностью заменить набор виджетов борда (editConfig). widget_inputs — уже в формате input
    (см. widgets_to_input). Сериализуется в JSON-строку, как ожидает каталог.
    """
    config_input: dict[str, Any] = {
        "id": board.id,
        "author": board.author,
        "title": board.title,
        "widgets": json.dumps(widget_inputs, ensure_ascii=False),
    }
    if board.search_string is not None:
        config_input["searchString"] = board.search_string
    # Возвращаем флаг как есть: editConfig заменяет конфиг целиком, и без него борд
    # потеряет скрытие и появится плиткой на стартовой странице ТЭ.
    if board.hidden_on_start_page is not None:
        config_input["hiddenOnStartPage"] = board.hidden_on_start_page
    logger.debug(
        "set_widgets board_id=%r widgets_count=%s headers=%s",
        board.id, len(widget_inputs), [w.get("header") for w in widget_inputs],
    )
    await _graphql(base_url, client, cookies, MUTATION_EDIT_CONFIG, {"ConfigInput": config_input})


def board_to_snapshot(board: BoardConfig) -> dict[str, Any]:
    """Конфиг борда в вид для хранения (JSONB в cabinet_snapshots)."""
    return {
        "id": board.id,
        "author": board.author,
        "title": board.title,
        "searchString": board.search_string,
        "hiddenOnStartPage": board.hidden_on_start_page,
        "widgets": list(board.widgets),
    }


def board_from_snapshot(data: dict[str, Any]) -> BoardConfig:
    """Обратное преобразование снимка в BoardConfig."""
    return BoardConfig(
        id=str(data.get("id") or ""),
        author=str(data.get("author") or ""),
        title=str(data.get("title") or ""),
        search_string=data.get("searchString"),
        widgets=list(data.get("widgets") or []),
        hidden_on_start_page=data.get("hiddenOnStartPage"),
    )


# --- Опросник -> чистка под-кабинетов -------------------------------------------------
# Аксиома: виджет «Вернуться назад» не трогаем никогда. «Выбрать всё» в ответах => вопрос пропускаем.
BACK_HEADER = "Вернуться назад"
SELECT_ALL = "Выбрать всё"


def _norm(s: str | None) -> str:
    """Нормализация заголовка/ответа для сравнения: схлопнуть пробелы, casefold, ё->е."""
    return " ".join((s or "").split()).casefold().replace("ё", "е")


# Системные виджеты — НЕ трогаем никогда (навигация/справка). Учтена опечатка в кабинете «Спарвочная».
SYSTEM_KEEP = {_norm(BACK_HEADER), _norm("Справочная информация"), _norm("Спарвочная информация")}


def extract_sub_cabinets(parent: BoardConfig) -> dict[str, str]:
    """Из виджетов родителя -> {sub_board_id: header_блока}. Берём infoboard-id из titleLink/links href; 'Вернуться назад' пропускаем."""
    subs: dict[str, str] = {}
    for w in parent.widgets:
        header = w.get("header") or ""
        if _norm(header) == _norm(BACK_HEADER):
            continue
        hrefs = [((w.get("titleLink") or {}).get("href")) or ""]
        hrefs += [(l.get("href") or "") for l in (w.get("links") or [])]
        for h in hrefs:
            if "infoboard=" in h:
                try:
                    subs[parse_infoboard_id(h)] = header
                    break
                except ParseError:
                    continue
    return subs


async def crawl_boards(
    base_url: str,
    client: httpx.AsyncClient,
    cookies: dict[str, str],
    root_id: str,
    *,
    max_depth: int = 6,
    max_boards: int = 300,
) -> tuple[dict[str, BoardConfig], dict[str, list[str]]]:
    """
    Обойти дерево кабинетов ВГЛУБЬ от root_id по ссылкам (LinksWidget), а не только один уровень:
    целевой под-кабинет может лежать на несколько уровней ниже родителя из payload.

    Возвращает:
      boards:  {board_id: BoardConfig} — все посещённые борды (уже с виджетами);
      by_name: {norm(заголовок): [board_id, ...]} — индекс для поиска под-кабинета по названию;
               индексируем и по собственному title борда, и по заголовку ссылки, что привела к нему.

    Защита: visited-set (от циклов), ограничение глубины (max_depth) и числа бордов (max_boards);
    в служебные разделы (Вернуться назад / Справочная) не углубляемся.
    """
    boards: dict[str, BoardConfig] = {}
    by_name: dict[str, list[str]] = {}
    seen: set[str] = {root_id}
    queue: deque[tuple[str, int, str | None]] = deque([(root_id, 0, None)])

    def _index(key: str | None, board_id: str) -> None:
        nk = _norm(key)
        if not nk:
            return
        lst = by_name.setdefault(nk, [])
        if board_id not in lst:
            lst.append(board_id)

    while queue and len(boards) < max_boards:
        board_id, depth, link_header = queue.popleft()
        try:
            board = await fetch_board(base_url, client, cookies, board_id)
        except (ParseError, NetworkError) as e:
            # Битая/недоступная ссылка на под-кабинет (борда нет -> 404 GRAPHQL_4XX,
            # либо BOARD_NOT_FOUND, либо таймаут) НЕ должна ронять весь обход — пропускаем борд.
            logger.warning("crawl: борд %r пропущен (%s)", board_id, getattr(e, "code", e))
            continue
        boards[board_id] = board
        _index(link_header, board_id)
        _index(board.title, board_id)
        if depth >= max_depth:
            continue
        for child_id, child_header in extract_sub_cabinets(board).items():
            if child_id in seen or _norm(child_header) in SYSTEM_KEEP:
                continue
            seen.add(child_id)
            queue.append((child_id, depth + 1, child_header))

    logger.info(
        "crawl от %r: бордов=%s (глубина<=%s), названий в индексе=%s",
        root_id, len(boards), max_depth, len(by_name),
    )
    return boards, by_name


@dataclass
class QuestionPlan:
    """План по одному вопросу опросника."""
    question: str
    action: str  # 'skip-select-all' | 'unknown-question' | 'no-sub' | 'ambiguous' | 'prune'
    sub_id: str | None = None
    keep_headers: list[str] = field(default_factory=list)
    delete_headers: list[str] = field(default_factory=list)
    # Заголовки виджетов, которые возвращаются из снимка (были удалены прошлым уточнением).
    restore_headers: list[str] = field(default_factory=list)


def plan_widgets_from_snapshot(
    snapshot_widgets: list[dict[str, Any]],
    current_widgets: list[dict[str, Any]],
    delete_headers: set[str],
) -> tuple[list[dict[str, Any]], list[str], list[str]]:
    """
    Собрать итоговый набор виджетов от ЭТАЛОНА (снимка), а не от текущего состояния борда.

    Возвращает (итоговый список для editConfig, восстановленные заголовки, удаляемые заголовки).

    Правила:
      - порядок берём из снимка; виджет, который сейчас на борде есть, переносим КАК ЕСТЬ
        (со своим widgetUuid), иначе создастся дубль;
      - виджет из снимка, которого сейчас нет и который не в списке на удаление, ВОЗВРАЩАЕМ
        с widgetUuid="" — новый uuid присвоит каталог (иначе каталог не примет чужой uuid);
      - системные виджеты (навигация/справка) не удаляем никогда;
      - виджеты, появившиеся на борде после снимка (в снимке их нет), сохраняем в конце —
        чужие ручные правки не теряем.
    """
    current_by_header: dict[str, dict[str, Any]] = {}
    for w in current_widgets:
        current_by_header.setdefault(_norm(w.get("header")), w)

    result: list[dict[str, Any]] = []
    restored: list[str] = []
    for w in snapshot_widgets:
        h = _norm(w.get("header"))
        if h not in SYSTEM_KEEP and h in delete_headers:
            continue
        current = current_by_header.get(h)
        if current is not None:
            result.append(current)
            continue
        revived = dict(w)
        revived["widgetUuid"] = ""
        result.append(revived)
        restored.append(w.get("header") or "")

    snapshot_headers = {_norm(w.get("header")) for w in snapshot_widgets}
    for w in current_widgets:
        if _norm(w.get("header")) not in snapshot_headers:
            result.append(w)

    result_headers = {_norm(w.get("header")) for w in result}
    deleted = [
        w.get("header") or "" for w in current_widgets
        if _norm(w.get("header")) not in result_headers
    ]
    return result, restored, deleted


# Справочник «вопрос -> виджеты» лежит JSON-файлом рядом с модулем (см. gen через scripts).
_MAP_PATH = Path(__file__).with_name("ecology_questionnaire_map.json")


def load_questionnaire_map(path: str | Path | None = None) -> list[dict[str, Any]]:
    """Загрузить справочник: [{question, q_norm, sub_block, answers:{option: widget_header}}]."""
    data = json.loads(Path(path or _MAP_PATH).read_text(encoding="utf-8"))
    out: list[dict[str, Any]] = []
    for item in data.get("questions", []):
        out.append({
            "question": item.get("question", ""),
            "q_norm": _norm(item.get("question")),
            "sub_block": item.get("sub_block") or "",
            "answers": dict(item.get("answers") or {}),
        })
    return out


def _find_q(qmap: list[dict[str, Any]], question: str) -> dict[str, Any] | None:
    """Найти запись справочника по тексту вопроса (нормализованное вхождение в обе стороны)."""
    qn = _norm(question)
    for m in qmap:
        if m["q_norm"] and (m["q_norm"] in qn or qn in m["q_norm"]):
            return m
    return None


async def correct_cabinet(
    base_url: str,
    client: httpx.AsyncClient,
    cookies: dict[str, str],
    parent_link: str,
    questions: list[dict[str, Any]],
    *,
    dry_run: bool = False,
    qmap: list[dict[str, Any]] | None = None,
    reg: str | None = None,
    store: Any = None,
    restore: bool = True,
) -> list[QuestionPlan]:
    """
    По опроснику почистить под-кабинеты, опираясь на справочник. parent_link — точка входа;
    целевой под-кабинет ищется ВГЛУБЬ по всему дереву (он может лежать на несколько уровней ниже).
    Удаляем ТОЛЬКО виджеты известных вариантов ответа, которые клиент НЕ выбрал; всё остальное
    (навигация, справка, выбранные варианты, неизвестные виджеты) остаётся. 'Выбрать всё' -> вопрос не трогаем.
    Под-кабинет находим по sub_block из справочника среди всех бордов дерева (по title/заголовку ссылки);
    при неоднозначности (>1 совпадения) кабинет НЕ трогаем (action='ambiguous').

    store + reg + restore: при первом проходе снимается эталон каждого борда дерева, и дальше
    каждое уточнение считается ОТ ЭТАЛОНА. Поэтому повторный опросник умеет и удалять, и
    возвращать ранее удалённые виджеты. Снимок пишется и в dry_run — чтение безопасно, а к
    моменту боевого прогона эталон уже будет в БД.
    """
    qmap = qmap if qmap is not None else load_questionnaire_map()
    parent_id = parse_infoboard_id(parent_link)
    boards, by_name = await crawl_boards(base_url, client, cookies, parent_id)
    logger.info(
        "correct_cabinet parent=%r поддерево: %s",
        parent_id, {bid: b.title for bid, b in boards.items()},
    )

    use_snapshots = bool(store is not None and reg and restore)
    if store is not None and reg:
        # Эталон снимаем со всего дерева, а не только с изменяемых бордов: следующий опросник
        # может задеть другой под-кабинет, и к тому моменту исходный состав должен быть сохранён.
        for board_id, board in boards.items():
            await store.save_if_absent(reg, board_id, board.title, board_to_snapshot(board))

    plans: list[QuestionPlan] = []
    for q in questions:
        question = str(q.get("question") or "")
        answers = list(q.get("answers") or [])
        if any(_norm(a) == _norm(SELECT_ALL) for a in answers):
            plans.append(QuestionPlan(question, "skip-select-all"))
            logger.info("Q %r -> 'Выбрать всё' -> пропуск", question[:60])
            continue
        m = _find_q(qmap, question)
        if m is None:
            plans.append(QuestionPlan(question, "unknown-question"))
            logger.warning("Q %r -> нет в справочнике -> пропуск", question[:60])
            continue
        matches = by_name.get(_norm(m["sub_block"]), [])
        if not matches:
            plans.append(QuestionPlan(question, "no-sub"))
            logger.warning("Q %r -> под-кабинет %r не найден в дереве -> пропуск", question[:60], m["sub_block"])
            continue
        if len(matches) > 1:
            plans.append(QuestionPlan(question, "ambiguous", None, [], list(matches)))
            logger.error(
                "Q %r -> под-кабинет %r найден в %s местах %s -> НЕ трогаем (нужно уточнение)",
                question[:60], m["sub_block"], len(matches), matches,
            )
            continue
        sub_id = matches[0]
        sub = boards[sub_id]
        selected = {_norm(a) for a in answers if _norm(a) != _norm(SELECT_ALL)}
        # к удалению — заголовки виджетов НЕвыбранных вариантов (из справочника)
        delete_headers = {
            _norm(header) for opt, header in m["answers"].items()
            if header and _norm(opt) not in selected
        }

        snapshot = await store.get(reg, sub_id) if use_snapshots else None
        if snapshot is not None:
            # Эталонный путь: итоговый набор считаем от снимка — умеем и убирать, и возвращать.
            base_widgets = list(board_from_snapshot(snapshot).widgets)
            final_widgets, restored, deleted = plan_widgets_from_snapshot(
                base_widgets, sub.widgets, delete_headers,
            )
            keep = [w.get("header") for w in final_widgets]
        else:
            # Снимка нет (борд появился позже или восстановление отключено) — прежнее поведение:
            # считаем от текущего состояния, только удаление.
            restored = []
            deleted = [
                w.get("header") or "" for w in sub.widgets
                if _norm(w.get("header")) not in SYSTEM_KEEP and _norm(w.get("header")) in delete_headers
            ]
            final_widgets = [
                w for w in sub.widgets
                if _norm(w.get("header")) in SYSTEM_KEEP or _norm(w.get("header")) not in delete_headers
            ]
            keep = [w.get("header") for w in final_widgets]

        plans.append(QuestionPlan(question, "prune", sub_id, keep, deleted, restored))
        logger.info(
            "Q %r -> sub=%s(%r) delete=%s restore=%s (от %s)",
            question[:60], sub_id, sub.title, deleted, restored,
            "снимка" if snapshot is not None else "текущего состояния",
        )
        if not dry_run and (deleted or restored):
            await set_widgets(base_url, client, cookies, sub, widgets_to_input(final_widgets))
    return plans


def prune_widgets(
    widgets: list[dict[str, Any]],
    *,
    keep_headers: set[str] | None = None,
    remove_headers: set[str] | None = None,
    remove_uuids: set[str] | None = None,
    keep_special: bool = True,
) -> list[dict[str, Any]]:
    """
    Отфильтровать виджеты.
      - keep_special: виджеты isSpecial=true оставляем всегда (системные блоки).
      - remove_uuids / remove_headers: явно удалить указанные.
      - keep_headers: если задан — оставить ТОЛЬКО виджеты с этими header (остальные удалить).
    Порядок проверок: keep_special → remove_* → keep_headers.
    """
    kept: list[dict[str, Any]] = []
    for w in widgets:
        uuid = w.get("widgetUuid")
        header = (w.get("header") or "").strip()
        if keep_special and bool(w.get("isSpecial")):
            kept.append(w)
            continue
        if remove_uuids and uuid in remove_uuids:
            continue
        if remove_headers and header in remove_headers:
            continue
        if keep_headers is not None and header not in keep_headers:
            continue
        kept.append(w)
    return kept
