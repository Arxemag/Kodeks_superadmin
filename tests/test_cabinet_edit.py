"""
Тесты редактирования кабинета по опроснику (cabinet_edit.correct_cabinet):
обход дерева ВГЛУБЬ (цель на несколько уровней ниже родителя), защита от неоднозначности,
неприкосновенность системных виджетов, реальное применение (set_widgets) вне dry-run.
"""
from __future__ import annotations

import pytest
from common.exceptions import ParseError

from services.infoboards_service import cabinet_edit
from services.infoboards_service.cabinet_edit import BoardConfig, correct_cabinet


def _link(header: str, board_id: str) -> dict:
    """LinksWidget-навигация на под-кабинет board_id."""
    return {
        "__typename": "LinksWidget",
        "header": header,
        "titleLink": {"href": f"kodeks://link/d?nd=1&infoboard={board_id}"},
    }


def _w(header: str) -> dict:
    """Контентный виджет с заголовком."""
    return {"__typename": "DocListWidget", "header": header, "widgetUuid": f"u-{header}"}


def _back() -> dict:
    return {"__typename": "LinksWidget", "header": cabinet_edit.BACK_HEADER, "titleLink": {"href": ""}}


# Справочник для тестов: один вопрос -> целевой под-кабинет, 3 известных варианта.
QMAP = [
    {
        "question": "Тестовый вопрос про процедуры",
        "q_norm": cabinet_edit._norm("Тестовый вопрос про процедуры"),
        "sub_block": "Целевой под-кабинет",
        "answers": {
            "Оставить А": "Виджет А",
            "Удалить Б": "Виджет Б",
            "Удалить В": "Виджет В",
        },
    }
]

# Дерево на 4 уровня: ROOT(0) -> ЭКОЛОГ(1) -> ПРОМЕЖУТОЧНЫЙ(2) -> ЦЕЛЬ(3).
TARGET_WIDGETS = [_w("Виджет А"), _w("Виджет Б"), _w("Виджет В"), _back()]
TREE = {
    "R0": BoardConfig("R0", "a", "Кабинеты экологии", None, [_link("к экологу", "R1")]),
    "R1": BoardConfig("R1", "a", "Эколог", None, [_link("к докам", "R2"), _back()]),
    "R2": BoardConfig("R2", "a", "Промежуточный", None, [_link("к цели", "R3"), _back()]),
    "R3": BoardConfig("R3", "a", "Целевой под-кабинет", None, list(TARGET_WIDGETS)),
}


def _patch_tree(monkeypatch, tree: dict[str, BoardConfig]) -> None:
    async def fake_fetch_board(base_url, client, cookies, board_id):
        board = tree.get(board_id)
        if board is None:
            from common.exceptions import ParseError
            raise ParseError(code="BOARD_NOT_FOUND", message=board_id)
        # отдаём копию виджетов, чтобы тест не мутировал эталон
        return BoardConfig(board.id, board.author, board.title, board.search_string, list(board.widgets))
    monkeypatch.setattr(cabinet_edit, "fetch_board", fake_fetch_board)


@pytest.mark.asyncio
async def test_target_found_deep_in_tree(monkeypatch):
    """Цель на 3 уровня ниже родителя — находится; удаляются невыбранные, системные и выбранный остаются."""
    _patch_tree(monkeypatch, TREE)
    link = "https://host/docs/?nd=1&infoboard=R0"
    questions = [{"question": "Тестовый вопрос про процедуры", "answers": ["Оставить А"]}]

    plans = await correct_cabinet("https://host", None, {}, link, questions, dry_run=True, qmap=QMAP)

    assert len(plans) == 1
    p = plans[0]
    assert p.action == "prune"
    assert p.sub_id == "R3"
    assert set(p.delete_headers) == {"Виджет Б", "Виджет В"}      # невыбранные
    assert "Виджет А" in p.keep_headers                           # выбранный — остаётся
    assert cabinet_edit.BACK_HEADER in p.keep_headers             # система — остаётся


