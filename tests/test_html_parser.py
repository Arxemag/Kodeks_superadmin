"""Behavioural tests of recognised catalog forms and complete form preservation."""
from __future__ import annotations

import pytest

from common.exceptions import ParseError
from services.users_service.html_parser import (
    UserState,
    group_index,
    normalize_title,
    parse_group_creation_form,
    parse_groups_catalog,
    parse_user_state,
)


def test_user_form_reads_spaced_and_unquoted_attributes_and_entities():
    state = parse_user_state('''<form><input name = uid value=u1>
        <input name='psw' value='p&amp;1'><input name=mail value='a&amp;b'>
        <input type=checkbox name=grp value=5 checked>
        <input type=checkbox name=grp value=3 checked></form>''')
    assert state.fields == {"uid": "u1", "psw": "p&1", "mail": "a&b"}
    assert state.groups == [3, 5]
    assert not state.is_empty_form


def test_user_preserves_successful_controls_in_order():
    state = parse_user_state('''<form id=edit><input name=uid value=u>
        <input name=psw value=p><input type=hidden name=csrf value='token'>
        <input type=hidden name=empty><input name=extra value=first>
        <input name=extra value=second><input name=ignored value=x disabled>
        <input type=checkbox name=grp value=9>
        <input type=checkbox name=grp value=7 checked>
        <input type=checkbox name=flag checked>
        <input type=radio name=radio value=a><input type=radio name=radio value=b checked>
        <textarea name=note>hello &amp; bye</textarea>
        <select name=kind><option value=a>A<option value=b selected>B</select>
        <select multiple name=multi><option value=x selected>X</option>
          <option value=z>Z</option><option value=y selected>Y</option></select>
        <input type=submit name=submit value=Save><button name=button>Cancel</button>
        </form><input form=edit name=outside value=ok>''')
    assert state.form_data == [
        ('uid', 'u'), ('psw', 'p'), ('csrf', 'token'), ('empty', ''),
        ('extra', 'first'), ('extra', 'second'), ('grp', '7'), ('flag', 'on'),
        ('radio', 'b'), ('note', 'hello & bye'), ('kind', 'b'),
        ('multi', 'x'), ('multi', 'y'), ('outside', 'ok'), ('submit', 'Save'),
    ]


def test_disabled_fieldset_preserves_only_first_legend_controls():
    state = parse_user_state('''<form><input name=uid value=u><input name=psw value=p>
      <fieldset disabled><legend><input name=enabled value=yes></legend>
        <input name=disabled value=no><legend><input name=also_disabled></legend></fieldset>
      <select name=sel><optgroup disabled><option value=no selected>No</option></optgroup></select>
      </form>''')
    assert state.form_data == [('uid', 'u'), ('psw', 'p'), ('enabled', 'yes')]


def test_select_default_and_text_option():
    state = parse_user_state('''<form><input name=uid value=u><input name=psw value=p>
       <select name=choice><option disabled value=no>No<option>  Some &amp; text  </select>
       <select multiple name=none><option value=x>X</select></form>''')
    assert ('choice', 'Some & text') in state.form_data
    assert not any(name == 'none' for name, _ in state.form_data)


def test_unrelated_form_controls_are_not_merged():
    state = parse_user_state('''<form><input name=uid value=u><input name=psw value=p></form>
        <form><input name=name value=wrong><input name=grp value=99></form>''')
    assert state.form_data == [('uid', 'u'), ('psw', 'p')]
    assert state.groups == []


@pytest.mark.parametrize('page', [
    '<form><input name=uid value=u></form><form><input name=psw value=p></form>',
    '<form><input name=uid value=a><input name=psw></form><form><input name=uid value=b><input name=psw></form>',
    '<form action=/login><input name=uid value=u><input name=psw value=p></form>',
    '<form action=/auth><input name=uid value=u><input name=psw value=p></form>',
    '<html><h1>Login</h1><input name=username><input name=password></html>',
    '<input name=psw value=p>',
    '<input name=uid value=u>',
    '<form><input name=uid value=u disabled><input name=psw value=p></form>',
    '<form><input name=uid value=u><input name=psw value=p><input name=uid value=other></form>',
])
def test_unrecognised_or_ambiguous_user_forms_raise(page):
    with pytest.raises(ParseError):
        parse_user_state(page)


def test_blank_uid_is_distinct_from_existing_user():
    state = parse_user_state('<form><input name=uid><input name=psw><input name=id></form>')
    assert state.is_empty_form
    assert state.fields['uid'] == ''


def test_legacy_userstate_constructor_keeps_compatibility():
    assert UserState({'uid': 'u'}, []).form_data == []


@pytest.mark.parametrize('value', ['not-a-number', '-1', ''])
def test_invalid_selected_group_raises(value):
    with pytest.raises(ParseError):
        parse_user_state(f'<input name=uid value=u><input name=psw value=p><input name=grp value="{value}" checked>')


