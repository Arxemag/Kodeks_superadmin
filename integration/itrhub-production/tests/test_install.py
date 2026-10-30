import importlib.util
from pathlib import Path
import sys

import pytest


PACKAGE = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('itrhub_sso_install', PACKAGE / 'install.py')
installer = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = installer
spec.loader.exec_module(installer)

MODULE = '''import { HttpModule } from '@nestjs/axios';
import { Module } from '@nestjs/common';
@Module({
  imports: [HttpModule],
  providers: [ExpertService],
  exports: [ExpertService],
})
export class ExpertModule {}
'''
ITEM = '''import { useLazyGetCabinetLinkQuery } from '~/entities/cabinet';
const [getCabinetLink] = useLazyGetCabinetLinkQuery();
const handleOpen = async (event: React.MouseEvent) => {
  event.stopPropagation();
  if (href || isResolvingLink || !structureId || !module.name) {
    return;
  }
  event.preventDefault();
  setIsResolvingLink(true);
  try {
    const link = (
      await getCabinetLink({
        structId: structureId,
        cabinetName: module.name,
      }).unwrap()
    )?.trim();
    if (link) {
      window.open(link, '_blank', 'noopener');
    }
  } catch {
    //
  } finally {
    setIsResolvingLink(false);
  }
};
const view = <Link href={href || undefined} onClick={handleOpen}>{module.name}</Link>;
'''
INDEX = "export { cabinetApi } from './api/cabinetApi';\n"
SIDEBAR = '''import { useLazyGetCabinetLinkQuery } from '~/entities/cabinet';
const [getCabinetLink] = useLazyGetCabinetLinkQuery();
const titleInputRef = useRef<HTMLInputElement>(null);
const links = <Link
  key={`view-link-${idx}`}
  href={link.link}
  target='_blank'
  rel='noopener'
  underline='hover'
  variant='body2'
>{link.name || link.link}</Link>;
const external = <Link href={otherLink}>Other destination</Link>;
'''


@pytest.fixture
def roots(tmp_path):
    backend, frontend = tmp_path / 'backend', tmp_path / 'frontend'
    for root, relative, source in [(backend, 'src/features/expert/expert.module.ts', MODULE),
        (frontend, 'src/shared/ui/OrgTree/ModuleItem.tsx', ITEM),
        (frontend, 'src/features/departmentSidebar/ui/DepartmentSidebar.tsx', SIDEBAR),
        (frontend, 'src/entities/cabinet/index.ts', INDEX)]:
        path = root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(source, encoding='utf-8')
    return backend, frontend


def test_module_patch_registers_one_controller_and_preserves_other_providers():
    patched = installer.patch_module(MODULE)
    assert patched.count('controllers: [ExpertSsoController]') == 1
    assert 'providers: [ExpertService]' in patched
    assert installer.patch_module(patched) == patched
    with pytest.raises(ValueError):
        installer.patch_module(MODULE.replace('  providers:', '  controllers: [OtherController],\n  providers:'))


def test_handler_opens_popup_before_await_and_does_not_change_tree_urls():
    patched = installer.patch_module_item(ITEM)
    assert patched.index("window.open('about:blank'") < patched.index('await getCabinetLink')
    assert 'popup.opener = null' in patched
    assert 'popup.close()' in patched
    assert 'createCabinetSsoLink({ link }).unwrap()' in patched
    assert 'if (href && !isCatalogLink(href)) return;' in patched
    assert 'href={href || undefined}' in patched
    assert 'getCabinetLink({ structId: structureId, cabinetName: module.name })' in patched
    assert installer.patch_module_item(patched) == patched


def test_unknown_handler_refuses_instead_of_overwriting_new_logic():
    with pytest.raises(ValueError, match='differs'):
        installer.patch_module_item(ITEM.replace('event.stopPropagation();', 'event.stopPropagation(); unknownBusinessRule();'))


def test_whitespace_and_comment_only_changes_are_accepted():
    changed = ITEM.replace('event.stopPropagation();', 'event . stopPropagation ( ) ; // old comment')
    assert 'createCabinetSsoLink' in installer.patch_module_item(changed)


