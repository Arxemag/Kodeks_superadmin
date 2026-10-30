"""Administrative form preservation and read-after-write verification.

Fixtures below are protocol unit examples, not proof of a live catalog contract.
"""
from contextlib import asynccontextmanager
from html import escape
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock
from urllib.parse import parse_qsl

import pytest
import httpx

from common.exceptions import NetworkError, ParseError
from services.infoboards_service import cabinet_acl
from services.infoboards_service import init_company_service as company
from services.infoboards_service.admin_dirs import (
    acl_checkbox_values, acl_control_names, build_admin_dirs_form, parse_form_pairs, verify_acl_form,
)


def acl_page(groups=(1,), *, untouched=(7,), extra="", active=True, view_groups=None):
    def fields(board, ids):
        result = ""
        for suffix in ("", "_view"):
            selected = view_groups if board == "p1" and suffix and view_groups is not None else ids
            value = escape('{"kw":[' + ",".join(f'"{group}"' for group in selected) + '] }', quote=True)
            result += f'<input type=hidden name="acl_infoboard_{board}{suffix}" value="{value}">'
            result += f'<input type=checkbox name="infoboard_{board}{suffix}" {"checked" if active and selected else ""}>'
        return result
    return (
        '<form action="/admin/dirs" method=post>'
        '<input name=unknown value=""><input name=repeat value=A><input name=repeat value=B>'
        '<textarea name=note>A &amp; B</textarea>'
        '<select name=multi multiple><option value=x selected>X</option><option value=y selected>Y</option></select>'
        + fields("p1", groups) + fields("p2", untouched) + extra
        + '<input type=submit name=cmd value=save></form>'
    )


def planned(html, groups):
    return build_admin_dirs_form(
        parse_form_pairs(html, acl=True, include_save=True), {"P1": groups},
        available_controls=acl_control_names(html),
        checkbox_values=acl_checkbox_values(html),
    )


@pytest.fixture
def ownership():
    return SimpleNamespace(get=AsyncMock(return_value=({}, {})), replace=AsyncMock())


def test_complete_form_preserves_empty_repeated_and_unknown_controls():
    pairs = planned(acl_page(), [3])
    assert [(key, value) for key, value in pairs if key == "repeat"] == [("repeat", "A"), ("repeat", "B")]
    assert ("unknown", "") in pairs
    assert ("note", "A & B") in pairs
    assert [(key, value) for key, value in pairs if key == "multi"] == [("multi", "x"), ("multi", "y")]
    assert ("cmd", "save") in pairs
    assert not any(key == "packprocess" for key, _ in pairs)
    verify_acl_form(pairs, acl_page((3,)))


def test_empty_override_clears_acl_and_activation():
    pairs = planned(acl_page(), [])
    assert not any(key in {"infoboard_p1", "infoboard_p1_view"} for key, _ in pairs)
    verify_acl_form(pairs, acl_page(()))


def test_previously_unchecked_real_checkbox_can_be_enabled():
    pairs = planned(acl_page(()), [3])
    assert ("infoboard_p1", "on") in pairs
    verify_acl_form(pairs, acl_page((3,)))


def test_enabling_checkbox_preserves_its_actual_value():
    html = acl_page(()).replace('name="infoboard_p1" ', 'name="infoboard_p1" value="enabled" ')
    assert ("infoboard_p1", "enabled") in planned(html, [3])


def test_missing_board_is_error_instead_of_invented_fields():
    with pytest.raises(ParseError) as caught:
        build_admin_dirs_form(parse_form_pairs(acl_page(), acl=True), {"missing": [3]}, extra_board_ids={"missing"})
    assert caught.value.code == "ACL_BOARD_NOT_FOUND"


@pytest.mark.parametrize("html", [
    '<html>sign in</html>',
    '<input name=acl_infoboard_p1 value="{&quot;kw&quot;:[]}">',
    acl_page() + acl_page(),
    acl_page(extra='<input name=acl_infoboard_bad value="not JSON">'),
])
def test_incomplete_ambiguous_or_invalid_form_is_rejected(html):
    with pytest.raises(ParseError):
        parse_form_pairs(html, acl=True)


def test_save_command_comes_from_actual_submitter():
    html = acl_page().replace('name=cmd value=save', 'name=actual_command value="Сохранить"')
    pairs = planned(html, [3])
    assert ("actual_command", "Сохранить") in pairs
    assert not any(key == "cmd" for key, _ in pairs)


