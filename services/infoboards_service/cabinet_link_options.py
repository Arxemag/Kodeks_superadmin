"""Apply one questionnaire selection to the links of a single native widget."""
from __future__ import annotations

from collections import Counter

from common.exceptions import ParseError


def _normalize_title(value: str) -> str:
    return " ".join(value.split()).casefold().replace("ё", "е")


def _invalid(message: str) -> ParseError:
    return ParseError(code="CABINET_LINK_OPTIONS_INVALID", message=message)


def _link_title(link: object) -> str:
    if not isinstance(link, dict) or not isinstance(link.get("title"), str):
        raise _invalid("A cabinet link must have a text title")
    return _normalize_title(link["title"])


def select_widget_links(
    widget: dict, archived: list[dict], options: set[str], selected: set[str],
) -> tuple[dict, list[dict]]:
    """Return the new widget and ordered archive without changing either input."""
    if not isinstance(widget, dict) or not isinstance(widget.get("links"), list):
        raise _invalid("A cabinet widget must contain a links list")
    if not isinstance(archived, list):
        raise _invalid("A cabinet link archive must be a list")
    if (not isinstance(options, (set, frozenset)) or not options
            or any(not isinstance(title, str) or not title or _normalize_title(title) != title for title in options)
            or not isinstance(selected, (set, frozenset)) or not selected <= options):
        raise _invalid("Invalid normalized options or selection for cabinet links")

    live = widget["links"]
    layout = [(link, _link_title(link), False) for link in live]
    hidden_by_index = {}
    size = len(live) + len(archived)
    for entry in archived:
        if not isinstance(entry, dict) or set(entry) != {"link", "index"}:
            raise _invalid("Invalid entry in the cabinet link archive")
        index = entry["index"]
        if type(index) is not int or not 0 <= index < size or index in hidden_by_index:
            raise _invalid("Invalid or duplicate index in the cabinet link archive")
        link = entry["link"]
        hidden_by_index[index] = (link, _link_title(link), True)
    for index, item in sorted(hidden_by_index.items()):
        layout.insert(index, item)

    matches = Counter(title for _, title, _ in layout if title in options)
    if any(count > 1 for count in matches.values()):
        raise ParseError(code="CABINET_LINK_OPTION_AMBIGUOUS", message="An option matches multiple links in the cabinet widget")
    if options - matches.keys():
        raise ParseError(code="CABINET_LINK_OPTION_NOT_FOUND", message="An option is absent from the cabinet widget and its archive")

    visible = []
    hidden = []
    for index, (link, title, was_hidden) in enumerate(layout):
        keep = title in selected if title in options else not was_hidden
        if keep:
            visible.append(link)
        else:
            hidden.append({"link": link, "index": index})
    return {**widget, "links": visible}, hidden
