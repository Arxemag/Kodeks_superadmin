import { enqueueSnackbar } from 'notistack';

export function isCatalogLink(value: string): boolean {
  try {
    const url = new URL(value);
    return url.protocol === 'https:' && url.hostname.endsWith('.kodeks.expert');
  } catch { return false; }
}

/** Open the popup synchronously while the browser still has the click gesture. */
export async function openCatalogSsoLink(
  link: string,
  createTicket: (body: { link: string }) => Promise<{ url: string }>,
): Promise<void> {
  if (!isCatalogLink(link)) return;
  let popup: Window | null = null;
  try {
    popup = window.open('about:blank', '_blank');
    if (!popup) {
      enqueueSnackbar('Разрешите открытие окна кабинета', { variant: 'warning' });
      return;
    }
    popup.opener = null;
    const { url } = await createTicket({ link });
    if (!isCatalogLink(url)) throw new Error('Invalid SSO response');
    popup.location.replace(url);
  } catch {
    popup?.close();
    enqueueSnackbar('Не удалось открыть кабинет. Попробуйте войти в портал заново', { variant: 'error' });
  }
}
