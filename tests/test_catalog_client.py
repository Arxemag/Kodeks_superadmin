"""Catalog HTTP boundary tests: HTML is evidence, an HTTP 200 alone is not."""
from __future__ import annotations

from contextlib import asynccontextmanager
from urllib.parse import parse_qsl

import httpx
import pytest

from common.exceptions import AuthError, NetworkError, ParseError, ServiceError
from services.users_service.catalog_client import CatalogClient


BASE = 'https://catalog.test'
LOGIN = '<form action=/login><input name=login><input type=password name=password></form>'
GROUP_FORM = '''<form action=/users/groups method=post>
    <input name=name value=''><input type=hidden name=gn value=''>
    <input type=hidden name=cmd value=add><input type=hidden name=csrf value=token>
    <input type=hidden name=empty><input name=extra value=first>
    <input name=extra value=second></form>'''


@pytest.fixture(autouse=True)
def isolated_catalog_lock(monkeypatch):
    @asynccontextmanager
    async def unlocked(base_url):
        yield
    monkeypatch.setattr('services.users_service.catalog_client.catalog_operation', unlocked)


@pytest.mark.asyncio
async def test_get_user_not_found_is_only_404():
    async with httpx.AsyncClient(transport=httpx.MockTransport(lambda request: httpx.Response(404))) as http:
        assert await CatalogClient(http).get_user_page(BASE, 'missing', {}) is None


@pytest.mark.asyncio
@pytest.mark.parametrize('status', [400, 401, 403, 405, 429, 500, 503])
async def test_get_user_other_errors_never_mean_absence(status):
    async with httpx.AsyncClient(transport=httpx.MockTransport(lambda request: httpx.Response(status))) as http:
        with pytest.raises((AuthError, NetworkError, ParseError)):
            await CatalogClient(http).get_user_page(BASE, 'u', {})


@pytest.mark.asyncio
async def test_get_user_rejects_login_html_with_http_200():
    async with httpx.AsyncClient(transport=httpx.MockTransport(lambda request: httpx.Response(200, text=LOGIN))) as http:
        with pytest.raises(AuthError):
            await CatalogClient(http).get_user_page(BASE, 'u', {})


@pytest.mark.asyncio
async def test_get_user_accepts_a_recognised_user_form():
    page = '<form><input name=uid value=u><input name=psw value=p><input name=id value=internal42></form>'
    requests = []
    def respond(request):
        requests.append(request)
        return httpx.Response(200, text=page)
    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as http:
        result = await CatalogClient(http).get_user_page(BASE, 'u', {})
    assert result == page
    assert requests[0].url.params['id'] == 'u'


@pytest.mark.asyncio
@pytest.mark.parametrize('follow', [False, True])
async def test_post_user_rejects_redirect_to_login(follow):
    requests = []
    def respond(request):
        requests.append(request)
        if request.url.path == '/login':
            return httpx.Response(200, text=LOGIN)
        return httpx.Response(302, headers={'location': '/login'})
    async with httpx.AsyncClient(transport=httpx.MockTransport(respond), follow_redirects=follow) as http:
        with pytest.raises(ServiceError):
            await CatalogClient(http).post_user(BASE, [('uid', 'u'), ('psw', 'p')], {})
    assert len([request for request in requests if request.method == 'POST']) == 1


@pytest.mark.asyncio
async def test_post_user_rejects_direct_login_response():
    async with httpx.AsyncClient(transport=httpx.MockTransport(lambda request: httpx.Response(200, text=LOGIN))) as http:
        with pytest.raises(ServiceError):
            await CatalogClient(http).post_user(BASE, [('uid', 'u'), ('psw', 'p')], {})


@pytest.mark.asyncio
async def test_create_existing_normalised_group_never_posts():
    requests = []
    def respond(request):
        requests.append(request)
        return httpx.Response(200, text='<a href = "grp?grp=42"> Отдел&nbsp;  продаж </a>')
    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as http:
        await CatalogClient(http).create_group(BASE, 'ОТДЕЛ продаж', {})
    assert requests
    assert all(request.method == 'GET' for request in requests)


