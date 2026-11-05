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
from urllib.parse import urlsplit

import httpx

from common.exceptions import NetworkError, ParseError
from common.cabinet_widget_state import CabinetWidgetState, CabinetWidgetStateRepository
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
      boardUuid
      author
      title
      background backgroundFilename icons background2 background2Filename disableBackgroundBlur
      logo logoFilename logo2 logo2Filename hiddenOnStartPage
      palette { primary { main dark text link } secondary { main dark text link } }
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
    settings: dict[str, Any] = field(default_factory=dict)


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
    except httpx.RequestError as e:
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
    widgets = one.get("widgets") if isinstance(one, dict) else None
    if not isinstance(widgets, list) or one.get("id") != board_id or not all(isinstance(w, dict) for w in widgets):
        raise ParseError(code="CABINET_BOARD_INVALID", message=f"Invalid configuration for {board_id!r}")
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
        settings={key: one[key] for key in (
            "boardUuid", "background", "backgroundFilename", "icons", "background2", "background2Filename",
            "disableBackgroundBlur", "logo", "logoFilename", "logo2", "logo2Filename", "hiddenOnStartPage", "palette",
        ) if one.get(key) is not None},
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
        **board.settings,
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
    try:
        await _graphql(base_url, client, cookies, MUTATION_EDIT_CONFIG, {"ConfigInput": config_input})
    except (NetworkError, ParseError) as error:
        # A mutation may have reached the catalog even if its reply failed.
        # The caller must reconcile before trying this full replacement again.
        raise ParseError(code="WIDGET_WRITE_UNCERTAIN", message=f"Widget write outcome is unknown for {board.id!r}") from error


# --- Опросник -> чистка под-кабинетов -------------------------------------------------
# Системные виджеты сохраняются. «Выбрать всё» возвращает сохранённые варианты.
BACK_HEADER = "Вернуться назад"
SELECT_ALL = "Выбрать всё"


def _norm(s: str | None) -> str:
    """Нормализация заголовка/ответа для сравнения: схлопнуть пробелы, casefold, ё->е."""
    return " ".join((s or "").split()).casefold().replace("ё", "е")


# Системные виджеты — НЕ трогаем никогда (навигация/справка). Учтена опечатка в кабинете «Спарвочная».
SYSTEM_KEEP = {_norm(BACK_HEADER), _norm("Справочная информация"), _norm("Спарвочная информация")}


def extract_sub_cabinets(parent: BoardConfig, *, base_url: str | None = None) -> dict[str, str]:
    """Из виджетов родителя -> {sub_board_id: header_блока}. Берём infoboard-id из titleLink/links href; 'Вернуться назад' пропускаем."""
    subs: dict[str, str] = {}
    for w in parent.widgets:
        header = w.get("header") or ""
        if _norm(header) in SYSTEM_KEEP:
            continue
        hrefs = [((w.get("titleLink") or {}).get("href")) or ""]
        hrefs += [(link.get("href") or "") for link in (w.get("links") or [])
                  if _norm(link.get("title")) not in SYSTEM_KEEP]
        for h in hrefs:
            if "infoboard=" in h:
                url = urlsplit(h)
                if url.scheme == "kodeks":
                    if url.netloc != "link" or url.path != "/d":
                        continue
                elif (url.scheme and url.scheme not in {"http", "https"}) or (
                    url.netloc and base_url and url.hostname != urlsplit(base_url).hostname
                ):
                    continue
                try:
                    subs[parse_infoboard_id(h)] = header
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
        except ParseError as e:
            if board_id == root_id or e.code != "BOARD_NOT_FOUND":
                raise
            logger.warning("crawl: борд %r пропущен (%s)", board_id, getattr(e, "code", e))
            continue
        except NetworkError as e:
            # Preserve the existing release fix for a link to a missing board.
            # A timeout or a temporary catalog outage must still be retried.
            if board_id != root_id and e.code == "GRAPHQL_4XX" and "status=404" in e.message:
                logger.warning("crawl: отсутствующий борд %r пропущен", board_id)
                continue
            raise
        boards[board_id] = board
        _index(link_header, board_id)
        _index(board.title, board_id)
        for child_id, child_header in extract_sub_cabinets(board, base_url=base_url).items():
            if child_id in seen or _norm(child_header) in SYSTEM_KEEP:
                continue
            if depth >= max_depth:
                raise ParseError(code="CABINET_PLAN_INVALID", message="Cabinet navigation exceeds the depth limit")
            seen.add(child_id)
            queue.append((child_id, depth + 1, child_header))

    if queue:
        raise ParseError(code="CABINET_PLAN_INVALID", message="Cabinet navigation exceeds the board limit")

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
    restore_headers: list[str] = field(default_factory=list)


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
    qn = _norm(question).removesuffix("(можно выбрать несколько)").strip()
    matches = [entry for entry in qmap if qn and entry["q_norm"] == qn]
    if len(matches) > 1:
        raise ParseError(code="CABINET_PLAN_INVALID", message="Question matches multiple entries")
    return matches[0] if matches else None


