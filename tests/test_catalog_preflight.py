"""Read-only diagnostics must not mutate catalog data or leak form values."""
import json

import httpx
import pytest

from scripts.catalog_preflight import check_catalog, docs_directory_number, form_schema, main, run_preflight
from common.exceptions import ParseError


GROUPS = '''<a href="grp?grp=7">Ecology</a><form action="../users/grp " method="POST">
<input type="SUBMIT" value="Create group"></form>'''
GROUP_CREATE = '''<form action="../users/groups" method="POST">
<input name="name"><input name="gn"><input name="cmd"><input name="private" value="DO_NOT_PRINT">
<input type="SUBMIT" value="Save"></form>'''
USER = '''<form action="users"><input name="uid" value=""><input name="psw" value="SECRET">
<input name="token" value="DO_NOT_PRINT"><input name="repeat" value="a"><input name="repeat" value="b"></form>'''
ACL = '''<form action="dirs"><input name="acl_infoboard_p1" value='{&quot;kw&quot;:[&quot;7&quot;]}'>
<input name="acl_infoboard_p1_view" value='{&quot;kw&quot;:[&quot;7&quot;]}'></form>'''


def transport_handler(requests, *, groups=GROUPS, group_create=GROUP_CREATE, user=USER, acl=ACL, boards=None):
    def handler(request):
        requests.append(request)
        path = request.url.path
        if path == "/users/groups":
            return httpx.Response(200, text=groups)
        if path == "/users/grp":
            assert request.method == "GET" and not request.url.query
            return httpx.Response(200, text=group_create)
        if path == "/users/usr":
            text = user
            if request.url.params.get("id") == "alice":
                text = text.replace('name="uid" value=""', 'name="uid" value="alice"')
            return httpx.Response(200, text=text)
        if path == "/admin/dirs":
            return httpx.Response(200, text='<a href="../admin/dir?n=4"><b>/docs/</b></a>')
        if path == "/admin/dir":
            assert request.url.params["n"] == "4"
            return httpx.Response(200, text=acl)
        if path == "/infoboard/graphql":
            assert request.method == "POST"
            body = json.loads(request.content)
            assert "mutation" not in body["query"].lower()
            return httpx.Response(200, json=boards or {"data": {"board": {"many": [{"id": "P1", "title": "Ecology"}]}}})
        raise AssertionError(f"Unexpected endpoint: {path}")
    return handler


@pytest.mark.asyncio
async def test_preflight_reads_complete_forms_and_never_saves():
    requests = []
    async with httpx.AsyncClient(transport=httpx.MockTransport(transport_handler(requests))) as client:
        checks = await check_catalog("https://catalog.local", client, {"session": "COOKIE_SECRET"}, user="alice")
    assert all(check["status"] == "PASS" for check in checks)
    assert len(checks) == 6
    assert all(r.method == "GET" or r.url.path == "/infoboard/graphql" for r in requests)
    serialized = json.dumps(checks)
    for private in ("SECRET", "DO_NOT_PRINT", "COOKIE_SECRET"):
        assert private not in serialized
    user_check = next(c for c in checks if c["name"] == "user_creation_form")
    assert "repeat" in user_check["repeated_controls"]
    assert [r.url.path for r in requests[:2]] == ["/users/groups", "/users/grp"]
    group_check = next(c for c in checks if c["name"] == "group_creation_form")
    assert group_check["control_names"] == ["cmd", "gn", "name", "private"]


@pytest.mark.asyncio
@pytest.mark.parametrize("creation_page", [
    GROUPS, GROUP_CREATE.replace('name="gn"', 'name="gn" value="7"'),
    GROUP_CREATE.replace('name="name"', 'name="name" value="Existing"'),
])
async def test_nonblank_or_unknown_creation_form_is_unverified_without_save(creation_page):
    requests = []
    handler = transport_handler(requests, group_create=creation_page)
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        checks = await check_catalog("https://catalog.local", client, {})
    assert {c["name"] for c in checks if c["status"] != "PASS"} == {"group_creation_form"}
    assert not any(r.method == "POST" and r.url.path.startswith("/users") for r in requests)


