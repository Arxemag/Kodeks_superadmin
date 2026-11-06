import json
from urllib.parse import parse_qs, urlencode

import pytest

from common.exceptions import ParseError
from services.auth_service import graphql_request as requests


UUID = "019E1234-CB81-7350-B0D7-48E650CF7D72"
EDIT = "mutation EditConfigWidgets($ConfigInput: ConfigInput) { editConfig(input: $ConfigInput) { id title } }"


@pytest.mark.parametrize("target", [{}, {"id": None}])
def test_native_edit_config_without_target_requires_create_authorization(target):
    result = _post(EDIT, variables={"ConfigInput": {"title": "Новый кабинет", **target}})
    assert (result.action, result.board_id, result.board_uuid) == ("createConfig", None, None)


def _post(query, **payload):
    body = json.dumps({"query": query, **payload}).encode()
    return requests.parse_graphql_request("POST", body, "context=docs")


def test_native_edit_config_variable_payload_preserves_full_widget_input():
    config = {"id": "P012O", "boardUuid": UUID, "title": "Мой кабинет", "widgets": '[{"widgetUuid":"existing"}]'}
    result = _post(EDIT, variables={"ConfigInput": config}, operationName="EditConfigWidgets")

    assert result.action == "editConfig"
    assert result.board_id == "P012O" and result.board_uuid == UUID.lower()
    assert result.params == "context=docs"
    assert json.loads(result.body)["variables"] == {"ConfigInput": config}


def test_aliased_edit_literal_input_resolves_declared_variable_defaults():
    result = _post('mutation Update($target: String = "P012O") { result: editConfig(input: '
                   '{id:$target, boardUuid:null, title:"Мой кабинет"}) { id } }',
                   variables=None, operationName=None)

    assert (result.action, result.board_id, result.board_uuid) == ("editConfig", "P012O", None)


def test_aliased_delete_and_native_redirect_action():
    delete = _post('mutation($id:String){deleted: deleteConfig(id:$id){id title}}', variables={"id": "P013K"})
    redirect = _post('mutation Jump($payload: RedirectActionPayload) {'
                     'sendRedirectAction(redirectActionPayload:$payload)}', variables={"payload": {"id": "P012O"}})

    assert (delete.action, delete.board_id, delete.board_uuid) == ("deleteConfig", "P013K", None)
    assert (redirect.action, redirect.board_id, redirect.board_uuid) == ("sendRedirectAction", None, None)


@pytest.mark.parametrize("method", ["GET", "POST"])
def test_query_fragments_and_aliases_are_preserved(method):
    query = 'query Read($id:String!){ alias: board { one(id:$id) { ...Cabinet } } } fragment Cabinet on Board { id title }'
    data = {"query": query, "operationName": "Read", "variables": {"id": "P012O"}}
    if method == "POST":
        result = _post(**data)
        assert json.loads(result.body) == data
    else:
        params = urlencode({**data, "variables": json.dumps(data["variables"]), "context": "docs"})
        result = requests.parse_graphql_request(method, b"", params.encode())
        assert result.body == b""
        assert parse_qs(result.params) == {"query": [query], "operationName": ["Read"],
                                          "variables": ['{"id":"P012O"}'], "context": ["docs"]}
    assert (result.action, result.board_id, result.board_uuid) == ("query", None, None)


def test_extensions_do_not_override_explicit_document():
    extensions = {"persistedQuery": {"version": 1, "sha256Hash": "0" * 64}}
    query = "# mutation is just a comment\n{ board { many { id } } }"
    result = _post(query, extensions=extensions)

    assert result.action == "query"
    assert json.loads(result.body) == {"query": query}
    with pytest.raises(ParseError):
        requests.parse_graphql_request("POST", json.dumps({"extensions": extensions}).encode(), "context=docs")


