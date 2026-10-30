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
GROUP_INDEX = '''<a href="grp?grp=7">Existing department</a>
<form action='../users/grp ' method=POST><input type=SUBMIT value='Create group'></form>'''
GROUP_FORM = '''<form action=../users/groups method=POST>
    <input name=name value=''><input type=hidden name=gn value=''>
    <input type=hidden name=cmd value=''><input type=hidden name=csrf value=token>
    <input type=hidden name=empty><input name=extra value=first>
    <input name=extra value=second><input type=SUBMIT value='Save'></form>'''


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


USER_BLANK = '<form><input name=uid value=""><input name=psw value=""></form>'
USER_SEARCH_ABSENT = '''<form action="../users/users" method=POST>
<input name=usr value=""><input name=add value=""><input name=tmpl value=alice>
</form>Пользователь "alice" не найден'''


@pytest.mark.asyncio
async def test_blank_user_form_requires_filtered_search_before_reporting_absence():
    requests = []
    def respond(request):
        requests.append(request)
        if request.url.path == '/users/usr':
            assert request.url.params['id'] == 'alice'
            return httpx.Response(200, text=USER_BLANK)
        assert request.url.path == '/users/users' and request.url.params['tmpl'] == 'alice'
        return httpx.Response(200, text=USER_SEARCH_ABSENT)
    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as http:
        assert await CatalogClient(http).get_user_page(BASE, 'alice', {}) is None
    assert [(r.method, r.url.path) for r in requests] == [('GET', '/users/usr'), ('GET', '/users/users')]


@pytest.mark.asyncio
@pytest.mark.parametrize('page', [
    USER_BLANK, '<html>Unexpected page</html>',
    USER_SEARCH_ABSENT.replace('name=tmpl value=alice', 'name=tmpl value=another'),
    USER_SEARCH_ABSENT.replace('"alice" не найден', '"another" не найден'),
    USER_SEARCH_ABSENT + '<a href="../users/usr?id=alice">alice</a>',
    USER_SEARCH_ABSENT.replace('Пользователь "alice" не найден', '<a href="../users/usr?id=alice">alice</a>'),
])
async def test_blank_user_form_search_cannot_convert_unknown_or_existing_user_to_absence(page):
    def respond(request):
        return httpx.Response(200, text=USER_BLANK if request.url.path == '/users/usr' else page)
    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as http:
        with pytest.raises(ParseError) as error:
            await CatalogClient(http).get_user_page(BASE, 'alice', {})
    assert error.value.code == 'USER_PRESENCE_UNCERTAIN'


@pytest.mark.asyncio
@pytest.mark.parametrize('status', [200, 401, 403, 404, 500])
async def test_blank_user_form_search_login_and_http_errors_never_mean_absence(status):
    def respond(request):
        if request.url.path == '/users/usr':
            return httpx.Response(200, text=USER_BLANK)
        return httpx.Response(status, text=LOGIN)
    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as http:
        with pytest.raises(ServiceError):
            await CatalogClient(http).get_user_page(BASE, 'alice', {})


@pytest.mark.asyncio
async def test_blank_user_form_search_network_error_never_means_absence():
    def respond(request):
        if request.url.path == '/users/usr':
            return httpx.Response(200, text=USER_BLANK)
        raise httpx.ReadTimeout('search unavailable', request=request)
    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as http:
        with pytest.raises(NetworkError):
            await CatalogClient(http).get_user_page(BASE, 'alice', {})


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
    assert len(requests) == 1
    assert requests[0].method == 'GET' and requests[0].url.path == '/users/groups'


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
            assert request.url.path == '/users/groups'
            posted = True
            return httpx.Response(200, text=GROUP_INDEX)
        if request.url.path == '/users/grp':
            return httpx.Response(200, text=GROUP_FORM)
        assert request.url.path == '/users/groups'
        page = GROUP_INDEX
        if posted:
            page += '<a href="grp?grp=99">New Group</a>'
        return httpx.Response(200, text=page)
    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as http:
        await CatalogClient(http).create_group(BASE, 'New Group', {})
    posts = [request for request in requests if request.method == 'POST']
    assert len(posts) == 1
    submitted = parse_qsl(posts[0].content.decode(), keep_blank_values=True)
    assert [(name, value) for name, value in submitted if name != 'name'] == [
        ('gn', ''), ('cmd', ''), ('csrf', 'token'), ('empty', ''),
        ('extra', 'first'), ('extra', 'second'),
    ]
    assert [(name, value) for name, value in submitted if name == 'name'] == [('name', 'New Group')]
    assert [(r.method, r.url.path) for r in requests] == [
        ('GET', '/users/groups'), ('GET', '/users/grp'),
        ('POST', '/users/groups'), ('GET', '/users/groups'),
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize('page', [
    GROUP_FORM.replace("name=gn value=''", "name=gn value='42'"),
    GROUP_FORM.replace("name=name value=''", "name=name value='Existing'"),
    GROUP_FORM.replace('method=POST', 'method=GET'),
    GROUP_FORM.replace('action=../users/groups', 'action=https://other.test/users/groups'),
    GROUP_INDEX,
    LOGIN,
])
async def test_create_group_rejects_unverified_creation_page_without_post(page):
    requests = []
    def respond(request):
        requests.append(request)
        return httpx.Response(200, text=GROUP_INDEX if request.url.path == '/users/groups' else page)
    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as http:
        with pytest.raises(ServiceError):
            await CatalogClient(http).create_group(BASE, 'New Group', {})
    assert [(r.method, r.url.path) for r in requests] == [
        ('GET', '/users/groups'), ('GET', '/users/grp'),
    ]


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
