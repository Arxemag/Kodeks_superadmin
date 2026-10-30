import { test } from 'node:test';
import assert from 'node:assert/strict';
import { authCookie, catalogUrl, companyIdentity, hostAliases, issuerUrl, readLink, registryForHost, registryRows, roleKind, supportUid } from '../backend/sso.policy.ts';

const rows = registryRows([{ reg_number: '465304', base_url: 'https://cabinet-05.kodeks.expert' },
  { reg_number: '350832', base_url: 'https://cabinet-07.kodeks.expert' }]);
const target = catalogUrl('https://cabinet-05.kodeks.expert/docs/?id=123#section');
const actor = { userId: 1, role: 'CURRENT', companyId: 9 };
const account = { markDeletion: false, status: 'CURRENT', user: { email: 'employee@example.org', available: true },
  company: { id: 9, available: true, reg: { regNumber: 465304 } } };
const rejects = (callback, status) => assert.throws(callback, error => error.status === status);

test('body contains a link only, never reg or uid supplied by browser', () => {
  assert.equal(readLink({ link: target.href }).hostname, target.hostname);
  for (const body of [null, [], target.href, { link: target.href, uid: 'admin' }, { link: target.href, reg: '350832' }])
    rejects(() => readLink(body), 400);
});
test('catalog addresses and encoded path escapes are checked', () => {
  for (const link of ['http://cabinet-05.kodeks.expert/', 'https://kodeks.expert/', 'https://kodeks.expert.evil.test/',
    'https://user:pass@cabinet-05.kodeks.expert/', 'https://cabinet-05.kodeks.expert:444/',
    'https://cabinet-05.kodeks.expert/\n', 'https://cabinet-05.kodeks.expert/%0d',
    'https://cabinet-05.kodeks.expert/%2f%2fevil.test', 'https://cabinet-05.kodeks.expert/%252f',
    'https://cabinet-05.kodeks.expert/' + 'x'.repeat(2048)]) rejects(() => catalogUrl(link), 400);
});
test('role whitelist distinguishes company and support accounts', () => {
  assert.equal(roleKind('CURRENT', ['CURRENT'], ['ADMIN']), 'company');
  assert.equal(roleKind('ADMIN', ['CURRENT'], ['ADMIN']), 'staff');
  rejects(() => roleKind('UNKNOWN', ['CURRENT'], ['ADMIN']), 403);
});
test('company identity comes from active DB account and company reg', () => {
  assert.deepEqual(companyIdentity(actor, account, 'FIRED'), { uid: 'employee', reg: '465304' });
});
test('cross tenant, deleted, fired, and missing account fail closed', () => {
  for (const value of [null, { ...account, markDeletion: true }, { ...account, status: 'FIRED' },
    { ...account, company: { ...account.company, id: 10 } }, { ...account, user: null },
    { ...account, status: 'POTENTIAL' }, { ...account, user: { ...account.user, available: false } },
    { ...account, company: { ...account.company, available: false } }])
    rejects(() => companyIdentity(actor, value, 'FIRED'), 403);
  rejects(() => companyIdentity({ ...actor, companyId: undefined }, account, 'FIRED'), 403);
});
test('company identity cannot open another catalog', () => {
  assert.equal(registryForHost(target, rows, {}, '465304').reg_number, '465304');
  rejects(() => registryForHost(target, rows, {}, '350832'), 403);
});
test('support UID is decoded from existing Auth session, not input', () => {
  assert.equal(supportUid(Buffer.from('support:private:password').toString('base64')), 'support');
  rejects(() => supportUid('not!base64'), 401);
  rejects(() => supportUid(Buffer.from('missing-colon').toString('base64')), 401);
  rejects(() => authCookie(undefined), 401);
});
test('legacy host aliases must be explicitly tied to the selected registry reg', () => {
  const alias = catalogUrl('https://platform.kodeks.expert/docs/?id=1');
  rejects(() => registryForHost(alias, rows, {}), 403);
  const aliases = hostAliases('{"platform.kodeks.expert":"350832"}');
  assert.equal(registryForHost(alias, rows, aliases).reg_number, '350832');
  rejects(() => registryForHost(alias, rows, aliases, '465304'), 403);
  rejects(() => hostAliases('{"evil.test":"350832"}'), 503);
});
test('unknown and ambiguous registry hosts cannot issue tickets', () => {
  rejects(() => registryForHost(catalogUrl('https://unknown.kodeks.expert/'), rows, {}), 403);
  rejects(() => registryForHost(target, [...rows, { ...rows[0], reg_number: 'other' }], {}), 503);
});
test('issuer URL must be a canonical single-ticket destination', () => {
  const url = 'https://cabinet-05.kodeks.expert/sso?ticket=one-time';
  assert.equal(issuerUrl(url, rows[0]), url);
  for (const wrong of ['https://cabinet-07.kodeks.expert/sso?ticket=x',
    'https://cabinet-05.kodeks.expert/docs/?ticket=x', url + '&ticket=duplicate', url + '&reg=spoof'])
    rejects(() => issuerUrl(wrong, rows[0]), 503);
});
