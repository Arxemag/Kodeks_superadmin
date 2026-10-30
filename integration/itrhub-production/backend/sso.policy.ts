/** Pure policy: identity comes from the authenticated actor and database only. */
export class SsoPolicyError extends Error {
  status: number;
  constructor(message: string, status = 400) { super(message); this.status = status; }
}
export type RegistryRow = { reg_number: string; base_url: string };
export type Actor = { userId: string | number; role: string; companyId?: string | number };
type CompanyAccount = {
  markDeletion: boolean; status: string;
  user?: { email: string; available: boolean };
  company?: { id: string | number; available: boolean; reg?: { regNumber: string | number } };
};
const controls = /[\x00-\x1f\x7f-\x9f]/;

export function catalogUrl(value: unknown): URL {
  if (typeof value !== 'string' || !value || value.length > 2048 || controls.test(value) || value.includes('\\')) {
    throw new SsoPolicyError('Некорректная ссылка кабинета');
  }
  let url: URL;
  try { url = new URL(value); } catch { throw new SsoPolicyError('Некорректная ссылка кабинета'); }
  if (url.protocol !== 'https:' || !url.hostname.endsWith('.kodeks.expert') || url.username || url.password || url.port) {
    throw new SsoPolicyError('Недопустимый адрес кабинета');
  }
  const path = url.pathname + url.search + url.hash;
  let decoded: string;
  try { decoded = decodeURIComponent(path); } catch { throw new SsoPolicyError('Некорректный путь кабинета'); }
  if (/%25/i.test(path) || !path.startsWith('/') || path.startsWith('//') || decoded.startsWith('//')
      || controls.test(decoded) || decoded.includes('\\')) {
    throw new SsoPolicyError('Некорректный путь кабинета');
  }
  return url;
}

export function readLink(body: unknown): URL {
  if (!body || typeof body !== 'object' || Array.isArray(body)
      || Object.keys(body).some(key => key !== 'link')) {
    throw new SsoPolicyError('Ожидается только ссылка кабинета');
  }
  return catalogUrl((body as { link?: unknown }).link);
}

export function roleKind(role: string, companyRoles: readonly string[], staffRoles: readonly string[]): 'company' | 'staff' {
  if (companyRoles.includes(role)) return 'company';
  if (staffRoles.includes(role)) return 'staff';
  throw new SsoPolicyError('Вход в кабинет для этой роли недоступен', 403);
}

export function authCookie(value: unknown): string {
  if (typeof value !== 'string' || !value || value.length > 4096 || controls.test(value)) {
    throw new SsoPolicyError('Сессия каталога отсутствует. Войдите в портал заново', 401);
  }
  return value;
}

export function companyIdentity(actor: Actor, account: CompanyAccount | null, fired: string): { reg: string; uid: string } {
  if (!account || account.markDeletion !== false || typeof account.status !== 'string' || account.status === fired
      || account.status !== actor.role || account.user?.available !== true || account.company?.available !== true
      || actor.companyId == null || account.company?.id == null
      || String(actor.companyId) !== String(account.company.id)) {
    throw new SsoPolicyError('Нет доступа к компании', 403);
  }
  const email = account.user?.email;
  const uid = typeof email === 'string' ? email.split('@', 1)[0] : '';
  const reg = account.company?.reg?.regNumber;
  if (!uid || controls.test(uid) || uid.includes(':') || reg == null || !String(reg).trim()) {
    throw new SsoPolicyError('Данные кабинета компании не настроены', 403);
  }
  return { reg: String(reg), uid };
}

export function supportUid(cookie: string): string {
  if (!/^[A-Za-z0-9+/]+={0,2}$/.test(cookie)) throw new SsoPolicyError('Сессия каталога недействительна. Войдите заново', 401);
  const decoded = Buffer.from(cookie, 'base64').toString('utf8');
  const colon = decoded.indexOf(':');
  const uid = colon < 0 ? '' : decoded.slice(0, colon);
  if (!uid || controls.test(uid)) throw new SsoPolicyError('Сессия каталога недействительна. Войдите заново', 401);
  return uid;
}

export function hostAliases(raw: string | undefined): Record<string, string> {
  if (!raw) return {};
  let value: unknown;
  try { value = JSON.parse(raw); } catch { throw new SsoPolicyError('Ошибка конфигурации SSO', 503); }
  if (!value || typeof value !== 'object' || Array.isArray(value)) throw new SsoPolicyError('Ошибка конфигурации SSO', 503);
  const aliases: Record<string, string> = {};
  for (const [host, reg] of Object.entries(value)) {
    if (host !== host.toLowerCase() || typeof reg !== 'string' || !reg.trim()
        || !/^[a-z0-9.-]+\.kodeks\.expert$/.test(host)) throw new SsoPolicyError('Ошибка конфигурации SSO', 503);
    aliases[host] = reg;
  }
  return aliases;
}

export function registryRows(value: unknown): RegistryRow[] {
  if (!Array.isArray(value)) throw new SsoPolicyError('Справочник каталогов недоступен', 503);
  for (const row of value) {
    if (!row || typeof row.reg_number !== 'string' || !row.reg_number || typeof row.base_url !== 'string') {
      throw new SsoPolicyError('Справочник каталогов недоступен', 503);
    }
    const base = catalogUrl(row.base_url);
    if (base.pathname !== '/' || base.search || base.hash) throw new SsoPolicyError('Ошибка адреса каталога', 503);
  }
  return value;
}

export function registryForHost(target: URL, rows: RegistryRow[], aliases: Record<string, string>, reg?: string): RegistryRow {
  const canonical = rows.filter(row => new URL(row.base_url).hostname === target.hostname);
  if (canonical.length > 1) throw new SsoPolicyError('Адрес каталога неоднозначен', 503);
  const selectedReg = canonical[0]?.reg_number ?? aliases[target.hostname];
  if (!selectedReg || (reg !== undefined && reg !== selectedReg)) throw new SsoPolicyError('Кабинет не относится к компании', 403);
  const selected = rows.filter(row => row.reg_number === selectedReg);
  if (selected.length !== 1) throw new SsoPolicyError('Каталог не настроен', 503);
  return selected[0];
}

export function issuerUrl(value: unknown, row: RegistryRow): string {
  const url = catalogUrl(value);
  if (url.hostname !== new URL(row.base_url).hostname || url.pathname !== '/sso' || url.hash
      || [...url.searchParams.keys()].some(key => key !== 'ticket') || url.searchParams.getAll('ticket').length !== 1
      || !url.searchParams.get('ticket')) throw new SsoPolicyError('Некорректный ответ сервиса входа', 503);
  return url.toString();
}
