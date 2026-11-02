"""Add the actual department snapshot to the existing durable create-user event.

No new producer, default group or transaction is introduced. Defaults to a plan.
"""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import shutil

# Sibling module contains the shared reviewed token matcher and atomic writer.
from install import atomic_write, compact, replace_fragment


RELATIVE_TARGET = Path('src/features/user/application/commands/create-user/create-user.command.handler.ts')
OLD_PAYLOAD = '''const payload = JSON.stringify({
  reg: user.userCompany.company.reg.regNumber,
  uid: user.email.split('@')[0],
  psw: user.userCompany.teAuth,
  mail: user.email,
  name: user.fullName,
  org: '',
  pos: '',
  telephon: user.phone,
  end: '',
});'''
NEW_PAYLOAD = '''// Kodeks: persist the actual departments in the same CREATE_USER outbox event.
        const departments = user.userCompany.departments;
        if (!Array.isArray(departments) || departments.some((department) =>
          !department || typeof department.id !== 'string' || !department.id.trim() ||
          typeof department.title !== 'string' || !department.title.trim()
        )) {
          throw new Error('The company user department snapshot is not initialized');
        }
        const payload = JSON.stringify({
          reg: user.userCompany.company.reg.regNumber,
          uid: user.email.split('@')[0],
          psw: user.userCompany.teAuth,
          mail: user.email,
          name: user.fullName,
          org: '',
          pos: '',
          telephon: user.phone,
          end: '',
          departments: departments.map((department) => ({ id: department.id, title: department.title })),
        });'''


def patch(source):
    # Preserve the known transaction that persists UserEntity and OutboxEntity.
    for fragment in ('await this.dataSource.transaction',
                     'await manager.getRepository(OutboxEntity).save(outbox);',
                     'await manager.getRepository(UserEntity).save(user);'):
        replace_fragment(source, fragment, fragment)
    current = compact(source)
    needle = compact(NEW_PAYLOAD)
    if any(current[index:index + len(needle)] == needle for index in range(len(current) - len(needle) + 1)):
        replace_fragment(source, NEW_PAYLOAD, NEW_PAYLOAD)
        return source
    return replace_fragment(source, OLD_PAYLOAD, NEW_PAYLOAD)


def install(backend_root, *, apply=False, backup_dir=None):
    root = Path(backend_root).resolve(strict=True)
    target = (root / RELATIVE_TARGET).resolve(strict=True)
    if not target.is_relative_to(root):
        raise ValueError('Target escapes backend root')
    original = target.read_bytes()
    updated = patch(original.decode('utf-8')).encode('utf-8')
    result = {'applied': False, 'changed_files': [str(target)] if updated != original else []}
    if not apply or updated == original:
        return result
    if backup_dir is None or not Path(backup_dir).is_absolute():
        raise ValueError('--apply requires an absolute --backup-dir')
    backup = Path(backup_dir).resolve()
    if backup.exists():
        raise ValueError('Use a new private backup directory')
    backup.mkdir(parents=True, mode=0o700)
    destination = backup / RELATIVE_TARGET
    destination.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(target, destination)
    (backup / 'manifest.json').write_text(json.dumps({
        'path': str(target), 'sha256': hashlib.sha256(original).hexdigest(),
    }, indent=2) + '\n', encoding='utf-8')
    atomic_write(target, updated, target.stat().st_mode & 0o777)
    return {**result, 'applied': True, 'backup_dir': str(backup)}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--backend-root', type=Path, required=True)
    parser.add_argument('--apply', action='store_true')
    parser.add_argument('--backup-dir', type=Path)
    args = parser.parse_args(argv)
    try:
        print(json.dumps(install(args.backend_root, apply=args.apply, backup_dir=args.backup_dir), indent=2))
    except (OSError, ValueError) as error:
        parser.exit(1, f'Department snapshot integration failed: {error}\n')


if __name__ == '__main__':
    main()