def _widget_selection(current, archive, allowed, selected):
    """Rebuild the order with hidden widgets, then split it by the new selection."""
    layout = [(widget, False) for widget in current]
    current_ids = {widget.get("widgetUuid") for widget in current if widget.get("widgetUuid")}
    current_headers = {_norm(widget.get("header")) for widget in current}
    archived_ids = set()
    for entry in sorted(archive, key=lambda item: item["index"]):
        widget = entry["widget"]
        uid = widget.get("widgetUuid")
        if not uid or uid in archived_ids:
            raise ParseError(code="CABINET_WIDGET_STATE_INVALID", message="Invalid archived widget identity")
        archived_ids.add(uid)
        if uid in current_ids:
            continue  # A widget restored outside the integration keeps its current configuration.
        if _norm(widget.get("header")) in selected & current_headers:
            raise ParseError(code="CABINET_WIDGET_CONFLICT", message="An archived widget has a replacement with the same title")
        layout.insert(entry["index"], (widget, True))

    visible, hidden, removed, restored = [], [], [], []
    for index, (widget, archived) in enumerate(layout):
        header = _norm(widget.get("header"))
        keep = header in selected if archived else (
            header not in allowed - selected or header in SYSTEM_KEEP or widget.get("isSpecial")
        )
        if keep:
            visible.append(widget)
            if archived:
                restored.append(widget.get("header"))
        else:
            hidden.append({"widget": widget, "index": index})
            if not archived:
                removed.append(widget.get("header"))
    return visible, hidden, removed, restored