@pytest.mark.asyncio
async def test_select_all_skips_question(monkeypatch):
    """'Выбрать всё' -> вопрос не трогаем."""
    _patch_tree(monkeypatch, TREE)
    link = "https://host/docs/?nd=1&infoboard=R0"
    questions = [{"question": "Тестовый вопрос про процедуры", "answers": [cabinet_edit.SELECT_ALL]}]

    plans = await correct_cabinet("https://host", None, {}, link, questions, dry_run=True, qmap=QMAP)
    assert plans[0].action == "skip-select-all"


@pytest.mark.asyncio
async def test_ambiguous_target_not_touched(monkeypatch):
    """Под-кабинет с тем же названием встречается дважды -> ambiguous, ничего не удаляем."""
    tree = dict(TREE)
    # вторая ветка из ЭКОЛОГ ведёт к ещё одному 'Целевой под-кабинет'
    tree["R1"] = BoardConfig("R1", "a", "Эколог", None, [_link("к докам", "R2"), _link("дубль", "R9"), _back()])
    tree["R9"] = BoardConfig("R9", "a", "Целевой под-кабинет", None, list(TARGET_WIDGETS))
    _patch_tree(monkeypatch, tree)
    link = "https://host/docs/?nd=1&infoboard=R0"
    questions = [{"question": "Тестовый вопрос про процедуры", "answers": ["Оставить А"]}]

    with pytest.raises(ParseError, match="ambiguous"):
        await correct_cabinet("https://host", None, {}, link, questions, dry_run=True, qmap=QMAP)


@pytest.mark.asyncio
async def test_unknown_question_skipped(monkeypatch):
    """Вопрос не из справочника -> пропуск (unknown-question)."""
    _patch_tree(monkeypatch, TREE)
    link = "https://host/docs/?nd=1&infoboard=R0"
    questions = [{"question": "Совершенно другой вопрос", "answers": ["что-то"]}]

    with pytest.raises(ParseError, match="unknown-question"):
        await correct_cabinet("https://host", None, {}, link, questions, dry_run=True, qmap=QMAP)


@pytest.mark.asyncio
async def test_missing_linked_board_does_not_hide_valid_target(monkeypatch):
    from common.exceptions import NetworkError
    tree = dict(TREE)
    tree['R2'] = BoardConfig('R2', 'a', 'Intermediate', None, [_link('broken', 'RBAD'), _link('target', 'R3')])
    async def fetch(base_url, client, cookies, board_id):
        if board_id == 'RBAD':
            raise NetworkError(code='GRAPHQL_4XX', message='GraphQL status=404')
        board = tree[board_id]
        return BoardConfig(board.id, board.author, board.title, board.search_string, list(board.widgets))
    monkeypatch.setattr(cabinet_edit, 'fetch_board', fetch)
    plans = await correct_cabinet('https://host', None, {}, '?infoboard=R0', [
        {'question': QMAP[0]['question'], 'answers': ['Оставить А']},
    ], dry_run=True, qmap=QMAP)
    assert plans[0].sub_id == 'R3'


@pytest.mark.asyncio
async def test_catalog_timeout_is_not_treated_as_a_broken_link(monkeypatch):
    from common.exceptions import NetworkError
    from unittest.mock import AsyncMock
    monkeypatch.setattr(cabinet_edit, 'fetch_board', AsyncMock(side_effect=NetworkError(code='GRAPHQL_REQUEST_FAILED')))
    with pytest.raises(NetworkError):
        await correct_cabinet('https://host', None, {}, '?infoboard=R0', [], dry_run=True, qmap=QMAP)


@pytest.mark.asyncio
async def test_apply_calls_set_widgets_with_kept(monkeypatch):
    """Вне dry-run вызывается set_widgets с виджетами БЕЗ удаляемых, но С системными и выбранным."""
    tree = dict(TREE)
    _patch_tree(monkeypatch, tree)
    captured = {}

    async def fake_set_widgets(base_url, client, cookies, board, widget_inputs):
        captured["board_id"] = board.id
        captured["headers"] = [w.get("header") for w in widget_inputs]
        tree[board.id] = BoardConfig(board.id, board.author, board.title, board.search_string, widget_inputs)
    monkeypatch.setattr(cabinet_edit, "set_widgets", fake_set_widgets)

    link = "https://host/docs/?nd=1&infoboard=R0"
    questions = [{"question": "Тестовый вопрос про процедуры", "answers": ["Оставить А"]}]

    await correct_cabinet("https://host", None, {}, link, questions, dry_run=False, qmap=QMAP)

    assert captured["board_id"] == "R3"
    assert set(captured["headers"]) == {"Виджет А", cabinet_edit.BACK_HEADER}  # Б и В удалены


