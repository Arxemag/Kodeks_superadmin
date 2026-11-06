"""Classify one native GraphQL operation before the gateway forwards it."""
from __future__ import annotations

from dataclasses import dataclass
import json
import re
from urllib.parse import parse_qsl, urlencode

from graphql import GraphQLError, parse
from graphql.language import OperationType
from graphql.language.ast import (
    DirectiveNode, FieldNode, FragmentDefinitionNode, Node, NullValueNode,
    ObjectValueNode, OperationDefinitionNode, StringValueNode, VariableNode,
)

from common.exceptions import ParseError


MAX_GRAPHQL_BODY = 1024 * 1024
MAX_GRAPHQL_PARAMS = 64 * 1024
MAX_GRAPHQL_TOKENS = 10_000
_REQUEST_KEYS = {"query", "variables", "operationName", "extensions"}
_BOARD_ID = re.compile(r"[A-Za-z0-9_-]{1,128}")
_BOARD_UUID = re.compile(r"[0-9a-fA-F]{8}(?:-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12}")
_BAD_ESCAPE = re.compile(r"%(?![0-9a-fA-F]{2})")


@dataclass(frozen=True)
class GraphqlRequest:
    body: bytes
    params: str
    action: str
    board_id: str | None = None
    board_uuid: str | None = None


def _invalid(message: str, *, code: str = "GRAPHQL_REQUEST_INVALID") -> ParseError:
    return ParseError(code=code, message=message, http_status=400)


def _object(pairs) -> dict:
    result = {}
    for key, value in pairs:
        if key in result:
            raise _invalid("Duplicate GraphQL request field")
        result[key] = value
    return result


def _nonfinite(_value: str):
    raise _invalid("Non-finite JSON number in GraphQL request")


def _decode_json(raw: str):
    try:
        return json.loads(raw, object_pairs_hook=_object, parse_constant=_nonfinite)
    except (ValueError, RecursionError) as error:
        raise _invalid("Invalid GraphQL request JSON") from error


def _encode_json(data) -> bytes:
    try:
        return json.dumps(data, ensure_ascii=False, allow_nan=False, separators=(",", ":")).encode("utf-8")
    except (ValueError, UnicodeError, RecursionError) as error:
        raise _invalid("Invalid JSON value in GraphQL request") from error


def _params(raw: str | bytes) -> dict[str, str]:
    try:
        if isinstance(raw, str):
            raw = raw.encode("utf-8")
        if len(raw) > MAX_GRAPHQL_PARAMS:
            raise _invalid("GraphQL query string exceeds the size limit")
        value = raw.decode("utf-8")
        if _BAD_ESCAPE.search(value):
            raise _invalid("Invalid URL encoding in GraphQL request")
        return _object(parse_qsl(value, keep_blank_values=True, strict_parsing=True,
                                 encoding="utf-8", errors="strict", max_num_fields=16))
    except (ValueError, UnicodeError) as error:
        raise _invalid("Invalid GraphQL query string") from error


def _unique(names) -> None:
    seen = set()
    for name in names:
        if name in seen:
            raise _invalid("Duplicate name in GraphQL operation")
        seen.add(name)


def _parse_operation(query: str, operation_name: str | None) -> OperationDefinitionNode:
    try:
        document = parse(query, no_location=True, max_tokens=MAX_GRAPHQL_TOKENS)
    except (GraphQLError, RecursionError) as error:
        raise _invalid("Malformed or oversized GraphQL operation") from error
    operations = [node for node in document.definitions if isinstance(node, OperationDefinitionNode)]
    if len(operations) != 1 or any(not isinstance(node, (OperationDefinitionNode, FragmentDefinitionNode))
                                   for node in document.definitions):
        raise _invalid("Exactly one GraphQL operation is required")
    operation = operations[0]
    if operation_name is not None and (operation.name is None or operation.name.value != operation_name):
        raise _invalid("GraphQL operationName does not match the operation")
    _unique(node.name.value for node in document.definitions if isinstance(node, FragmentDefinitionNode))
    _unique(node.variable.name.value for node in operation.variable_definitions or ())

    # GraphQL parsing checks syntax. The gateway also needs unique names before
    # using input values, so a later validator cannot select a different value.
    stack: list[Node] = [document]
    while stack:
        node = stack.pop()
        if isinstance(node, (FieldNode, DirectiveNode)):
            _unique(argument.name.value for argument in node.arguments)
        if isinstance(node, ObjectValueNode):
            _unique(field.name.value for field in node.fields)
        for key in node.keys:
            value = getattr(node, key)
            if isinstance(value, Node):
                stack.append(value)
            elif isinstance(value, tuple):
                stack.extend(item for item in value if isinstance(item, Node))
    return operation


