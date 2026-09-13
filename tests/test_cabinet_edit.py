"""
Тесты редактирования кабинета по опроснику (cabinet_edit.correct_cabinet):
обход дерева ВГЛУБЬ (цель на несколько уровней ниже родителя), защита от неоднозначности,
неприкосновенность системных виджетов, реальное применение (set_widgets) вне dry-run.
"""
from __future__ import annotations

import json

import pytest

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


# То же дерево ПОСЛЕ первого уточнения: в цели остались только «Виджет А» и навигация.
PRUNED_TREE = dict(TREE)
PRUNED_TREE["R3"] = BoardConfig("R3", "a", "Целевой под-кабинет", None, [_w("Виджет А"), _back()])


async def _noop_set_widgets(base_url, client, cookies, board, widget_inputs) -> None:
    """Заглушка записи: тесту важен только расчёт плана."""
    return None


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

    plans = await correct_cabinet("https://host", None, {}, link, questions, dry_run=True, qmap=QMAP)
    assert plans[0].action == "ambiguous"
    assert set(plans[0].delete_headers) == {"R3", "R9"}  # конфликтующие борды


@pytest.mark.asyncio
async def test_unknown_question_skipped(monkeypatch):
    """Вопрос не из справочника -> пропуск (unknown-question)."""
    _patch_tree(monkeypatch, TREE)
    link = "https://host/docs/?nd=1&infoboard=R0"
    questions = [{"question": "Совершенно другой вопрос", "answers": ["что-то"]}]

    plans = await correct_cabinet("https://host", None, {}, link, questions, dry_run=True, qmap=QMAP)
    assert plans[0].action == "unknown-question"


@pytest.mark.asyncio
async def test_crawl_skips_broken_subcabinet(monkeypatch):
    """Битая ссылка на под-кабинет (404 -> NetworkError) не роняет обход; цель находится."""
    tree = dict(TREE)
    # у ПРОМЕЖУТОЧНОГО (R2) появляется ссылка на несуществующий борд RBAD + рабочая на цель R3
    tree["R2"] = BoardConfig("R2", "a", "Промежуточный", None,
                             [_link("битая", "RBAD"), _link("к цели", "R3"), _back()])

    async def fake_fetch_board(base_url, client, cookies, board_id):
        if board_id == "RBAD":
            from common.exceptions import NetworkError
            raise NetworkError(code="GRAPHQL_4XX", message="GraphQL status=404 P000S not found")
        b = tree.get(board_id)
        if b is None:
            from common.exceptions import ParseError
            raise ParseError(code="BOARD_NOT_FOUND", message=board_id)
        return BoardConfig(b.id, b.author, b.title, b.search_string, list(b.widgets))
    monkeypatch.setattr(cabinet_edit, "fetch_board", fake_fetch_board)

    link = "https://host/docs/?nd=1&infoboard=R0"
    questions = [{"question": "Тестовый вопрос про процедуры", "answers": ["Оставить А"]}]
    plans = await correct_cabinet("https://host", None, {}, link, questions, dry_run=True, qmap=QMAP)

    assert len(plans) == 1
    assert plans[0].action == "prune"          # цель R3 найдена, несмотря на битый RBAD
    assert plans[0].sub_id == "R3"
    assert set(plans[0].delete_headers) == {"Виджет Б", "Виджет В"}


@pytest.mark.asyncio
async def test_apply_calls_set_widgets_with_kept(monkeypatch):
    """Вне dry-run вызывается set_widgets с виджетами БЕЗ удаляемых, но С системными и выбранным."""
    _patch_tree(monkeypatch, TREE)
    captured = {}

    async def fake_set_widgets(base_url, client, cookies, board, widget_inputs):
        captured["board_id"] = board.id
        captured["headers"] = [w.get("header") for w in widget_inputs]
    monkeypatch.setattr(cabinet_edit, "set_widgets", fake_set_widgets)

    link = "https://host/docs/?nd=1&infoboard=R0"
    questions = [{"question": "Тестовый вопрос про процедуры", "answers": ["Оставить А"]}]

    await correct_cabinet("https://host", None, {}, link, questions, dry_run=False, qmap=QMAP)

    assert captured["board_id"] == "R3"
    assert set(captured["headers"]) == {"Виджет А", cabinet_edit.BACK_HEADER}  # Б и В удалены


# --- Снимок кабинета и восстановление виджетов при повторном опроснике ----------------


class StubSnapshotStore:
    """In-memory аналог CabinetSnapshotStore: эталон пишется один раз и не перезаписывается."""

    def __init__(self) -> None:
        self.data: dict[tuple[str, str], dict] = {}
        self.save_calls: list[str] = []

    async def save_if_absent(self, reg: str, board_id: str, title: str, config: dict) -> bool:
        self.save_calls.append(board_id)
        key = (reg, board_id)
        if key in self.data:
            return False
        self.data[key] = json.loads(json.dumps(config, ensure_ascii=False))
        return True

    async def get(self, reg: str, board_id: str) -> dict | None:
        value = self.data.get((reg, board_id))
        return json.loads(json.dumps(value, ensure_ascii=False)) if value is not None else None