def test_live_acl_install_submitter_preserves_full_form_without_invented_command():
    html = acl_page(extra='<input type=button value="Установить группы" onclick="setGroups()">').replace(
        'name=cmd value=save', 'value="Установить"')
    pairs = planned(html, [3])
    assert not any(key == "cmd" for key, _ in pairs)
    assert [(key, value) for key, value in pairs if key == "repeat"] == [("repeat", "A"), ("repeat", "B")]
    assert dict(pairs)["unknown"] == ""


def test_multiple_live_install_submitters_are_rejected():
    html = acl_page(extra='<input type=submit value="Установить">').replace(
        'name=cmd value=save', 'value="Установить"')
    with pytest.raises(ParseError) as caught:
        parse_form_pairs(html, acl=True, include_save=True)
    assert caught.value.code == "ACL_SAVE_CONTROL_UNKNOWN"


def test_save_submitter_associated_by_form_id_is_included():
    html = acl_page().replace('<form', '<form id="settings"', 1).replace(
        '<input type=submit name=cmd value=save>', '')
    html += '<input type=submit form="settings" name=apply value="Установить">'
    pairs = parse_form_pairs(html, acl=True, include_save=True)
    assert ("apply", "Установить") in pairs


def inactive_blank_page():
    html = acl_page((), untouched=(7,), active=False)
    return html.replace('value="{&quot;kw&quot;:[] }"', 'value=""')


def test_blank_inactive_acl_is_preserved_through_parse_plan_build_and_verify():
    from common.acl_ownership import plan_acl_assignments
    from services.infoboards_service.admin_dirs import acl_values

    html = inactive_blank_page()
    pairs = parse_form_pairs(html, acl=True, include_save=True)
    assert dict(pairs)["acl_infoboard_p1"] == ""
    assert acl_values(pairs)["acl_infoboard_p1"] == {"kw": []}
    current = {name: {int(group) for group in value["kw"]} for name, value in acl_values(pairs).items()}
    overrides, owned = plan_acl_assignments(current, {}, {"acl_infoboard_p1": set()})
    result = build_admin_dirs_form(pairs, {}, available_controls=acl_control_names(html), field_overrides=overrides)
    assert dict(result)["acl_infoboard_p1"] == ""
    assert dict(result)["acl_infoboard_p1_view"] == ""
    assert owned == {}
    assert [(k, v) for k, v in result if k == "repeat"] == [("repeat", "A"), ("repeat", "B")]
    verify_acl_form(result, html)


def test_explicit_assignment_initializes_only_target_blank_acl():
    from services.infoboards_service.admin_dirs import acl_values

    html = inactive_blank_page()
    pairs = parse_form_pairs(html, acl=True, include_save=True)
    result = build_admin_dirs_form(pairs, {}, available_controls=acl_control_names(html),
                                  checkbox_values=acl_checkbox_values(html),
                                  field_overrides={"acl_infoboard_p1": [3]})
    assert acl_values(result)["acl_infoboard_p1"]["kw"] == ["3"]
    assert dict(result)["infoboard_p1"] == "on"
    assert dict(result)["acl_infoboard_p1_view"] == ""
    assert "infoboard_p1_view" not in dict(result)
    assert dict(result)["acl_infoboard_p2"] == dict(pairs)["acl_infoboard_p2"]
    actual = html.replace('name="acl_infoboard_p1" value=""',
                          'name="acl_infoboard_p1" value="{&quot;kw&quot;:[&quot;3&quot;]}"')
    actual = actual.replace('name="infoboard_p1" >', 'name="infoboard_p1" checked>')
    verify_acl_form(result, actual)


@pytest.mark.parametrize("change", ["checked", "disabled", "missing", "text", "malformed"])
def test_blank_acl_requires_real_enabled_unchecked_checkbox(change):
    html = inactive_blank_page()
    if change == "malformed":
        html = html.replace('name="acl_infoboard_p1" value=""', 'name="acl_infoboard_p1" value="not-json"')
    elif change == "missing":
        html = html.replace('name="infoboard_p1"', 'name="unrelated_flag"')
    elif change == "text":
        html = html.replace('type=checkbox name="infoboard_p1"', 'type=text name="infoboard_p1"')
    else:
        html = html.replace('name="infoboard_p1" >', f'name="infoboard_p1" {change}>')
    with pytest.raises(ParseError) as caught:
        parse_form_pairs(html, acl=True)
    assert caught.value.code == "ACL_FORM_INVALID"


def test_blank_acl_without_dom_context_is_rejected():
    from services.infoboards_service.admin_dirs import acl_values
    with pytest.raises(ParseError):
        acl_values([("acl_infoboard_p1", "")])