def _resolve(value, variables: dict, declarations: dict):
    if not isinstance(value, VariableNode):
        return value
    name = value.name.value
    if name not in declarations:
        raise _invalid("Mutation uses an undeclared variable")
    return variables[name] if name in variables else declarations[name]


def _target(value, variables: dict, declarations: dict, *, optional_uuid: bool = False) -> str | None:
    value = _resolve(value, variables, declarations)
    if isinstance(value, StringValueNode):
        value = value.value
    if optional_uuid and (value is None or isinstance(value, NullValueNode)):
        return None
    pattern = _BOARD_UUID if optional_uuid else _BOARD_ID
    if not isinstance(value, str) or not pattern.fullmatch(value):
        raise _invalid("Invalid cabinet UUID" if optional_uuid else "Invalid cabinet ID")
    return value.lower() if optional_uuid else value


def _classify(method: str, operation: OperationDefinitionNode, variables: dict) -> tuple[str, str | None, str | None]:
    if operation.operation is OperationType.QUERY:
        return "query", None, None
    if method != "POST" or operation.operation is not OperationType.MUTATION:
        raise _invalid("GraphQL operation is not permitted with this request", code="GRAPHQL_MUTATION_DENIED")
    selections = operation.selection_set.selections
    if len(selections) != 1 or not isinstance(selections[0], FieldNode):
        raise _invalid("A mutation must contain exactly one direct field", code="GRAPHQL_MUTATION_DENIED")
    field = selections[0]
    action = field.name.value
    arguments = {argument.name.value: argument.value for argument in field.arguments}
    declarations = {node.variable.name.value: node.default_value for node in operation.variable_definitions or ()}
    if action == "editConfig":
        if arguments.keys() != {"input"}:
            raise _invalid("editConfig requires one input argument")
        value = _resolve(arguments["input"], variables, declarations)
        if isinstance(value, ObjectValueNode):
            value = {item.name.value: item.value for item in value.fields}
        if not isinstance(value, dict):
            raise _invalid("editConfig input must be an object")
        return action, _target(value.get("id"), variables, declarations), _target(
            value.get("boardUuid"), variables, declarations, optional_uuid=True,
        )
    if action == "deleteConfig":
        if arguments.keys() != {"id"}:
            raise _invalid("deleteConfig requires one id argument")
        return action, _target(arguments["id"], variables, declarations), None
    if action == "sendRedirectAction":
        return action, None, None
    raise _invalid("Unsupported GraphQL mutation", code="GRAPHQL_MUTATION_DENIED")


def parse_graphql_request(method: str, raw_body: bytes, query_string: str | bytes) -> GraphqlRequest:
    """Parse and re-encode a request so the native service sees the classified operation."""
    method = method.upper()
    params = _params(query_string)
    if params.get("context", "docs") != "docs":
        raise _invalid("Invalid GraphQL context")
    if method == "POST":
        if params.keys() - {"context"}:
            raise _invalid("POST GraphQL parameters must be in the JSON body")
        if len(raw_body) > MAX_GRAPHQL_BODY:
            raise _invalid("GraphQL body exceeds the size limit")
        try:
            data = _decode_json(raw_body.decode("utf-8"))
        except UnicodeError as error:
            raise _invalid("GraphQL body must be UTF-8 JSON") from error
    elif method == "GET":
        if raw_body or params.keys() - (_REQUEST_KEYS | {"context"}):
            raise _invalid("Invalid GET GraphQL request")
        data = {key: value for key, value in params.items() if key != "context"}
        for key in ("variables", "extensions"):
            if key in data:
                data[key] = _decode_json(data[key])
        params = {key: value for key, value in params.items() if key == "context"}
    else:
        raise _invalid("Unsupported GraphQL request method")

    if not isinstance(data, dict) or data.keys() - _REQUEST_KEYS:
        raise _invalid("GraphQL request must be one JSON object")
    query = data.get("query")
    name = data.get("operationName")
    variables = data.get("variables")
    if not isinstance(query, str) or not query.strip() or (name is not None and not isinstance(name, str)):
        raise _invalid("An explicit GraphQL query and a valid operationName are required")
    if variables is not None and not isinstance(variables, dict):
        raise _invalid("GraphQL variables must be an object")
    if data.get("extensions") is not None and not isinstance(data["extensions"], dict):
        raise _invalid("GraphQL extensions must be an object")

    # The explicit document is authoritative. Extensions such as persisted
    # queries could otherwise make the native service execute a different one.
    data.pop("extensions", None)
    canonical = _encode_json(data)
    operation = _parse_operation(query, name)
    action, board_id, board_uuid = _classify(method, operation, variables or {})
    if method == "GET":
        params["query"] = query
        if name is not None:
            params["operationName"] = name
        if "variables" in data:
            params["variables"] = _encode_json(variables).decode("utf-8")
        canonical = b""
    return GraphqlRequest(canonical, urlencode(params), action, board_id, board_uuid)
