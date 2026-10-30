"""Plan/apply the reviewed SSO additions to existing ITR Hub source files only."""
from __future__ import annotations

import argparse
from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import tempfile


PACKAGE = Path(__file__).resolve().parent
OLD_HANDLER = '''
event.stopPropagation();
if (href || isResolvingLink || !structureId || !module.name) { return; }
event.preventDefault();
setIsResolvingLink(true);
try {
  const link = (await getCabinetLink({ structId: structureId, cabinetName: module.name, }).unwrap())?.trim();
  if (link) { window.open(link, '_blank', 'noopener'); }
} catch {} finally { setIsResolvingLink(false); }
'''
NEW_HANDLER = '''
    // Kodeks SSO: fresh ticket for each click, never persist it in the tree.
    event.stopPropagation();
    if (href && !isCatalogLink(href)) return;
    event.preventDefault();
    if (isResolvingLink || (!href && (!structureId || !module.name))) return;
    const popup = window.open('about:blank', '_blank');
    if (!popup) {
      enqueueSnackbar('Разрешите открытие окна кабинета', { variant: 'warning' });
      return;
    }
    popup.opener = null;
    setIsResolvingLink(true);
    try {
      let resolvedLink = href;
      if (!resolvedLink) {
        const structId = structureId;
        const cabinetName = module.name;
        if (!structId || !cabinetName) throw new Error('Cabinet link is missing');
        resolvedLink = (await getCabinetLink({ structId, cabinetName }).unwrap())?.trim() ?? '';
      }
      const link = resolvedLink;
      if (!link) throw new Error('Cabinet link is missing');
      if (!isCatalogLink(link)) { popup.location.replace(link); return; }
      const { url } = await createCabinetSsoLink({ link }).unwrap();
      if (!isCatalogLink(url)) throw new Error('Invalid SSO response');
      popup.location.replace(url);
    } catch {
      popup.close();
      enqueueSnackbar('Не удалось открыть кабинет. Попробуйте войти в портал заново', { variant: 'error' });
    } finally {
      setIsResolvingLink(false);
    }
  '''
PREVIOUS_SSO_HANDLER = NEW_HANDLER.replace('''      let resolvedLink = href;
      if (!resolvedLink) {
        const structId = structureId;
        const cabinetName = module.name;
        if (!structId || !cabinetName) throw new Error('Cabinet link is missing');
        resolvedLink = (await getCabinetLink({ structId, cabinetName }).unwrap())?.trim() ?? '';
      }
      const link = resolvedLink;''', '''      const link = href || (await getCabinetLink({ structId: structureId, cabinetName: module.name }).unwrap())?.trim();''')
INDEX_EXPORT = "export { useCreateCabinetSsoLinkMutation } from './api/ssoApi';"
SIDEBAR_LINK = '''<Link key={`view-link-${idx}`} href={link.link} target='_blank' rel='noopener' underline='hover' variant='body2'>{link.name || link.link}</Link>'''
SIDEBAR_LINK_SSO = '''<Link key={`view-link-${idx}`} href={link.link} target='_blank' rel='noopener' underline='hover' variant='body2' onClick={(event) => handleCatalogSsoOpen(event, link.link)}>{link.name || link.link}</Link>'''
SIDEBAR_HANDLER = '''const [createCabinetSsoLink] = useCreateCabinetSsoLinkMutation();
  const handleCatalogSsoOpen = (event: React.MouseEvent, link: string) => {
    if (!isCatalogLink(link)) return;
    event.preventDefault();
    event.stopPropagation();
    void openCatalogSsoLink(link, (body) => createCabinetSsoLink(body).unwrap());
  };'''


def tokens(source):
    pattern = re.compile(r'''\s+|//[^\n]*|/\*.*?\*/|'(?:\\.|[^'\\])*'|"(?:\\.|[^"\\])*"|[A-Za-z_$][\w$]*|[^\s]''', re.S)
    return [(m.group(), m.start(), m.end()) for m in pattern.finditer(source)
            if not m.group().isspace() and not m.group().startswith(('//', '/*'))]


def compact(source):
    return [value for value, _, _ in tokens(source)]


def replace_fragment(source, old, new):
    stream, needle = tokens(source), compact(old)
    matches = [index for index in range(len(stream) - len(needle) + 1)
               if [value for value, _, _ in stream[index:index + len(needle)]] == needle]
    if len(matches) != 1:
        raise ValueError(f"Expected one known source fragment, found {len(matches)}: {old[:75]}")
    index = matches[0]
    return source[:stream[index][1]] + new + source[stream[index + len(needle) - 1][2]:]