def test_blank_and_json_empty_are_not_equivalent_when_verifying():
    html = inactive_blank_page()
    expected = parse_form_pairs(html, acl=True, include_save=True)
    changed = html.replace('name="acl_infoboard_p1" value=""',
                           'name="acl_infoboard_p1" value="{&quot;kw&quot;:[]}"')
    with pytest.raises(ParseError) as caught:
        verify_acl_form(expected, changed)
    assert caught.value.code == "ACL_WRITE_NOT_CONFIRMED"
    with pytest.raises(ParseError):
        verify_acl_form(parse_form_pairs(changed, acl=True), html)


@pytest.mark.parametrize("actual", [acl_page(), acl_page((3,), untouched=(9,)), acl_page((3,), active=False)])
def test_verify_rejects_noop_unrelated_acl_change_or_missing_activation(actual):
    with pytest.raises(ParseError) as caught:
        verify_acl_form(planned(acl_page(), [3]), actual)
    assert caught.value.code == "ACL_WRITE_NOT_CONFIRMED"


@pytest.mark.asyncio
async def test_http_acl_requires_fresh_read_and_preserves_repeats(monkeypatch, ownership):
    fetch = AsyncMock(side_effect=[(acl_page(), "https://catalog/admin/dirs", "", "2"), (acl_page((1, 3)), "", "", "2")])
    monkeypatch.setattr(company, "fetch_acl_form_via_http", fetch)
    client = SimpleNamespace(post=AsyncMock(return_value=SimpleNamespace(status_code=200)))
    service = company.InitCompanyService(None, None, client, None, ownership)

    await service._apply_acl("https://catalog", {}, {"P1": [3]}, {}, reg="reg-a")

    sent = parse_qsl(client.post.await_args.kwargs["content"], keep_blank_values=True)
    assert [(key, value) for key, value in sent if key == "repeat"] == [("repeat", "A"), ("repeat", "B")]
    assert ("unknown", "") in sent
    assert fetch.await_count == 2


@pytest.mark.asyncio
async def test_http_200_without_applied_change_is_not_success(monkeypatch, ownership):
    monkeypatch.setattr(company, "fetch_acl_form_via_http", AsyncMock(return_value=(acl_page(), "https://catalog/admin/dirs", "", "2")))
    client = SimpleNamespace(post=AsyncMock(return_value=SimpleNamespace(status_code=200)))
    service = company.InitCompanyService(None, None, client, None, ownership)
    with pytest.raises(ParseError) as caught:
        await service._apply_acl("https://catalog", {}, {"P1": [3]}, {}, reg="reg-a")
    assert caught.value.code == "ACL_WRITE_NOT_CONFIRMED"
    assert client.post.await_count == 1


@pytest.mark.asyncio
async def test_unknown_required_board_fails_before_http_write(monkeypatch, ownership):
    monkeypatch.setattr(company, "fetch_acl_form_via_http", AsyncMock(return_value=(acl_page(), "https://catalog/admin/dirs", "", "2")))
    client = SimpleNamespace(post=AsyncMock())
    service = company.InitCompanyService(None, None, client, None, ownership)
    with pytest.raises(ParseError):
        await service._apply_acl("https://catalog", {}, {"missing": [3]}, {}, reg="reg-a")
    client.post.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize('failure', ['timeout', 'http_500', 'read_after_write'])
async def test_acl_post_uncertain_outcome_never_becomes_retryable(monkeypatch, ownership, failure):
    before = (acl_page(), 'https://catalog/admin/dirs', '', '2')
    fetch = AsyncMock(return_value=before)
    if failure == 'read_after_write':
        fetch.side_effect = [before, NetworkError(code='ADMIN_GET_FAILED', message='offline')]
    monkeypatch.setattr(company, 'fetch_acl_form_via_http', fetch)
    post = AsyncMock(return_value=SimpleNamespace(status_code=500 if failure == 'http_500' else 200))
    if failure == 'timeout':
        post.side_effect = httpx.ReadTimeout('response not received')
    service = company.InitCompanyService(None, None, SimpleNamespace(post=post), None, ownership)
    with pytest.raises(ParseError) as caught:
        await service._apply_acl('https://catalog', {}, {'P1': [3]}, {}, reg='reg-a')
    assert caught.value.code == 'ACL_WRITE_UNCERTAIN'
    post.assert_awaited_once()
    ownership.replace.assert_not_awaited()