@pytest.mark.asyncio
async def test_invalid_later_question_prevents_all_writes(monkeypatch):
    from unittest.mock import AsyncMock
    _patch_tree(monkeypatch, TREE)
    save = AsyncMock()
    monkeypatch.setattr(cabinet_edit, "set_widgets", save)
    with pytest.raises(ParseError):
        await correct_cabinet("https://host", None, {}, "?infoboard=R0", [
            {"question": QMAP[0]["question"], "answers": ["Оставить А"]},
            {"question": "Unknown", "answers": []},
        ], qmap=QMAP)
    save.assert_not_awaited()


@pytest.mark.asyncio
async def test_unknown_answer_cannot_delete_known_widgets(monkeypatch):
    from unittest.mock import AsyncMock
    _patch_tree(monkeypatch, TREE)
    save = AsyncMock()
    monkeypatch.setattr(cabinet_edit, "set_widgets", save)
    with pytest.raises(ParseError, match="Unknown answer"):
        await correct_cabinet("https://host", None, {}, "?infoboard=R0", [
            {"question": QMAP[0]["question"], "answers": ["Unknown"]},
        ], qmap=QMAP)
    save.assert_not_awaited()


@pytest.mark.asyncio
async def test_noop_widget_save_is_not_success(monkeypatch):
    from unittest.mock import AsyncMock
    _patch_tree(monkeypatch, TREE)
    monkeypatch.setattr(cabinet_edit, "set_widgets", AsyncMock())
    with pytest.raises(ParseError, match="not applied"):
        await correct_cabinet("https://host", None, {}, "?infoboard=R0", [
            {"question": QMAP[0]["question"], "answers": ["Оставить А"]},
        ], qmap=QMAP)


@pytest.mark.asyncio
async def test_mutation_timeout_is_not_treated_as_retryable_read_failure(monkeypatch):
    from unittest.mock import AsyncMock
    from common.exceptions import NetworkError
    monkeypatch.setattr(cabinet_edit, '_graphql', AsyncMock(side_effect=NetworkError(code='GRAPHQL_REQUEST_FAILED')))
    with pytest.raises(ParseError) as error:
        await cabinet_edit.set_widgets('https://host', None, {}, TREE['R3'], [])
    assert error.value.code == 'WIDGET_WRITE_UNCERTAIN'


_STATE_BASE = 'https://host'
_STATE_REG = '465304'
_STATE_BOARD = 'R3'


class _WidgetStateRepository:
    def __init__(self):
        self.states = {}

    async def get(self, base, reg, board_id):
        from copy import deepcopy
        from common.cabinet_widget_state import CabinetWidgetState

        return deepcopy(self.states.get((base, reg, board_id), CabinetWidgetState(archive=[], pending=None)))

    async def put(self, base, reg, board_id, state):
        from copy import deepcopy

        self.states[base, reg, board_id] = deepcopy(state)