@pytest.mark.asyncio
@pytest.mark.parametrize('page', [LOGIN, '<html><body>Unexpected catalog page</body></html>'])
async def test_create_group_unknown_html_never_posts(page):
    requests = []
    def respond(request):
        requests.append(request)
        return httpx.Response(200, text=page)
    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as http:
        with pytest.raises(ServiceError):
            await CatalogClient(http).create_group(BASE, 'New Group', {})
    assert all(request.method == 'GET' for request in requests)


@pytest.mark.asyncio
async def test_create_group_preserves_entire_successful_form_and_verifies():
    requests = []
    posted = False
    def respond(request):
        nonlocal posted
        requests.append(request)
        if request.method == 'POST':
            posted = True
            return httpx.Response(200, text=GROUP_FORM)
        page = GROUP_FORM
        if posted:
            page += '<a href="grp?grp=99">New Group</a>'
        return httpx.Response(200, text=page)
    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as http:
        await CatalogClient(http).create_group(BASE, 'New Group', {})
    posts = [request for request in requests if request.method == 'POST']
    assert len(posts) == 1
    submitted = parse_qsl(posts[0].content.decode(), keep_blank_values=True)
    assert [(name, value) for name, value in submitted if name != 'name'] == [
        ('gn', ''), ('cmd', 'add'), ('csrf', 'token'), ('empty', ''),
        ('extra', 'first'), ('extra', 'second'),
    ]
    assert [(name, value) for name, value in submitted if name == 'name'] == [('name', 'New Group')]
    assert requests[-1].method == 'GET'


@pytest.mark.asyncio
async def test_create_group_does_not_report_success_when_reread_is_missing():
    requests = []
    def respond(request):
        requests.append(request)
        return httpx.Response(200, text=GROUP_FORM)
    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as http:
        with pytest.raises(ServiceError):
            await CatalogClient(http).create_group(BASE, 'New Group', {})
    assert len([request for request in requests if request.method == 'POST']) == 1
    assert requests[-1].method == 'GET'


@pytest.mark.asyncio
async def test_ambiguous_existing_groups_never_post():
    requests = []
    def respond(request):
        requests.append(request)
        return httpx.Response(200, text=GROUP_FORM + '<a href="grp?grp=1">Dept</a><a href="grp?grp=2">DEPT</a>')
    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as http:
        with pytest.raises(ParseError) as error:
            await CatalogClient(http).create_group(BASE, 'Dept', {})
    assert error.value.code == 'AMBIGUOUS_GROUP'
    assert all(request.method == 'GET' for request in requests)


@pytest.mark.asyncio
@pytest.mark.parametrize('mode', ['timeout', 'server_error'])
async def test_indeterminate_group_write_is_not_automatically_repeated(mode):
    requests = []
    def respond(request):
        requests.append(request)
        if request.method == 'GET':
            return httpx.Response(200, text=GROUP_FORM)
        if mode == 'timeout':
            raise httpx.ReadTimeout('response lost', request=request)
        return httpx.Response(503, text='unavailable')
    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as http:
        with pytest.raises(ParseError) as error:
            await CatalogClient(http).create_group(BASE, 'New Group', {})
    assert error.value.code == 'CATALOG_WRITE_UNCERTAIN'
    assert sum(r.method == 'POST' for r in requests) == 1


@pytest.mark.asyncio
async def test_server_error_on_read_is_retryable_not_uncertain_write():
    async with httpx.AsyncClient(transport=httpx.MockTransport(lambda request: httpx.Response(503))) as http:
        with pytest.raises(NetworkError) as error:
            await CatalogClient(http).get_groups_page(BASE, {})
    assert error.value.code == 'CATALOG_5XX'
