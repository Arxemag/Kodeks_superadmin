"""Parse catalog forms without treating an unrecognised page as an empty catalog.

The parser recognises form structure, not HTTP success. A blank uid is exposed to
callers as an empty form; it does not prove that a requested user is absent.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from html.parser import HTMLParser
import unicodedata
from urllib.parse import parse_qs, urlsplit

from common.exceptions import ParseError


_TRACKED_FIELDS = {"uid", "psw", "name", "org", "pos", "mail", "telephon", "end", "id"}
_VOID = {"area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta", "param", "source", "track", "wbr"}


@dataclass
class _Node:
    tag: str
    attrs: dict[str, str | None] = field(default_factory=dict)
    children: list[_Node | str] = field(default_factory=list)
    parent: _Node | None = field(default=None, repr=False)

    def walk(self):
        yield self
        for child in self.children:
            if isinstance(child, _Node):
                yield from child.walk()

    def text(self) -> str:
        return "".join(child if isinstance(child, str) else child.text() for child in self.children)


class _Document(HTMLParser):
    def __init__(self, source: str):
        super().__init__(convert_charrefs=True)
        self.root = _Node("document")
        self.stack = [self.root]
        self.feed(source)
        self.close()

    def handle_starttag(self, tag, attrs):
        # HTML permits omission of </option> before the next option/optgroup.
        if tag in {"option", "optgroup"} and self.stack[-1].tag == "option":
            self.stack.pop()
        if tag == "optgroup" and self.stack[-1].tag == "optgroup":
            self.stack.pop()
        node = _Node(tag, dict(attrs), parent=self.stack[-1])
        self.stack[-1].children.append(node)
        if tag not in _VOID:
            self.stack.append(node)

    def handle_startendtag(self, tag, attrs):
        self.handle_starttag(tag, attrs)
        if tag not in _VOID:
            self.handle_endtag(tag)

    def handle_endtag(self, tag):
        for index in range(len(self.stack) - 1, 0, -1):
            if self.stack[index].tag == tag:
                del self.stack[index:]
                break

    def handle_data(self, data):
        self.stack[-1].children.append(data)


def normalize_title(value: str) -> str:
    """Normalise text already decoded from HTML; do not strip punctuation or ё."""
    return " ".join(unicodedata.normalize("NFC", value).casefold().split())


def group_index(groups: dict[str, int]) -> dict[str, int]:
    """Index by normalised title, rejecting two different IDs for one title."""
    result: dict[str, int] = {}
    for title, group_id in groups.items():
        key = normalize_title(title)
        if not key:
            raise ParseError(code="HTML_PARSE_ERROR", message="Group title is empty")
        if key in result and result[key] != group_id:
            raise ParseError(code="AMBIGUOUS_GROUP", message=f"Several group IDs match title {title!r}")
        result[key] = group_id
    return result


def _ancestor(node: _Node, tag: str) -> _Node | None:
    parent = node.parent
    while parent is not None:
        if parent.tag == tag:
            return parent
        parent = parent.parent
    return None


def _inside(node: _Node, ancestor: _Node) -> bool:
    while node is not None:
        if node is ancestor:
            return True
        node = node.parent
    return False


def _disabled(node: _Node) -> bool:
    if "disabled" in node.attrs:
        return True
    parent = node.parent
    while parent is not None:
        if parent.tag == "optgroup" and "disabled" in parent.attrs:
            return True
        if parent.tag == "fieldset" and "disabled" in parent.attrs:
            legend = next((c for c in parent.children if isinstance(c, _Node) and c.tag == "legend"), None)
            if legend is None or not _inside(node, legend):
                return True
        parent = parent.parent
    return False


def _form_controls(root: _Node, form: _Node) -> list[_Node]:
    controls = []
    for node in root.walk():
        if node.tag not in {"input", "select", "textarea", "button"}:
            continue
        if "form" in node.attrs:
            belongs = bool(form.attrs.get("id")) and node.attrs["form"] == form.attrs["id"]
        else:
            owner = _ancestor(node, "form")
            belongs = owner is form or (form is root and owner is None)
        if belongs:
            controls.append(node)
    return controls


def _successful(controls: list[_Node]) -> list[tuple[str, str]]:
    pairs = []
    for node in controls:
        name = node.attrs.get("name")
        if not name or _disabled(node):
            continue
        if node.tag == "button":
            continue
        if node.tag == "textarea":
            pairs.append((name, node.text()))
        elif node.tag == "select":
            options = [o for o in node.walk() if o.tag == "option"]
            if "multiple" in node.attrs:
                chosen = [o for o in options if "selected" in o.attrs and not _disabled(o)]
            else:
                selected = [o for o in options if "selected" in o.attrs]
                chosen = selected[-1:] if selected else next(([o] for o in options if not _disabled(o)), [])
                chosen = [o for o in chosen if not _disabled(o)]
            pairs.extend((name, o.attrs.get("value") if o.attrs.get("value") is not None else " ".join(o.text().split())) for o in chosen)
        else:
            kind = (node.attrs.get("type") or "text").lower()
            if kind in {"submit", "button", "reset", "image", "file"}:
                continue  # No submitter is clicked; files are not URL-encoded fields.
            if kind in {"checkbox", "radio"} and "checked" not in node.attrs:
                continue
            default = "on" if kind in {"checkbox", "radio"} else ""
            pairs.append((name, node.attrs.get("value") if node.attrs.get("value") is not None else default))
    return pairs


def _save_submitter(controls: list[_Node]) -> list[tuple[str, str]]:
    """Retain one actual save button; reject ambiguity instead of guessing."""
    candidates = []
    for node in controls:
        kind = (node.attrs.get("type") or ("submit" if node.tag == "button" else "text")).lower()
        if node.tag not in {"input", "button"} or kind != "submit" or _disabled(node):
            continue
        name = node.attrs.get("name")
        value = node.attrs.get("value") or ""
        label = normalize_title(value or node.text())
        if name in {"set", "cmd"} or any(word in label for word in ("сохран", "создат", "добав", "save", "create", "add")):
            candidates.append((name, value))
    if len(candidates) > 1:
        raise ParseError(code="HTML_PARSE_ERROR", message="Several save submitters were found")
    return [(name, value) for name, value in candidates if name]


@dataclass(frozen=True)
class UserState:
    fields: dict[str, str]
    groups: list[int]
    form_data: list[tuple[str, str]] = field(default_factory=list)
    is_empty_form: bool = False
    action: str = ""
    method: str = "post"


def parse_user_state(page_html: str) -> UserState:
    root = _Document(page_html).root
    forms = [n for n in root.walk() if n.tag == "form"]
    candidates = []
    for form in forms or [root]:
        if _ancestor(form, "form") is not None:
            raise ParseError(code="HTML_PARSE_ERROR", message="Nested user forms are unsupported")
        controls = _form_controls(root, form)
        names = {n.attrs.get("name") for n in controls if not _disabled(n)}
        if {"uid", "psw"} <= names:
            action = (form.attrs.get("action") or "").casefold()
            if any(part in urlsplit(action).path.split("/") for part in ("login", "signin", "sign-in", "auth")):
                continue
            candidates.append((form, controls))
    if len(candidates) != 1:
        raise ParseError(code="HTML_PARSE_ERROR", message="A unique user form with uid and psw was not found")
    form, controls = candidates[0]
    pairs = _successful(controls) + _save_submitter(controls)
    fields: dict[str, str] = {}
    groups = []
    for name, value in pairs:
        if name in _TRACKED_FIELDS:
            if name in fields and fields[name] != value:
                raise ParseError(code="HTML_PARSE_ERROR", message=f"Conflicting values of user field {name!r}")
            fields[name] = value
        if name == "grp":
            try:
                group_id = int(value)
                if group_id < 0:
                    raise ValueError
                groups.append(group_id)
            except ValueError as error:
                raise ParseError(code="HTML_PARSE_ERROR", message=f"Invalid group value in HTML: {value!r}") from error
    if not {"uid", "psw"} <= fields.keys():
        raise ParseError(code="HTML_PARSE_ERROR", message="Successful uid and psw controls were not found")
    return UserState(fields, sorted(set(groups)), pairs, not fields["uid"].strip(),
                     form.attrs.get("action") or "", (form.attrs.get("method") or "post").lower())


def parse_groups_catalog(page_html: str) -> dict[str, int]:
    root = _Document(page_html).root
    pairs: list[tuple[str, int]] = []
    recognised = False
    for node in root.walk():
        if node.tag == "a":
            target = urlsplit(node.attrs.get("href") or "")
            query = parse_qs(target.query)
            rel = (node.attrs.get("rel") or "").lower().split()
            is_group_page = target.path.rstrip("/").split("/")[-1].lower() == "groups"
            if "next" in rel or (is_group_page and any(key.lower() in {"page", "offset", "start", "page_size", "pagenum"} for key in query)):
                raise ParseError(code="GROUP_LIST_PAGINATED", message="Catalog group list may be incomplete; pagination must be read before writes")
            if target.path.rstrip("/").split("/")[-1] != "grp" or "grp" not in query:
                continue
            recognised = True
            raw_id = query["grp"][0]
        elif node.tag == "option":
            select = _ancestor(node, "select")
            if select is None or select.attrs.get("name") != "grp":
                continue
            recognised = True
            raw_id = node.attrs.get("value") or ""
            if not raw_id and not node.text().strip():
                continue
        else:
            if node.tag == "select" and node.attrs.get("name") == "grp":
                recognised = True
            if node.tag == "form":
                names = {n.attrs.get("name") for n in _form_controls(root, node)}
                if {"name", "gn"} <= names:
                    recognised = True
            if "data-groups-catalog" in node.attrs:
                recognised = True  # Explicit marker for adapter fixtures/known catalogue markup.
            continue
        title = node.text().strip()
        try:
            group_id = int(raw_id)
            if group_id < 0 or not title:
                raise ValueError
        except (ValueError, TypeError) as error:
            raise ParseError(code="HTML_PARSE_ERROR", message="Malformed group entry") from error
        pairs.append((title, group_id))
    if not recognised:
        raise ParseError(code="HTML_PARSE_ERROR", message="Group catalogue markup was not found")
    groups: dict[str, int] = {}
    normalised: dict[str, int] = {}
    for title, group_id in pairs:
        key = normalize_title(title)
        if key in normalised and normalised[key] != group_id:
            raise ParseError(code="AMBIGUOUS_GROUP", message=f"Several group IDs match title {title!r}")
        normalised[key] = group_id
        groups[title] = group_id
    return groups


def parse_group_creation_form(page_html: str) -> list[tuple[str, str]]:
    """Return the one recognised group form without discarding hidden/default fields.

    The actual named save submitter is included, when identifiable.
    The adapter must validate its action before posting these controls.
    """
    return parse_group_creation_details(page_html)[0]


def parse_group_creation_details(page_html: str) -> tuple[list[tuple[str, str]], str, str]:
    """Return controls and the real submission target of the unique group form."""
    root = _Document(page_html).root
    candidates = []
    for form in (node for node in root.walk() if node.tag == "form"):
        if _ancestor(form, "form") is not None:
            raise ParseError(code="HTML_PARSE_ERROR", message="Nested group forms are unsupported")
        controls = _form_controls(root, form)
        pairs = _successful(controls) + _save_submitter(controls)
        names = {name for name, _ in pairs}
        if {"name", "gn"} <= names:
            candidates.append((pairs, form.attrs.get("action") or "", (form.attrs.get("method") or "post").lower()))
    if len(candidates) != 1:
        raise ParseError(code="HTML_PARSE_ERROR", message="A unique group creation form with name and gn was not found")
    return candidates[0]