def test_dry_run_changes_nothing_and_apply_backs_up_only_touched_originals(roots, tmp_path):
    backend, frontend = roots
    original = {path: path.read_bytes() for root in roots for path in root.rglob('*') if path.is_file()}
    result = installer.install(backend, frontend)
    assert not result['applied'] and len(result['changed_files']) == 8
    assert all(path.read_bytes() == content for path, content in original.items())
    backup = tmp_path / 'backup'
    assert installer.install(backend, frontend, apply=True, backup_dir=backup)['applied']
    assert len(list(backup.rglob('*.ts'))) == 2  # module and frontend barrel
    assert len(list(backup.rglob('*.tsx'))) == 2
    assert (backup / 'backend/src/features/expert/expert.module.ts').read_bytes() == original[backend / 'src/features/expert/expert.module.ts']
    assert not installer.install(backend, frontend, apply=True, backup_dir=backup)['applied']


def test_any_mismatch_aborts_all_files_before_backup(roots, tmp_path):
    backend, frontend = roots
    item = frontend / 'src/shared/ui/OrgTree/ModuleItem.tsx'
    item.write_text(ITEM.replace('setIsResolvingLink(false);', 'setIsResolvingLink(false); doSomethingElse();'), encoding='utf-8')
    with pytest.raises(ValueError):
        installer.install(backend, frontend, apply=True, backup_dir=tmp_path / 'backup')
    assert not (backend / 'src/features/expert/api/sso.controller.ts').exists()
    assert not (tmp_path / 'backup').exists()


def test_new_module_conflict_is_never_overwritten(roots):
    backend, frontend = roots
    path = backend / 'src/features/expert/api/sso.controller.ts'
    path.parent.mkdir()
    path.write_text('existing user implementation', encoding='utf-8')
    with pytest.raises(ValueError, match='differs'):
        installer.install(backend, frontend)
    assert path.read_text(encoding='utf-8') == 'existing user implementation'


def test_partial_write_failure_restores_originals(roots, tmp_path, monkeypatch):
    backend, frontend = roots
    original_module = (backend / 'src/features/expert/expert.module.ts').read_bytes()
    real_write = installer.atomic_write
    calls = 0
    def failing_write(*args, **kwargs):
        nonlocal calls
        calls += 1
        if calls == 6:
            raise OSError('simulated filesystem failure')
        return real_write(*args, **kwargs)
    monkeypatch.setattr(installer, 'atomic_write', failing_write)
    with pytest.raises(OSError):
        installer.install(backend, frontend, apply=True, backup_dir=tmp_path / 'backup')
    assert (backend / 'src/features/expert/expert.module.ts').read_bytes() == original_module
    assert not (backend / 'src/features/expert/api/sso.controller.ts').exists()
    assert not (frontend / 'src/entities/cabinet/api/ssoApi.ts').exists()


def test_sidebar_patch_is_idempotent_and_preserves_unrelated_links():
    patched = installer.patch_sidebar(SIDEBAR)
    assert installer.patch_sidebar(patched) == patched
    assert 'onClick={(event) => handleCatalogSsoOpen(event, link.link)}' in patched
    assert '<Link href={otherLink}>Other destination</Link>' in patched
    assert 'const titleInputRef = useRef<HTMLInputElement>(null);' in patched
    assert 'if (!isCatalogLink(link)) return;' in patched
    assert 'event.preventDefault();' in patched
    assert 'void openCatalogSsoLink(link, (body) => createCabinetSsoLink(body).unwrap());' in patched
    assert patched.count('const [createCabinetSsoLink]') == 1


def test_sidebar_unknown_fragment_refuses_all_changes():
    with pytest.raises(ValueError):
        installer.patch_sidebar(SIDEBAR.replace("variant='body2'", "variant='h4'"))
    with pytest.raises(ValueError):
        installer.patch_sidebar(SIDEBAR.replace('const [getCabinetLink]', 'const [differentHook]'))