def patch_module(source):
    imported = "import { ExpertSsoController } from './api/sso.controller';"
    property_ = 'controllers: [ExpertSsoController],'
    if 'ExpertSsoController' in source:
        # Both exact constructs must already exist; do not repair partial patches.
        if not imported in source or not re.search(r'controllers\s*:\s*\[\s*ExpertSsoController\s*\]', source):
            raise ValueError('Existing ExpertSsoController registration differs from this package')
        return source
    if re.search(r'\bcontrollers\s*:', source):
        raise ValueError('ExpertModule already has a controllers property; review required')
    source = replace_fragment(source, "import { HttpModule } from '@nestjs/axios';",
                              "import { HttpModule } from '@nestjs/axios';\n" + imported)
    matches = list(re.finditer(r'(?m)^(\s*)providers\s*:\s*\[', source))
    if len(matches) != 1 or 'export class ExpertModule' not in source:
        raise ValueError('ExpertModule providers declaration differs from the reviewed source')
    match = matches[0]
    return source[:match.start()] + match[1] + property_ + '\n' + source[match.start():]


def handler_span(source):
    stream = tokens(source)
    starts = [i for i in range(len(stream) - 3) if [t[0] for t in stream[i:i + 4]] == ['const', 'handleOpen', '=', 'async']]
    if len(starts) != 1:
        raise ValueError('Expected one async ModuleItem handleOpen declaration')
    index = starts[0] + 4
    while index < len(stream) - 2:
        if [t[0] for t in stream[index:index + 3]] == ['=', '>', '{']:
            begin = index + 2
            depth = 1
            for end in range(begin + 1, len(stream)):
                depth += (stream[end][0] == '{') - (stream[end][0] == '}')
                if depth == 0:
                    return stream[begin][2], stream[end][1]
            break
        index += 1
    raise ValueError('ModuleItem handleOpen body could not be identified')


def patch_module_item(source):
    begin, end = handler_span(source)
    current = compact(source[begin:end])
    if current in (compact(NEW_HANDLER), compact(PREVIOUS_SSO_HANDLER)):
        required = ['useCreateCabinetSsoLinkMutation', 'isCatalogLink', 'enqueueSnackbar']
        if not all(name in source[:begin] for name in required):
            raise ValueError('Incomplete existing SSO handler integration')
        return source if current == compact(NEW_HANDLER) else source[:begin] + '\n' + NEW_HANDLER + source[end:]
    if compact(source[begin:end]) != compact(OLD_HANDLER):
        raise ValueError('ModuleItem handleOpen differs from the reviewed source; no changes written')
    source = source[:begin] + '\n' + NEW_HANDLER + source[end:]
    source = replace_fragment(source, "import { useLazyGetCabinetLinkQuery } from '~/entities/cabinet';",
                              "import { useLazyGetCabinetLinkQuery, useCreateCabinetSsoLinkMutation } from '~/entities/cabinet';\n"
                              "import { isCatalogLink } from '~/entities/cabinet/api/catalogSso';\n"
                              "import { enqueueSnackbar } from 'notistack';")
    return replace_fragment(source, 'const [getCabinetLink] = useLazyGetCabinetLinkQuery();',
                            'const [getCabinetLink] = useLazyGetCabinetLinkQuery();\n'
                            '  const [createCabinetSsoLink] = useCreateCabinetSsoLinkMutation();')


def patch_index(source):
    if INDEX_EXPORT in source:
        return source
    if 'useCreateCabinetSsoLinkMutation' in source or "from './api/cabinetApi';" not in source:
        raise ValueError('Cabinet public exports differ from the reviewed source')
    return source.rstrip() + '\n' + INDEX_EXPORT + '\n'


def patch_sidebar(source):
    if 'handleCatalogSsoOpen' in source:
        # Refuse partial/different integrations, including an unconnected handler.
        replace_fragment(source, SIDEBAR_HANDLER, SIDEBAR_HANDLER)
        replace_fragment(source, SIDEBAR_LINK_SSO, SIDEBAR_LINK_SSO)
        replace_fragment(source, "import { useLazyGetCabinetLinkQuery, useCreateCabinetSsoLinkMutation } from '~/entities/cabinet';",
                         "import { useLazyGetCabinetLinkQuery, useCreateCabinetSsoLinkMutation } from '~/entities/cabinet';")
        replace_fragment(source, "import { isCatalogLink, openCatalogSsoLink } from '~/entities/cabinet/api/catalogSso';",
                         "import { isCatalogLink, openCatalogSsoLink } from '~/entities/cabinet/api/catalogSso';")
        return source
    if 'useCreateCabinetSsoLinkMutation' in source or 'openCatalogSsoLink' in source:
        raise ValueError('Partial/different SSO integration already exists in DepartmentSidebar')
    source = replace_fragment(source, SIDEBAR_LINK, SIDEBAR_LINK_SSO)
    source = replace_fragment(source, "import { useLazyGetCabinetLinkQuery } from '~/entities/cabinet';",
                              "import { useLazyGetCabinetLinkQuery, useCreateCabinetSsoLinkMutation } from '~/entities/cabinet';\n"
                              "import { isCatalogLink, openCatalogSsoLink } from '~/entities/cabinet/api/catalogSso';")
    return replace_fragment(source, 'const [getCabinetLink] = useLazyGetCabinetLinkQuery();',
                            'const [getCabinetLink] = useLazyGetCabinetLinkQuery();\n  ' + SIDEBAR_HANDLER)


