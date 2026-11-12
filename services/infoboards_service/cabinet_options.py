"""Resolve questionnaire options to native widget headers or link titles."""
from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
from functools import lru_cache
import json
from pathlib import Path

from common.exceptions import ParseError


def normalize_title(value: str | None) -> str:
    return " ".join((value or "").split()).casefold().replace("ё", "е")


SELECT_ALL = normalize_title("Выбрать всё")


def question_key(title: str) -> str:
    return normalize_title(title).removesuffix("(можно выбрать несколько)").strip()


@lru_cache(maxsize=1)
def _legacy_profiles():
    path = Path(__file__).with_name("cabinet_questionnaire_legacy.json")
    data = json.loads(path.read_text(encoding="utf-8"))
    return {
        normalize_title(root): {question_key(item["question"]): item for item in questions}
        for root, questions in data.items()
    }


def legacy_question(root_title, question):
    return _legacy_profiles().get(normalize_title(root_title), {}).get(question_key(question))


@dataclass(frozen=True)
class OptionSelection:
    board: str
    allowed: frozenset[str]
    selected: frozenset[str]
    link_widget: str | None = None


def _invalid(message):
    return ParseError(code="CABINET_PLAN_INVALID", message=message)


def _labels(values):
    if not isinstance(values, list) or any(not isinstance(value, str) or not value.strip() for value in values):
        raise _invalid("Cabinet option labels must be nonempty strings")
    labels = {normalize_title(value) for value in values}
    if len(labels) != len(values):
        raise _invalid("Duplicate cabinet option label")
    return labels


def resolve_options(item, legacy, boards, archives, protected):
    explicit = item.get("options") is not None
    config = item if explicit else legacy
    if config is None:
        raise _invalid("Question does not identify cabinet options")
    options = _labels(config["options"]) - {SELECT_ALL}
    chosen = _labels(item["answers"])
    if not options or (chosen != {SELECT_ALL} and not chosen <= options):
        raise _invalid("The selected answers do not match the available cabinet options")
    if chosen == {SELECT_ALL}:
        chosen = options
    aliases = {normalize_title(source): normalize_title(target)
               for source, target in config.get("aliases", {}).items()}
    allowed = frozenset(aliases.get(label, label) for label in options)
    selected = frozenset(aliases.get(label, label) for label in chosen)
    if len(allowed) != len(options) or allowed & protected:
        raise _invalid("Cabinet option labels overlap or include a system widget")

    toggles = config.get("toggle", {})
    if toggles:
        if len(chosen) != 1:
            raise _invalid("A yes/no question requires exactly one answer")
        resolved = []
        for title, header in toggles.items():
            label = normalize_title(header)
            matches = _find_targets(boards, archives, {label}, {normalize_title(title)}, None)
            if len(matches) != 1 or label in protected:
                raise _invalid(f"Cannot resolve the cabinet toggle {header!r}")
            resolved.append(OptionSelection(matches[0], frozenset({label}),
                                            frozenset({label}) if chosen == {"да"} else frozenset()))
        return resolved

    block_titles = _labels(config["blocks"]) if config.get("blocks") is not None else set()
    raw_link_widget = config.get("linkWidget") if explicit else config.get("link_widget")
    link_widget = normalize_title(raw_link_widget) if raw_link_widget is not None else None
    if raw_link_widget is not None and (not link_widget or link_widget in protected):
        raise _invalid("Invalid links widget label")
    matches = _find_targets(boards, archives, allowed, block_titles, link_widget)
    found_titles = {normalize_title(boards[bid].title) for bid in matches}
    if not matches or block_titles - found_titles or (
        not explicit and len(matches) != config["copies"]
    ):
        raise _invalid(f"Cannot resolve all blocks for question {item['question']!r}")
    return [OptionSelection(bid, allowed, selected, link_widget) for bid in matches]


def _find_targets(boards, archives, allowed, block_titles, link_widget):
    matches = []
    for bid, board in boards.items():
        if block_titles and normalize_title(board.title) not in block_titles:
            continue
        archive = archives[bid]
        live_ids = {widget.get("widgetUuid") for widget in board.widgets}
        widgets = board.widgets + [entry["widget"] for entry in archive
                                   if "widget" in entry and entry["widget"].get("widgetUuid") not in live_ids]
        if link_widget is None:
            counts = Counter(normalize_title(widget.get("header")) for widget in widgets
                             if not widget.get("isSpecial"))
        else:
            targets = [widget for widget in board.widgets if normalize_title(widget.get("header")) == link_widget]
            if not targets:
                continue
            if len(targets) != 1 or targets[0].get("isSpecial"):
                raise _invalid(f"Ambiguous links widget in cabinet {bid!r}")
            target = targets[0]
            links = (target.get("links") or []) + [
                hidden["link"] for entry in archive if entry.get("widgetUuid") == target.get("widgetUuid")
                for hidden in entry["links"]
            ]
            counts = Counter(normalize_title(link.get("title")) for link in links)
        if allowed <= counts.keys():
            if any(counts[label] != 1 for label in allowed):
                raise _invalid(f"Ambiguous option labels in cabinet {bid!r}")
            matches.append(bid)
    return matches
