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
    logger.debug(
        "set_widgets board_id=%r widgets_count=%s headers=%s",
        board.id, len(widget_inputs), [w.get("header") for w in widget_inputs],
    )
    await _graphql(base_url, client, cookies, MUTATION_EDIT_CONFIG, {"ConfigInput": config_input})


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


@dataclass
class QuestionPlan:
    """План по одному вопросу опросника."""
    question: str
    action: str  # 'skip-select-all' | 'no-sub' | 'prune'
    sub_id: str | None = None
    keep_headers: list[str] = field(default_factory=list)
    delete_headers: list[str] = field(default_factory=list)


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
) -> list[QuestionPlan]:
    """
    По опроснику почистить под-кабинеты родителя parent_link, опираясь на справочник.
    Удаляем ТОЛЬКО виджеты известных вариантов ответа, которые клиент НЕ выбрал; всё остальное
    (навигация, справка, выбранные варианты, неизвестные виджеты) остаётся. 'Выбрать всё' -> вопрос не трогаем.
    Под-кабинет находим по sub_block из справочника среди ссылок родителя; расхождения текстов
    ответ/заголовок резолвятся справочником (option -> точный widget_header).
    """
    qmap = qmap if qmap is not None else load_questionnaire_map()
    parent_id = parse_infoboard_id(parent_link)
    parent = await fetch_board(base_url, client, cookies, parent_id)
    sub_by_block = {_norm(block): sid for sid, block in extract_sub_cabinets(parent).items()}
    logger.info("correct_cabinet parent=%r sub_by_block=%s", parent_id, sub_by_block)

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
        sub_id = sub_by_block.get(_norm(m["sub_block"]))
        if not sub_id:
            plans.append(QuestionPlan(question, "no-sub"))
            logger.warning("Q %r -> под-кабинет %r не найден у родителя -> пропуск", question[:60], m["sub_block"])
            continue
        selected = {_norm(a) for a in answers if _norm(a) != _norm(SELECT_ALL)}
        # к удалению — заголовки виджетов НЕвыбранных вариантов (из справочника)
        delete_headers = {
            _norm(header) for opt, header in m["answers"].items()
            if header and _norm(opt) not in selected
        }
        sub = await fetch_board(base_url, client, cookies, sub_id)
        keep, delete = [], []
        for w in sub.widgets:
            if _norm(w.get("header")) in delete_headers:
                delete.append(w.get("header"))
            else:
                keep.append(w.get("header"))
        plans.append(QuestionPlan(question, "prune", sub_id, keep, delete))
        logger.info("Q %r -> sub=%s delete=%s", question[:60], sub_id, delete)
        if not dry_run and delete:
            kept_widgets = [w for w in sub.widgets if _norm(w.get("header")) not in delete_headers]
            await set_widgets(base_url, client, cookies, sub, widgets_to_input(kept_widgets))
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