def test_group_links_handle_attribute_spacing_nested_text_and_entities():
    groups = parse_groups_catalog('''<a href = '/users/grp?other=x&amp;grp=10'><b>Отдел</b>&nbsp; один</a>
       <a href=/users/grp?grp=20>Второй</a>''')
    assert group_index(groups) == {'отдел один': 10, 'второй': 20}


def test_group_select_ignores_unrelated_options():
    assert parse_groups_catalog('''<select name=other><option value=99>Wrong</select>
       <select name=grp><option value=20>Group Twenty</select>''') == {'Group Twenty': 20}


@pytest.mark.parametrize('page', [
    '<html><body></body></html>',
    '<form action=/login><input name=username><input name=password></form>',
    '<select name=region><option value=1>Russia</select>',
    '<option value=20>Unowned option</option>',
])
def test_unknown_group_page_never_means_empty_catalog(page):
    with pytest.raises(ParseError):
        parse_groups_catalog(page)


@pytest.mark.parametrize('page', [
    '<form><input name=name><input type=hidden name=gn></form>',
    '<select name=grp></select>',
    '<div data-groups-catalog></div>',
])
def test_recognised_empty_group_catalog(page):
    assert parse_groups_catalog(page) == {}


@pytest.mark.parametrize('second', ['Отдел', 'ОТДЕЛ', ' Отдел ', 'Отдел\u00a0'])
def test_duplicate_normalised_group_names_are_conflicts(second):
    with pytest.raises(ParseError) as error:
        parse_groups_catalog(f'<a href="grp?grp=1">Отдел</a><a href="grp?grp=2">{second}</a>')
    assert error.value.code == 'AMBIGUOUS_GROUP'


def test_same_group_repeated_on_page_is_not_conflict():
    assert group_index(parse_groups_catalog('''<a href="grp?grp=1">Отдел</a>
       <select name=grp><option value=1>ОТДЕЛ</select>''')) == {'отдел': 1}


def test_normalization_nfc_whitespace_and_significant_letters():
    assert normalize_title('  И\u0306\u00a0  Отдел  ') == 'й отдел'
    assert normalize_title('Ёж-отдел') != normalize_title('Еж отдел')
    assert normalize_title('A &amp; B') == 'a &amp; b'  # Already-decoded text; no second unescape.


def test_group_index_rejects_conflicts():
    with pytest.raises(ParseError):
        group_index({'Отдел': 1, ' ОТДЕЛ ': 2})


@pytest.mark.parametrize('link', [
    '<a rel="next" href="/users/groups?page=2">Next</a>',
    '<a href="/users/groups?offset=100">2</a>',
])
def test_unread_pagination_never_becomes_a_complete_group_list(link):
    with pytest.raises(ParseError) as error:
        parse_groups_catalog('<a href="grp?grp=1">Dept</a>' + link)
    assert error.value.code == 'GROUP_LIST_PAGINATED'


def test_group_creation_form_preserves_hidden_empty_repeated_and_selects():
    assert parse_group_creation_form('''<form><input name=search></form>
        <form><input name=name><input name=gn><input type=hidden name=cmd value=add>
        <input name=extra value=a><input name=extra value=b>
        <select name=scope><option value=local>Local</select></form>''') == [
            ('name', ''), ('gn', ''), ('cmd', 'add'),
            ('extra', 'a'), ('extra', 'b'), ('scope', 'local')]


def test_user_form_includes_actual_named_save_button():
    state = parse_user_state('''<form id="usr"><input name="uid" value="alice">
      <input name="psw" value="p"><input type="submit" name="set" value="Сохранить">
      <button type="submit" name="cancel" value="cancel">Отмена</button></form>''')
    assert ('set', 'Сохранить') in state.form_data
    assert ('cancel', 'cancel') not in state.form_data


def test_group_form_includes_external_save_button():
    pairs = parse_group_creation_form('''<form id="g"><input name="name"><input name="gn"></form>
       <button form="g" type="submit" name="cmd" value="add">Добавить</button>''')
    assert ('cmd', 'add') in pairs


def test_multiple_save_buttons_are_not_guessed():
    with pytest.raises(ParseError, match="Several save submitters"):
        parse_user_state('''<form><input name="uid" value="alice"><input name="psw" value="p">
          <input type="submit" name="set" value="Save"><input type="submit" name="cmd" value="Save">
        </form>''')


@pytest.mark.parametrize('page', [
    '<input name=name><input name=gn>',
    '<form><input name=name></form><form><input name=gn></form>',
    '<form><input name=name><input name=gn></form><form><input name=name><input name=gn></form>',
    '<form><input name=name><input name=gn disabled></form>',
])
def test_group_creation_requires_one_complete_form(page):
    with pytest.raises(ParseError):
        parse_group_creation_form(page)