@pytest.mark.parametrize("query,variables", [
    ('query Read{board{many{id}}} mutation Write{deleteConfig(id:"P1"){id}}', None),
    ('query A{board{many{id}}} query B{board{many{id}}}', None),
    ('subscription{board{id}}', None),
    ('mutation{editConfig: hiddenWrite(id:"P1"){id}}', None),
    ('mutation{editConfig(input:{id:"P1"}){id} deleteConfig(id:"P2"){id}}', None),
    ('mutation{ ...Write } fragment Write on Mutation { deleteConfig(id:"P1"){id} }', None),
    ('mutation{ ... on Mutation { deleteConfig(id:"P1"){id} } }', None),
    ('mutation{editConfig(input:{id:"P1",id:"P2"}){id}}', None),
    ('mutation{editConfig(input:{id:"P1", extra:{x:1,x:2}}){id}}', None),
    ('mutation{editConfig(input:{id:"P1"},input:{id:"P2"}){id}}', None),
    ('mutation M($x:String,$x:String){deleteConfig(id:$x){id}}', {"x": "P1"}),
    ('mutation{deleteConfig(id:$missing){id}}', {"missing": "P1"}),
    ('mutation{deleteConfig(id:"P1",boardUuid:"other"){id}}', None),
    ('mutation{deleteConfig(id:P1){id}}', None),
    ('mutation{editConfig(input:{id:"../admin"}){id}}', None),
    ('mutation{editConfig(input:{id:"P1",boardUuid:"different parser"}){id}}', None),
    ('mutation($input:ConfigInput){editConfig(input:$input){id}}', {"input": [{"id": "P1"}]}),
    ('{ board { one(id:"P1", id:"P2") { id } } }', None),
])
def test_rejects_ambiguous_and_unsupported_operations(query, variables):
    with pytest.raises(ParseError) as error:
        _post(query, variables=variables)
    assert error.value.http_status == 400


@pytest.mark.parametrize("raw", [
    b'[{"query":"{board{many{id}}}"}]',
    b'{"query":"{board{many{id}}}","query":"mutation{deleteConfig(id:\\"P1\\"){id}}"}',
    b'{"query":"{board{many{id}}}","variables":{},"variables":null}',
    b'{"query":"{board{many{id}}}","variables":{"input":{"id":"P1","\\u0069d":"P2"}}}',
    b'{"query":"{board{many{id}}}","variables":{"x":NaN}}',
    b'{"query":"{board{many{id}}}","variables":{"x":1e999}}',
    b'{"query":"{board{many{id}}}","operationName":true}',
    b'{"query":"{board{many{id}}}","extensions":[]}',
    b'{"query":"{board{many{id}}}","document":"ignored"}',
    b'\xff',
])
def test_rejects_invalid_or_ambiguous_json(raw):
    with pytest.raises(ParseError) as error:
        requests.parse_graphql_request("POST", raw, "context=docs")
    assert error.value.http_status == 400


def test_operation_name_must_select_the_only_operation():
    with pytest.raises(ParseError, match="operationName does not match"):
        _post("query Read{board{many{id}}}", operationName="Write")


@pytest.mark.parametrize("method,body,params", [
    ("GET", b"", urlencode({"query": 'mutation{deleteConfig(id:"P1"){id}}', "context": "docs"})),
    ("GET", b'{"query":"{board{many{id}}}"}', "query=%7Bboard%7Bmany%7Bid%7D%7D%7D"),
    ("GET", b"", "query=%7Bboard%7Bmany%7Bid%7D%7D%7D&%71uery=another"),
    ("POST", b'{"query":"{board{many{id}}}"}', "context=docs&query=override"),
    ("POST", b'{"query":"{board{many{id}}}"}', "context=docs&context=docs"),
    ("POST", b'{"query":"{board{many{id}}}"}', "context=admin"),
    ("POST", b'{"query":"{board{many{id}}}"}', "context=%XX"),
])
def test_transport_cannot_supply_an_alternate_operation(method, body, params):
    with pytest.raises(ParseError) as error:
        requests.parse_graphql_request(method, body, params)
    assert error.value.http_status == 400


def test_body_url_and_ast_limits_are_deterministic(monkeypatch):
    monkeypatch.setattr(requests, "MAX_GRAPHQL_BODY", 4)
    with pytest.raises(ParseError, match="body exceeds"):
        _post("{board{many{id}}}")
    monkeypatch.setattr(requests, "MAX_GRAPHQL_PARAMS", 2)
    with pytest.raises(ParseError, match="query string exceeds"):
        requests.parse_graphql_request("GET", b"", "query=%7Ba%7D")
    monkeypatch.setattr(requests, "MAX_GRAPHQL_PARAMS", 4096)
    monkeypatch.setattr(requests, "MAX_GRAPHQL_TOKENS", 5)
    with pytest.raises(ParseError, match="oversized GraphQL operation"):
        requests.parse_graphql_request("GET", b"", urlencode({"query": "{board{many{id}}}"}))