@dataclass
class Change:
    path: Path
    content: bytes
    section: str
    relative: str


def plan(backend_root, frontend_root):
    roots = {'backend': Path(backend_root).resolve(strict=True), 'frontend': Path(frontend_root).resolve(strict=True)}
    changes = []
    def target(section, relative):
        path = (roots[section] / relative).resolve()
        if not path.is_relative_to(roots[section]):
            raise ValueError(f'Target escapes {section} root: {relative}')
        return path
    additions = [
        ('backend', 'sso.controller.ts', 'src/features/expert/api/sso.controller.ts'),
        ('backend', 'sso.policy.ts', 'src/features/expert/api/sso.policy.ts'),
        ('frontend', 'ssoApi.ts', 'src/entities/cabinet/api/ssoApi.ts'),
        ('frontend', 'catalogSso.ts', 'src/entities/cabinet/api/catalogSso.ts'),
    ]
    for section, package_name, relative in additions:
        path = target(section, relative)
        content = (PACKAGE / section / package_name).read_bytes()
        if path.exists():
            if path.read_bytes() != content:
                raise ValueError(f'Existing new module differs from this package: {path}')
        else:
            changes.append(Change(path, content, section, relative))
    for section, relative, patch in [
        ('backend', 'src/features/expert/expert.module.ts', patch_module),
        ('frontend', 'src/shared/ui/OrgTree/ModuleItem.tsx', patch_module_item),
        ('frontend', 'src/entities/cabinet/index.ts', patch_index),
        ('frontend', 'src/features/departmentSidebar/ui/DepartmentSidebar.tsx', patch_sidebar),
    ]:
        path = target(section, relative)
        content = path.read_bytes()
        updated = patch(content.decode('utf-8')).encode('utf-8')
        if content != updated:
            changes.append(Change(path, updated, section, relative))
    return changes


def atomic_write(path, content, mode=0o644):
    missing = []
    parent = path.parent
    while not parent.exists():
        missing.append(parent)
        parent = parent.parent
    for directory in reversed(missing):
        owner = directory.parent.stat()
        directory.mkdir()
        if hasattr(os, 'chown'):
            os.chown(directory, owner.st_uid, owner.st_gid)
    owner = path.stat() if path.exists() else path.parent.stat()
    fd, temporary = tempfile.mkstemp(prefix='.itrhub-sso-', dir=path.parent)
    try:
        with os.fdopen(fd, 'wb') as stream:
            stream.write(content)
        os.chmod(temporary, mode)
        if hasattr(os, 'chown'):
            os.chown(temporary, owner.st_uid, owner.st_gid)
        os.replace(temporary, path)
    finally:
        Path(temporary).unlink(missing_ok=True)


def install(backend_root, frontend_root, *, apply=False, backup_dir=None):
    changes = plan(backend_root, frontend_root)
    result = {'applied': False, 'changed_files': [str(change.path) for change in changes]}
    if not apply or not changes:
        return result
    if backup_dir is None or not Path(backup_dir).is_absolute():
        raise ValueError('--apply requires an absolute --backup-dir')
    backup = Path(backup_dir).resolve()
    if backup.exists():
        raise ValueError('Use a new backup directory for this installation')
    backup.mkdir(parents=True, mode=0o700)
    originals = {}
    manifest = []
    for change in changes:
        path = change.path
        original = path.read_bytes() if path.exists() else None
        mode = path.stat().st_mode & 0o777 if original is not None else 0o644
        originals[path] = (original, mode)
        if original is not None:
            saved = backup / change.section / change.relative
            saved.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(path, saved)
        manifest.append({'path': str(path), 'existed': original is not None,
                         'sha256': hashlib.sha256(original).hexdigest() if original is not None else None})
    (backup / 'manifest.json').write_text(json.dumps(manifest, indent=2) + '\n', encoding='utf-8')
    written = []
    try:
        for change in changes:
            atomic_write(change.path, change.content, originals[change.path][1])
            written.append(change.path)
    except Exception:
        for path in reversed(written):
            original, mode = originals[path]
            if original is None:
                path.unlink(missing_ok=True)
            else:
                atomic_write(path, original, mode)
        raise
    return {**result, 'applied': True, 'backup_dir': str(backup)}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--backend-root', type=Path, required=True)
    parser.add_argument('--frontend-root', type=Path, required=True)
    parser.add_argument('--apply', action='store_true')
    parser.add_argument('--backup-dir', type=Path)
    args = parser.parse_args(argv)
    try:
        print(json.dumps(install(args.backend_root, args.frontend_root, apply=args.apply, backup_dir=args.backup_dir), indent=2))
    except (OSError, ValueError) as error:
        parser.exit(1, f'ITR Hub SSO integration failed: {error}\n')


if __name__ == '__main__':
    main()