@pytest.mark.asyncio
async def test_ownership_failure_after_verified_write_requires_reconciliation(monkeypatch, ownership):
    fetch = AsyncMock(side_effect=[
        (acl_page(), 'https://catalog/admin/dirs', '', '2'),
        (acl_page((1, 3)), '', '', '2'),
    ])
    monkeypatch.setattr(company, 'fetch_acl_form_via_http', fetch)
    ownership.replace.side_effect = NetworkError(code='ACL_OWNERSHIP_WRITE_FAILED', message='offline')
    post = AsyncMock(return_value=SimpleNamespace(status_code=200))
    service = company.InitCompanyService(None, None, SimpleNamespace(post=post), None, ownership)
    with pytest.raises(ParseError) as caught:
        await service._apply_acl('https://catalog', {}, {'P1': [3]}, {}, reg='reg-a')
    assert caught.value.code == 'ACL_OWNERSHIP_PERSIST_UNCERTAIN'
    post.assert_awaited_once()


@pytest.mark.asyncio
@pytest.mark.parametrize('failure', ['timeout', 'http_500', 'read_after_write'])
async def test_legacy_acl_submit_reports_uncertain_outcome(monkeypatch, failure):
    before = (acl_page(), 'https://catalog/admin/dirs', '', '2')
    fetch = AsyncMock(return_value=before)
    if failure == 'read_after_write':
        fetch.side_effect = [before, NetworkError(code='ADMIN_GET_FAILED', message='offline')]
    monkeypatch.setattr(cabinet_acl, 'fetch_acl_form_via_http', fetch)
    post = AsyncMock(return_value=SimpleNamespace(status_code=500 if failure == 'http_500' else 200, text=''))
    if failure == 'timeout':
        post.side_effect = httpx.ReadTimeout('response not received')
    with pytest.raises(ParseError) as caught:
        await cabinet_acl.post_acl_form('https://catalog', SimpleNamespace(post=post), {},
                                       'https://catalog/admin/dirs', planned(acl_page(), [3]))
    assert caught.value.code == 'ACL_WRITE_UNCERTAIN'
    post.assert_awaited_once()


@pytest.mark.asyncio
async def test_init_company_handler_does_not_retry_uncertain_acl_write(monkeypatch, ownership):
    from services.infoboards_service import init_company_worker as worker

    @asynccontextmanager
    async def session():
        yield None

    resolver = SimpleNamespace(with_session=session)
    settings = SimpleNamespace(KAFKA_INIT_COMPANY_DLQ_TOPIC='company-dlq', USERS_RETRY_ATTEMPTS=3,
                               USERS_RETRY_BASE_DELAY=0.001, USERS_RETRY_MAX_DELAY=0.01)
    record = SimpleNamespace(topic='init_company', partition=0, offset=3,
                             value={'id': 1, 'reg': 'reg-a', 'departments': []})
    monkeypatch.setattr(company, 'fetch_acl_form_via_http', AsyncMock(
        return_value=(acl_page(), 'https://catalog/admin/dirs', '', '2')))
    post = AsyncMock(side_effect=httpx.ReadTimeout('response not received'))
    service = company.InitCompanyService(None, None, SimpleNamespace(post=post), None, ownership)
    async def handle(dto):
        await service._apply_acl('https://catalog', {}, {'P1': [3]}, {}, reg='reg-a')
    monkeypatch.setattr(worker, 'InitCompanyService', lambda **kwargs: SimpleNamespace(handle=handle))
    producer = SimpleNamespace(send_and_wait=AsyncMock())
    await worker._handle_with_retries(record, resolver, None, None, producer, settings)
    post.assert_awaited_once()
    producer.send_and_wait.assert_awaited_once()
    assert producer.send_and_wait.await_args.args[1]['error_code'] == 'ACL_WRITE_UNCERTAIN'


@pytest.mark.asyncio
async def test_setup_preserves_actual_fields_without_fabricated_defaults():
    dirs = '<a href="../admin/dir?n=2">/docs/</a>'
    setup = '<form><input name=n value=2><input name=repeat value=A><input name=repeat value=B><input type=submit name=setup2 value="Настройки сервисов"></form>'
    client = SimpleNamespace(
        get=AsyncMock(side_effect=[SimpleNamespace(status_code=200, text=dirs), SimpleNamespace(status_code=200, text=setup)]),
        post=AsyncMock(return_value=SimpleNamespace(status_code=200, text=acl_page())),
    )
    await cabinet_acl.fetch_acl_form_via_http("https://catalog", client, {})
    sent = parse_qsl(client.post.await_args.kwargs["content"], keep_blank_values=True)
    assert sent == [("n", "2"), ("repeat", "A"), ("repeat", "B"), ("setup2", "Настройки сервисов")]


