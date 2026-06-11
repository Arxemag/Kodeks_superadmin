"""
Тесты редактирования кабинета по опроснику (cabinet_edit.correct_cabinet):
обход дерева ВГЛУБЬ (цель на несколько уровней ниже родителя), защита от неоднозначности,
неприкосновенность системных виджетов, реальное применение (set_widgets) вне dry-run.
"""
from __future__ import annotations

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