async def correct_cabinet(
    base_url: str,
    client: httpx.AsyncClient,
    cookies: dict[str, str],
    parent_link: str,
    questions: list[dict[str, Any]],
    *,
    dry_run: bool = False,
    qmap: list[dict[str, Any]] | None = None,
    repository: CabinetWidgetStateRepository | None = None,
    reg: str | None = None,
) -> list[QuestionPlan]:
    """Validate the complete questionnaire and apply the new widget selection."""
    if repository is not None and not reg:
        raise ValueError("A registration is required for the widget archive")
    link = urlsplit(parent_link)
    if link.netloc and (link.scheme not in {"http", "https"} or link.hostname != urlsplit(base_url).hostname):
        raise ParseError(code="CABINET_LINK_OUT_OF_SCOPE", message="Cabinet link belongs to another catalog")
    qmap = qmap if qmap is not None else load_questionnaire_map()
    parent_id = parse_infoboard_id(parent_link)
    boards, by_name = await crawl_boards(base_url, client, cookies, parent_id)
    plans: list[QuestionPlan] = []
    selections: dict[str, tuple[set[str], set[str], set[str]]] = {}
    seen_questions: set[str] = set()
    for item in questions:
        question = item.get("question")
        answers = item.get("answers")
        if (not isinstance(question, str) or not question.strip() or not isinstance(answers, list)
                or any(not isinstance(answer, str) or not answer.strip() for answer in answers)):
            raise ParseError(code="CABINET_PLAN_INVALID", message="A question requires a title and an explicit answer list")
        m = _find_q(qmap, question)
        key = m["q_norm"] if m else _norm(question)
        if key in seen_questions:
            raise ParseError(code="CABINET_PLAN_INVALID", message="Duplicate questionnaire question")
        seen_questions.add(key)
        chosen = {_norm(answer) for answer in answers}
        select_all = _norm(SELECT_ALL) in chosen
        if select_all and len(chosen) != 1:
            raise ParseError(code="CABINET_PLAN_INVALID", message="Select all cannot be combined with individual answers")
        if select_all and (m is None or repository is None):
            plans.append(QuestionPlan(question, "skip-select-all"))
            continue
        if m is None:
            plans.append(QuestionPlan(question, "unknown-question"))
            continue
        options = {_norm(answer): _norm(header) for answer, header in m["answers"].items()}
        if not select_all and chosen - options.keys():
            raise ParseError(code="UNKNOWN_QUESTIONNAIRE_ANSWER", message=f"Unknown answer for question {question!r}")
        if not select_all and any(not options[answer] for answer in chosen):
            raise ParseError(code="QUESTIONNAIRE_WIDGET_UNMAPPED", message=f"An answer has no widget mapping for {question!r}")
        matches = by_name.get(_norm(m["sub_block"]), [])
        if len(matches) != 1:
            plans.append(QuestionPlan(question, "ambiguous" if matches else "no-sub"))
            continue
        board_id = matches[0]
        allowed = set(options.values()) - {""}
        selected = allowed if select_all else {options[answer] for answer in chosen}
        required = set() if select_all else selected
        if board_id in selections:
            previous_allowed, previous_selected, previous_required = selections[board_id]
            if previous_allowed & allowed:
                raise ParseError(code="CABINET_PLAN_INVALID", message="Questions overlap on the same widget")
            allowed, selected = previous_allowed | allowed, previous_selected | selected
            required = previous_required | required
        selections[board_id] = allowed, selected, required
        plans.append(QuestionPlan(question, "prune", board_id))

    invalid = [plan.action for plan in plans if plan.action in {"unknown-question", "no-sub", "ambiguous"}]
    if invalid:
        raise ParseError(code="CABINET_PLAN_INVALID", message=f"Cannot resolve all questionnaire targets: {invalid}")

    changes = []
    for board_id, (allowed, selected, required) in selections.items():
        board = boards[board_id]
        current = widgets_to_input(board.widgets)
        state = await repository.get(base_url, reg, board_id) if repository else CabinetWidgetState([])
        archive = state.archive
        if state.pending is not None:
            if current != state.pending["after"]:
                raise ParseError(code="WIDGET_WRITE_UNCERTAIN", message=f"Previous widget write is still unconfirmed for {board_id!r}")
            archive = state.pending["archive"]
        expected, hidden, removed, restored = _widget_selection(current, archive, allowed, selected)
        if required - {_norm(widget.get("header")) for widget in expected}:
            raise ParseError(code="CABINET_WIDGET_NOT_FOUND", message=f"A selected widget is absent from the cabinet and archive for {board_id!r}")
        if expected != current and repository:
            ids = [entry["widget"].get("widgetUuid") for entry in hidden]
            if not all(ids) or len(set(ids)) != len(ids) or any(not widget for widget in current):
                raise ParseError(code="CABINET_BOARD_INVALID", message=f"Cannot safely archive widgets in {board_id!r}")
        for plan in plans:
            if plan.sub_id == board_id:
                plan.keep_headers = [widget.get("header") for widget in expected]
                plan.delete_headers = removed
                plan.restore_headers = restored
        changes.append((board, current, expected, state, archive, hidden))

    if dry_run:
        return plans
    for board, current, expected, state, archive, hidden in changes:
        if current == expected:
            if repository and (state.pending is not None or state.archive != hidden):
                await repository.put(base_url, reg, board.id, CabinetWidgetState(hidden))
            continue
        if repository:
            transition = {"before": current, "after": expected, "archive": hidden}
            await repository.put(base_url, reg, board.id, CabinetWidgetState(archive, transition))
        uncertain = None
        try:
            await set_widgets(base_url, client, cookies, board, expected)
        except ParseError as error:
            if error.code != "WIDGET_WRITE_UNCERTAIN":
                raise
            uncertain = error
        try:
            actual = await fetch_board(base_url, client, cookies, board.id)
        except (NetworkError, ParseError) as error:
            raise ParseError(code="WIDGET_WRITE_UNCERTAIN", message=f"Widget write could not be verified for {board.id!r}") from error
        actual_inputs = widgets_to_input(actual.widgets)
        if (actual.title, actual.author, actual.search_string, actual.settings) != (
            board.title, board.author, board.search_string, board.settings
        ):
            raise ParseError(code="WIDGET_WRITE_UNCERTAIN", message=f"Cabinet settings changed during the widget write for {board.id!r}")
        if actual_inputs != expected:
            if uncertain or actual_inputs != current:
                raise ParseError(code="WIDGET_WRITE_UNCERTAIN", message=f"Widget write is unconfirmed for {board.id!r}") from uncertain
            if repository:
                await repository.put(base_url, reg, board.id, CabinetWidgetState(archive))
            raise ParseError(code="CABINET_WRITE_NOT_CONFIRMED", message=f"Widget configuration was not applied for {board.id!r}")
        if repository:
            try:
                await repository.put(base_url, reg, board.id, CabinetWidgetState(hidden))
            except NetworkError as error:
                raise ParseError(code="WIDGET_WRITE_UNCERTAIN", message=f"Saved widgets were verified but the archive could not be finalized for {board.id!r}") from error
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