@pytest.mark.asyncio
async def test_unknown_group_page_does_not_prevent_other_checks_or_create_group():
    requests = []
    async with httpx.AsyncClient(transport=httpx.MockTransport(transport_handler(requests, groups="<h1>Login</h1>"))) as client:
        checks = await check_catalog("https://catalog.local", client, {})
    failed = {c["name"] for c in checks if c["status"] != "PASS"}
    assert failed == {"groups", "group_creation_form"}
    assert not any(r.method == "POST" and r.url.path.startswith("/users") for r in requests)


@pytest.mark.asyncio
async def test_js_acl_not_readable_by_get_is_unverified_without_setup_post():
    requests = []
    async with httpx.AsyncClient(transport=httpx.MockTransport(transport_handler(requests, acl="<form><input name='path' value='/docs/'></form>"))) as client:
        checks = await check_catalog("https://catalog.local", client, {})
    acl = next(c for c in checks if c["name"] == "acl_form_get")
    assert acl["status"] == "UNVERIFIED"
    assert not any(r.method == "POST" and r.url.path.startswith("/admin") for r in requests)


@pytest.mark.asyncio
@pytest.mark.parametrize("boards", [
    {"errors": [{"message": "SECRET"}]},
    {"data": {"board": {"many": None}}},
    {"data": {"board": {"many": [{"id": "1", "title": "A"}, {"id": "2", "title": "a"}]}}},
])
async def test_graphql_business_errors_and_collisions_are_unverified(boards):
    async with httpx.AsyncClient(transport=httpx.MockTransport(transport_handler([], boards=boards))) as client:
        checks = await check_catalog("https://catalog.local", client, {})
    check = next(c for c in checks if c["name"] == "boards")
    assert check["status"] == "UNVERIFIED"
    assert "SECRET" not in json.dumps(checks)


def test_docs_directory_never_falls_back_to_arbitrary_number():
    assert docs_directory_number('<a href="/admin/dir?n=8">/docs/</a>') == "8"
    assert docs_directory_number('<a href="/admin/dir?n=2"><b> /DOCS/ </b></a>'
                                 '<a href="/admin/dir?n=12">/docs2/</a>') == "2"
    with pytest.raises(ParseError):
        docs_directory_number('<a href="/admin/dir?n=2">/users/</a>')
    with pytest.raises(ParseError):
        docs_directory_number('<a href="/admin/dir?n=2">/docs/</a><a href="/admin/dir?n=3">/docs/</a>')


def test_form_summary_has_no_values():
    assert form_schema([("psw", "SECRET"), ("x", "1"), ("x", "2")]) == {
        "control_count": 3, "control_names": ["psw", "x"], "repeated_controls": ["x"],
    }


@pytest.mark.asyncio
async def test_setup_error_is_reported_without_raw_credentials_and_closes_resolver(monkeypatch):
    from unittest.mock import AsyncMock
    from common.exceptions import ConfigError
    resolver = type("Resolver", (), {"shutdown": AsyncMock()})()
    monkeypatch.setattr("scripts.catalog_preflight.RegResolver", lambda: resolver)
    def invalid_settings():
        raise ConfigError(message="postgresql://user:PRIVATE_PASSWORD@server/db")
    monkeypatch.setattr("scripts.catalog_preflight.get_settings", invalid_settings)
    report = await run_preflight("TEST")
    assert not report["ok"]
    assert report["checks"][0]["error_code"] == "CONFIG_ERROR"
    assert "PRIVATE_PASSWORD" not in json.dumps(report)
    resolver.shutdown.assert_awaited_once()


@pytest.mark.parametrize("ok, expected", [(True, 0), (False, 1)])
def test_cli_exit_status_and_report_file(monkeypatch, tmp_path, capsys, ok, expected):
    async def fake_run(reg, *, user=None):
        assert reg == "TEST" and user == "alice"
        return {"read_only": True, "checks": [], "ok": ok}
    monkeypatch.setattr("scripts.catalog_preflight.run_preflight", fake_run)
    output = tmp_path / "report.json"
    assert main(["--reg", "TEST", "--user", "alice", "--output", str(output)]) == expected
    assert json.loads(output.read_text()) == json.loads(capsys.readouterr().out)