class _WidgetCatalog:
    """Keep live config separate from the state repository, like the real catalog."""

    def __init__(self, monkeypatch):
        from copy import deepcopy

        self.original = [
            {**_w('Виджет А'), 'position': 'right', 'description': 'A', 'limit': 3},
            {**_w('Ручной виджет'), 'position': 'left', 'description': 'Ручная настройка', 'counter': 2},
            {**_w('Виджет Б'), 'position': 'right', 'description': 'B', 'counter': 7,
             'palette': {'colorWidget': '#123456', 'colorText': '#abcdef'}},
            {**_back(), 'widgetUuid': 'u-back', 'position': 'left'},
            {**_w('Виджет В'), 'position': 'left', 'description': 'C', 'limit': 5},
        ]
        self.tree = dict(TREE)
        self.tree[_STATE_BOARD] = BoardConfig(_STATE_BOARD, 'a', TREE[_STATE_BOARD].title,
                                              None, deepcopy(self.original))
        _patch_tree(monkeypatch, self.tree)
        self.posts = []
        self.lose_response = False
        self.apply_write = True

    def live_inputs(self):
        return cabinet_edit.widgets_to_input(self.tree[_STATE_BOARD].widgets)

    def set_live_inputs(self, inputs):
        from copy import deepcopy

        types = {widget['widgetUuid']: widget['__typename'] for widget in self.original}
        widgets = [{**deepcopy(item), '__typename': types[item['widgetUuid']]} for item in inputs]
        board = self.tree[_STATE_BOARD]
        self.tree[_STATE_BOARD] = BoardConfig(board.id, board.author, board.title, board.search_string, widgets)

    async def post(self, url, *, json, **kwargs):
        from json import loads
        import httpx

        assert 'mutation EditConfigWidgets' in json['query']
        config = json['variables']['ConfigInput']
        assert config['id'] == _STATE_BOARD and isinstance(config['widgets'], str)
        inputs = loads(config['widgets'])
        self.posts.append(inputs)
        if self.apply_write:
            self.set_live_inputs(inputs)
        if self.lose_response:
            raise httpx.RemoteProtocolError('connection closed after catalog applied the write')
        return httpx.Response(200, json={'data': {'editConfig': {'id': _STATE_BOARD}}})


def _answers(*selected):
    return [{'question': QMAP[0]['question'], 'answers': list(selected)}]


async def _correct_with_state(catalog, repo, questions):
    return await correct_cabinet(_STATE_BASE, catalog, {}, '?infoboard=R0', questions,
                                 qmap=QMAP, repository=repo, reg=_STATE_REG)


@pytest.mark.asyncio
async def test_reselection_restores_original_config_uuid_and_order_without_losing_manual_widget(monkeypatch):
    catalog = _WidgetCatalog(monkeypatch)
    repo = _WidgetStateRepository()
    a, manual, b, back, c = original = catalog.live_inputs()

    await _correct_with_state(catalog, repo, _answers('Оставить А'))
    assert catalog.live_inputs() == [a, manual, back]
    state = await repo.get(_STATE_BASE, _STATE_REG, _STATE_BOARD)
    assert state.pending is None
    assert sorted((item['index'], item['widget']['widgetUuid']) for item in state.archive) == [
        (2, b['widgetUuid']), (4, c['widgetUuid']),
    ]

    await _correct_with_state(catalog, repo, _answers('Удалить Б'))
    assert catalog.live_inputs() == [manual, b, back]
    await _correct_with_state(catalog, repo, _answers('Оставить А', 'Удалить В'))
    assert catalog.live_inputs() == [a, manual, back, c]
    assert len(catalog.posts) == 3

    await _correct_with_state(catalog, repo, _answers(cabinet_edit.SELECT_ALL))
    assert catalog.live_inputs() == original
    assert len(catalog.posts) == 4
    state = await repo.get(_STATE_BASE, _STATE_REG, _STATE_BOARD)
    assert state.archive == [] and state.pending is None


@pytest.mark.asyncio
async def test_repeating_questionnaire_state_does_not_submit_another_mutation(monkeypatch):
    catalog = _WidgetCatalog(monkeypatch)
    repo = _WidgetStateRepository()
    questions = _answers('Оставить А')
    await _correct_with_state(catalog, repo, questions)
    before = catalog.live_inputs()
    state_before = await repo.get(_STATE_BASE, _STATE_REG, _STATE_BOARD)

    await _correct_with_state(catalog, repo, questions)

    assert len(catalog.posts) == 1
    assert catalog.live_inputs() == before
    assert await repo.get(_STATE_BASE, _STATE_REG, _STATE_BOARD) == state_before


