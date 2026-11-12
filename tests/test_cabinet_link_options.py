from copy import deepcopy

import pytest

from common.exceptions import ParseError
from services.infoboards_service.cabinet_link_options import select_widget_links


def _link(title, href):
    return {"title": title, "href": href, "about": "Описание", "notForChange": False}


def test_links_restore_full_content_and_order_and_repeat_without_changes():
    first = _link("  Вариант\u00a0А ", "/a")
    second = _link("Вариант Б", "/b")
    third = _link("Вариант В", "/c")
    manual = _link("Ручная ссылка", "/manual")
    widget = {"widgetUuid": "existing-uuid", "header": "Выбор", "titleLink": {"href": "/entry"},
              "palette": {"colorWidget": "#abcdef"}, "links": [first, manual, second, third]}
    original = deepcopy(widget)
    options = {"вариант а", "вариант б", "вариант в"}

    filtered, archive = select_widget_links(widget, [], options, {"вариант а"})
    assert filtered == {**widget, "links": [first, manual]}
    assert archive == [{"link": second, "index": 2}, {"link": third, "index": 3}]
    assert select_widget_links(filtered, archive, options, {"вариант а"}) == (filtered, archive)

    new_manual = _link("Добавлена вручную", "/new-manual")
    filtered["links"].append(new_manual)
    restored, new_archive = select_widget_links(filtered, archive, options, {"вариант б"})
    assert restored == {**widget, "links": [manual, second, new_manual]}
    assert new_archive == [{"link": first, "index": 0}, {"link": third, "index": 3}]
    assert select_widget_links(restored, new_archive, options, options) == (
        {**widget, "links": [first, manual, second, third, new_manual]}, [],
    )
    assert widget == original


def test_normalization_and_existing_hidden_links_for_another_selection():
    first = _link("Ёщё\u00a0один Вариант", "/a")
    second = _link("Скрыто по другому вопросу", "/b")
    widget = {"header": "Работа", "links": [first]}
    archive = [{"link": second, "index": 0}]

    empty, archived = select_widget_links(widget, archive, {"еще один вариант"}, set())
    assert empty["links"] == []
    assert archived == [*archive, {"link": first, "index": 1}]
    assert select_widget_links(empty, archived, {"еще один вариант"}, {"еще один вариант"}) == (widget, archive)


@pytest.mark.parametrize("widget,archive,error_code", [
    ({"links": None}, [], "CABINET_LINK_OPTIONS_INVALID"),
    ({"links": [None]}, [], "CABINET_LINK_OPTIONS_INVALID"),
    ({"links": [{"title": 1}]}, [], "CABINET_LINK_OPTIONS_INVALID"),
    ({"links": []}, [{"link": {"title": "A"}, "index": True}], "CABINET_LINK_OPTIONS_INVALID"),
    ({"links": []}, [{"link": {"title": "A"}, "index": 1}], "CABINET_LINK_OPTIONS_INVALID"),
    ({"links": []}, [{"link": {"title": "A"}, "index": 0}, {"link": {"title": "B"}, "index": 0}],
     "CABINET_LINK_OPTIONS_INVALID"),
    ({"links": [_link("A", "/a"), _link(" a ", "/alias")]}, [], "CABINET_LINK_OPTION_AMBIGUOUS"),
    ({"links": [_link("A", "/a")]}, [{"link": _link("a", "/archive"), "index": 1}],
     "CABINET_LINK_OPTION_AMBIGUOUS"),
    ({"links": [_link("B", "/b")]}, [], "CABINET_LINK_OPTION_NOT_FOUND"),
])
def test_malformed_duplicate_and_missing_link_options_fail_closed(widget, archive, error_code):
    before = deepcopy((widget, archive))

    with pytest.raises(ParseError) as error:
        select_widget_links(widget, archive, {"a"}, set())

    assert error.value.code == error_code
    assert (widget, archive) == before


def test_selection_must_belong_to_complete_options():
    with pytest.raises(ParseError) as error:
        select_widget_links({"links": [_link("A", "/a")]}, [], {"a"}, {"b"})

    assert error.value.code == "CABINET_LINK_OPTIONS_INVALID"
