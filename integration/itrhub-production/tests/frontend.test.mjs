import { test } from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { stripTypeScriptTypes } from 'node:module';
import vm from 'node:vm';

// Execute the actual helper with only the portal's notifier and browser surface
// substituted. No production source tree or React runtime is required.
function helper(open) {
  const notices = [];
  const source = readFileSync(new URL('../frontend/catalogSso.ts', import.meta.url), 'utf8')
    .replace(/^import \{ enqueueSnackbar \} from 'notistack';\r?\n/m, '')
    .replace(/export (?=(?:async )?function)/g, '');
  const context = vm.createContext({ URL, window: { open }, enqueueSnackbar: (...args) => notices.push(args) });
  vm.runInContext(stripTypeScriptTypes(source) + '\nglobalThis.api = { isCatalogLink, openCatalogSsoLink };', context);
  return { ...context.api, notices };
}
const link = 'https://cabinet-05.kodeks.expert/docs/?nd=1';
function popup() {
  return { opener: 'portal', destinations: [], closed: false,
    location: { replace(url) { this.owner.destinations.push(url); } },
    close() { this.closed = true; } };
}
function newPopup() {
  const value = popup();
  value.location.owner = value;
  return value;
}

test('ordinary links are not intercepted or turned into SSO requests', async () => {
  let calls = 0;
  const api = helper(() => { calls++; });
  assert.equal(api.isCatalogLink(link), true);
  assert.equal(api.isCatalogLink('https://platform.kodeks.expert/docs/'), true);
  assert.equal(api.isCatalogLink('https://example.org/document'), false);
  await api.openCatalogSsoLink('https://example.org/document', () => { calls++; });
  assert.equal(calls, 0);
});
test('popup opens and loses its opener synchronously before awaiting fresh ticket', async () => {
  const opened = newPopup();
  let resolve;
  const api = helper(() => opened);
  const work = api.openCatalogSsoLink(link, () => new Promise(done => { resolve = done; }));
  assert.equal(opened.opener, null);
  assert.deepEqual(opened.destinations, []);
  resolve({ url: 'https://cabinet-05.kodeks.expert/sso?ticket=fresh' });
  await work;
  assert.deepEqual(opened.destinations, ['https://cabinet-05.kodeks.expert/sso?ticket=fresh']);
});
test('each click asks for a fresh ticket rather than retaining the previous URL', async () => {
  const popups = [];
  let calls = 0;
  const api = helper(() => { const opened = newPopup(); popups.push(opened); return opened; });
  const create = async () => ({ url: `https://cabinet-05.kodeks.expert/sso?ticket=${++calls}` });
  await api.openCatalogSsoLink(link, create);
  await api.openCatalogSsoLink(link, create);
  assert.equal(calls, 2);
  assert.notEqual(popups[0].destinations[0], popups[1].destinations[0]);
});
test('blocked popups show a warning without issuing an unused ticket', async () => {
  let calls = 0;
  const api = helper(() => null);
  await api.openCatalogSsoLink(link, async () => { calls++; });
  assert.equal(calls, 0);
  assert.equal(api.notices[0][1].variant, 'warning');
});
test('failed ticket request closes the blank window and shows a generic error', async () => {
  const opened = newPopup();
  const api = helper(() => opened);
  await api.openCatalogSsoLink(link, async () => { throw new Error('private upstream details'); });
  assert.equal(opened.closed, true);
  assert.equal(api.notices[0][1].variant, 'error');
  assert.equal(api.notices[0][0].includes('private upstream details'), false);
});
test('invalid redirect response closes the window without navigating off catalog', async () => {
  const opened = newPopup();
  const api = helper(() => opened);
  await api.openCatalogSsoLink(link, async () => ({ url: 'https://other.example/sso' }));
  assert.equal(opened.closed, true);
  assert.deepEqual(opened.destinations, []);
});