@pytest.mark.asyncio
async def test_snapshot_taken_for_whole_tree(monkeypatch):
    """Эталон снимается со ВСЕХ бордов дерева, а не только с изменяемого, и в dry-run тоже."""
    _patch_tree(monkeypatch, TREE)
    store = StubSnapshotStore()

    await correct_cabinet(
        "https://host", None, {}, "https://host/docs/?nd=1&infoboard=R0",
        [{"question": "Тестовый вопрос про процедуры", "answers": ["Оставить А"]}],
        dry_run=True, qmap=QMAP, reg="465302", store=store,
    )

    assert sorted(store.save_calls) == ["R0", "R1", "R2", "R3"]
    assert [w["header"] for w in store.data[("465302", "R3")]["widgets"]] == [
        "Виджет А", "Виджет Б", "Виджет В", cabinet_edit.BACK_HEADER,
    ]


@pytest.mark.asyncio
async def test_snapshot_not_overwritten_on_second_pass(monkeypatch):
    """Повторный проход не перезаписывает эталон: снимок остаётся исходным."""
    _patch_tree(monkeypatch, TREE)
    monkeypatch.setattr(cabinet_edit, "set_widgets", _noop_set_widgets)
    store = StubSnapshotStore()
    link = "https://host/docs/?nd=1&infoboard=R0"
    question = [{"question": "Тестовый вопрос про процедуры", "answers": ["Оставить А"]}]

    await correct_cabinet("https://host", None, {}, link, question, qmap=QMAP, reg="465302", store=store)
    # второй проход идёт уже по УРЕЗАННОМУ дереву
    _patch_tree(monkeypatch, PRUNED_TREE)
    await correct_cabinet("https://host", None, {}, link, question, qmap=QMAP, reg="465302", store=store)

    assert len(store.data[("465302", "R3")]["widgets"]) == 4  # эталон не тронут


@pytest.mark.asyncio
async def test_second_pass_restores_deselected_widget(monkeypatch):
    """Клиент выбрал больше вариантов — ранее удалённый виджет возвращается с пустым widgetUuid."""
    _patch_tree(monkeypatch, PRUNED_TREE)
    store = StubSnapshotStore()
    store.data[("465302", "R3")] = {
        "id": "R3", "author": "a", "title": "Целевой под-кабинет",
        "searchString": None, "hiddenOnStartPage": True,
        "widgets": [w for w in TARGET_WIDGETS],
    }
    captured = {}

    async def fake_set_widgets(base_url, client, cookies, board, widget_inputs):
        captured["headers"] = [w.get("header") for w in widget_inputs]
        captured["uuids"] = {w.get("header"): w.get("widgetUuid") for w in widget_inputs}
    monkeypatch.setattr(cabinet_edit, "set_widgets", fake_set_widgets)

    plans = await correct_cabinet(
        "https://host", None, {}, "https://host/docs/?nd=1&infoboard=R0",
        [{"question": "Тестовый вопрос про процедуры", "answers": ["Оставить А", "Удалить Б"]}],
        qmap=QMAP, reg="465302", store=store,
    )

    assert plans[0].restore_headers == ["Виджет Б"]
    assert "Виджет Б" in captured["headers"]          # вернули
    assert "Виджет В" not in captured["headers"]      # остался удалённым
    assert captured["uuids"]["Виджет Б"] == ""        # новый виджет: uuid присвоит каталог
    assert captured["uuids"]["Виджет А"] == "u-Виджет А"  # существующий сохранил свой uuid


@pytest.mark.asyncio
async def test_restore_keeps_widget_added_after_snapshot(monkeypatch):
    """Виджет, добавленный в кабинет вручную после снимка, не теряется при уточнении."""
    tree = dict(PRUNED_TREE)
    tree["R3"] = BoardConfig(
        "R3", "a", "Целевой под-кабинет", None,
        [_w("Виджет А"), _w("Добавлен вручную"), _back()],
    )
    _patch_tree(monkeypatch, tree)
    store = StubSnapshotStore()
    store.data[("465302", "R3")] = {
        "id": "R3", "author": "a", "title": "Целевой под-кабинет",
        "searchString": None, "hiddenOnStartPage": None,
        "widgets": [w for w in TARGET_WIDGETS],
    }
    captured = {}

    async def fake_set_widgets(base_url, client, cookies, board, widget_inputs):
        captured["headers"] = [w.get("header") for w in widget_inputs]
    monkeypatch.setattr(cabinet_edit, "set_widgets", fake_set_widgets)

    await correct_cabinet(
        "https://host", None, {}, "https://host/docs/?nd=1&infoboard=R0",
        [{"question": "Тестовый вопрос про процедуры", "answers": ["Оставить А", "Удалить Б"]}],
        qmap=QMAP, reg="465302", store=store,
    )

    assert "Добавлен вручную" in captured["headers"]


@pytest.mark.asyncio
async def test_set_widgets_preserves_hidden_on_start_page(monkeypatch):
    """editConfig — полная замена конфига: флаг скрытия с главной ТЭ должен уходить обратно."""
    captured = {}

    async def fake_graphql(base_url, client, cookies, query, variables):
        captured["input"] = variables["ConfigInput"]
        return {}
    monkeypatch.setattr(cabinet_edit, "_graphql", fake_graphql)

    board = BoardConfig("R3", "kodeks", "Целевой", None, [], hidden_on_start_page=True)
    await cabinet_edit.set_widgets("https://host", None, {}, board, [])

    assert captured["input"]["hiddenOnStartPage"] is True