@pytest.mark.asyncio
@pytest.mark.parametrize('questions', [
    [{'question': ' \t ', 'answers': ['Оставить А']}],
    [*_answers('Оставить А'), *_answers('Удалить Б')],
])
async def test_empty_or_repeated_question_is_rejected_before_any_widget_or_state_change(monkeypatch, questions):
    catalog = _WidgetCatalog(monkeypatch)
    repo = _WidgetStateRepository()
    original = catalog.live_inputs()

    with pytest.raises(ParseError):
        await _correct_with_state(catalog, repo, questions)

    assert catalog.live_inputs() == original
    assert catalog.posts == []
    assert repo.states == {}


@pytest.mark.asyncio
async def test_readback_recovers_applied_write_after_remote_protocol_error(monkeypatch):
    catalog = _WidgetCatalog(monkeypatch)
    catalog.lose_response = True
    repo = _WidgetStateRepository()
    a, manual, b, back, c = catalog.live_inputs()

    await _correct_with_state(catalog, repo, _answers('Оставить А'))

    assert catalog.posts == [[a, manual, back]]
    assert catalog.live_inputs() == [a, manual, back]
    state = await repo.get(_STATE_BASE, _STATE_REG, _STATE_BOARD)
    assert state.pending is None
    assert {item['widget']['widgetUuid'] for item in state.archive} == {b['widgetUuid'], c['widgetUuid']}


@pytest.mark.asyncio
async def test_pending_write_matching_before_does_not_repeat_or_discard_uncertain_state(monkeypatch):
    from common.cabinet_widget_state import CabinetWidgetState

    catalog = _WidgetCatalog(monkeypatch)
    repo = _WidgetStateRepository()
    a, manual, b, back, c = original = catalog.live_inputs()
    pending = {'before': original, 'after': [a, manual, back],
               'archive': [{'widget': b, 'index': 2}, {'widget': c, 'index': 4}]}
    initial = CabinetWidgetState(archive=[], pending=pending)
    await repo.put(_STATE_BASE, _STATE_REG, _STATE_BOARD, initial)

    with pytest.raises(ParseError) as error:
        await _correct_with_state(catalog, repo, _answers('Оставить А'))

    assert error.value.code == 'WIDGET_WRITE_UNCERTAIN'
    assert catalog.posts == []
    assert catalog.live_inputs() == original
    assert await repo.get(_STATE_BASE, _STATE_REG, _STATE_BOARD) == initial


@pytest.mark.asyncio
async def test_pending_write_matching_after_recovers_archive_without_repeating_post(monkeypatch):
    from common.cabinet_widget_state import CabinetWidgetState

    catalog = _WidgetCatalog(monkeypatch)
    repo = _WidgetStateRepository()
    a, manual, b, back, c = original = catalog.live_inputs()
    archive = [{'widget': b, 'index': 2}, {'widget': c, 'index': 4}]
    pending = {'before': original, 'after': [a, manual, back], 'archive': archive}
    await repo.put(_STATE_BASE, _STATE_REG, _STATE_BOARD, CabinetWidgetState(archive=[], pending=pending))
    catalog.set_live_inputs(pending['after'])

    await _correct_with_state(catalog, repo, _answers('Оставить А'))

    assert catalog.posts == []
    assert catalog.live_inputs() == pending['after']
    assert await repo.get(_STATE_BASE, _STATE_REG, _STATE_BOARD) == CabinetWidgetState(archive=archive, pending=None)
    await _correct_with_state(catalog, repo, _answers(cabinet_edit.SELECT_ALL))
    assert catalog.posts == [original]
    assert catalog.live_inputs() == original


@pytest.mark.asyncio
async def test_positive_response_without_application_clears_pending_but_fails_write(monkeypatch):
    catalog = _WidgetCatalog(monkeypatch)
    catalog.apply_write = False
    repo = _WidgetStateRepository()
    original = catalog.live_inputs()

    with pytest.raises(ParseError) as error:
        await _correct_with_state(catalog, repo, _answers('Оставить А'))

    assert error.value.code == 'CABINET_WRITE_NOT_CONFIRMED'
    assert len(catalog.posts) == 1
    assert catalog.live_inputs() == original
    state = await repo.get(_STATE_BASE, _STATE_REG, _STATE_BOARD)
    assert state.pending is None and state.archive == []