@pytest.mark.asyncio
@pytest.mark.parametrize("mode,confirmed", [("board", True), ("board", False), ("field", True), ("field", False), ("noop", True)])
async def test_browser_reopens_and_verifies_after_single_native_submit(monkeypatch, confirmed, mode):
    import playwright.async_api

    @asynccontextmanager
    async def context(value):
        yield value

    save = SimpleNamespace(count=AsyncMock(return_value=1), click=AsyncMock())
    form = MagicMock()
    form.count = AsyncMock(return_value=1)
    form.locator.return_value = save
    locator = MagicMock()
    locator.filter.return_value = form
    page = SimpleNamespace(locator=MagicMock(return_value=locator), evaluate=AsyncMock(), wait_for_load_state=AsyncMock())
    browser_context = SimpleNamespace(add_cookies=AsyncMock(), new_page=AsyncMock(return_value=page))
    browser = SimpleNamespace(new_context=AsyncMock(return_value=browser_context))
    monkeypatch.setattr(playwright.async_api, "async_playwright", lambda: context(None))
    monkeypatch.setattr(cabinet_acl, "open_chromium", lambda *args, **kwargs: context(browser))
    after = acl_page((3,), view_groups=(4,)) if mode == "field" else acl_page((3,))
    read = AsyncMock(side_effect=[acl_page(), after if confirmed else acl_page()])
    monkeypatch.setattr(cabinet_acl, "_open_acl_browser_page", read)

    matrix = {"P1": [1 if mode == "noop" else 3]} if mode != "field" else {}
    options = {"field_overrides": {"acl_infoboard_p1": [3], "acl_infoboard_p1_view": [4]}} if mode == "field" else {}
    if confirmed:
        await cabinet_acl.apply_acl_via_browser("https://catalog", {}, "2", matrix, **options)
    else:
        with pytest.raises(ParseError) as caught:
            await cabinet_acl.apply_acl_via_browser("https://catalog", {}, "2", matrix, **options)
        assert caught.value.code == "ACL_WRITE_NOT_CONFIRMED"
    if mode == "noop":
        assert read.await_count == 1
        save.click.assert_not_awaited()
        return
    assert read.await_count == 2
    save.click.assert_awaited_once()
    if mode == "field":
        assert '"4"' in page.evaluate.await_args.args[1]["acl_infoboard_p1_view"]
        assert '"3"' in page.evaluate.await_args.args[1]["acl_infoboard_p1"]


@pytest.mark.asyncio
@pytest.mark.parametrize('failure', ['click', 'navigation', 'read_after_write'])
async def test_browser_uncertain_submit_is_not_retryable(monkeypatch, failure):
    import playwright.async_api

    @asynccontextmanager
    async def context(value):
        yield value

    save = SimpleNamespace(count=AsyncMock(return_value=1), click=AsyncMock())
    if failure == 'click':
        save.click.side_effect = TimeoutError('timed out')
    form = MagicMock()
    form.count = AsyncMock(return_value=1)
    form.locator.return_value = save
    locator = MagicMock()
    locator.filter.return_value = form
    page = SimpleNamespace(locator=MagicMock(return_value=locator), evaluate=AsyncMock(), wait_for_load_state=AsyncMock())
    if failure == 'navigation':
        page.wait_for_load_state.side_effect = TimeoutError('navigation timeout')
    browser_context = SimpleNamespace(add_cookies=AsyncMock(), new_page=AsyncMock(return_value=page))
    browser = SimpleNamespace(new_context=AsyncMock(return_value=browser_context))
    monkeypatch.setattr(playwright.async_api, 'async_playwright', lambda: context(None))
    monkeypatch.setattr(cabinet_acl, 'open_chromium', lambda *args, **kwargs: context(browser))
    read = AsyncMock(return_value=acl_page())
    if failure == 'read_after_write':
        read.side_effect = [acl_page(), NetworkError(code='ADMIN_GET_FAILED', message='offline')]
    monkeypatch.setattr(cabinet_acl, '_open_acl_browser_page', read)
    with pytest.raises(ParseError) as caught:
        await cabinet_acl.apply_acl_via_browser('https://catalog', {}, '2', {'P1': [3]})
    assert caught.value.code == 'ACL_WRITE_UNCERTAIN'
    save.click.assert_awaited_once()
