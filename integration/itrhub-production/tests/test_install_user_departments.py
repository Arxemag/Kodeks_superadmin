import importlib.util
import json
from pathlib import Path
import shutil
import subprocess
import sys

import pytest


PACKAGE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PACKAGE))
try:
    spec = importlib.util.spec_from_file_location('itrhub_departments_install', PACKAGE / 'install_user_departments.py')
    installer = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(installer)
finally:
    sys.path.pop(0)

SOURCE = '''class CreateUserCommandHandler {
  async execute() {
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
    });
    const outboxData = { payload, eventType: 'CREATE_USER' };
    await this.dataSource.transaction(async manager => {
      await manager.getRepository(OutboxEntity).save(outbox);
      await manager.getRepository(UserEntity).save(user);
    });
  }
}'''


def test_exact_known_payload_patch_preserves_the_existing_transaction():
    updated = installer.patch(SOURCE)
    assert 'departments: departments.map((department) => ({ id: department.id, title: department.title }))' in updated
    assert updated.count('JSON.stringify(') == 1
    assert updated[updated.index('    const outboxData'):] == SOURCE[SOURCE.index('    const outboxData'):]
    assert installer.patch(updated) == updated


@pytest.mark.parametrize('change', ['payload', 'transaction'])
def test_unknown_source_fails_instead_of_replacing_business_logic(change):
    source = SOURCE.replace("org: '',", "org: 'changed',") if change == 'payload' else SOURCE.replace('.save(outbox)', '.save(otherEvent)')
    with pytest.raises(ValueError):
        installer.patch(source)


def test_plan_and_apply_backup_only_the_touched_handler(tmp_path):
    root = tmp_path / 'backend'
    target = root / installer.RELATIVE_TARGET
    target.parent.mkdir(parents=True)
    target.write_bytes(SOURCE.encode())
    assert not installer.install(root)['applied']
    assert target.read_bytes() == SOURCE.encode()
    backup = tmp_path / 'private-backup'
    assert installer.install(root, apply=True, backup_dir=backup)['applied']
    assert (backup / installer.RELATIVE_TARGET).read_bytes() == SOURCE.encode()
    assert not installer.install(root, apply=True, backup_dir=backup)['applied']


def run_payload(user):
    node = shutil.which('node')
    if not node:
        pytest.skip('Node runtime is needed to execute the actual JavaScript payload builder')
    program = '''const fs = require('node:fs'); const vm = require('node:vm');
const input = JSON.parse(fs.readFileSync(0, 'utf8'));
try {
  const result = vm.runInNewContext(input.source + '\\nJSON.parse(payload)', { user: input.user });
  process.stdout.write(JSON.stringify({ ok: true, result }));
} catch(error) { process.stdout.write(JSON.stringify({ ok: false, message: error.message })); }
'''
    completed = subprocess.run([node, '-e', program], input=json.dumps({'source': installer.NEW_PAYLOAD, 'user': user}),
                               text=True, capture_output=True, check=True)
    return json.loads(completed.stdout)


def user(departments):
    return {'email': 'member@example.test', 'fullName': 'Test', 'phone': '', 'userCompany': {
        'company': {'reg': {'regNumber': '465304'}}, 'teAuth': 'unused-test-password', 'departments': departments,
    }}


def test_payload_contains_actual_uuid_and_title_and_keeps_original_profile_fields():
    actual = [{'id': 'f23f0e81-08b9-490b-b567-87023140da30', 'title': 'Actual department', 'ignoredField': 'x'}]
    output = run_payload(user(actual))
    assert output['ok']
    payload = output['result']
    assert payload['departments'] == [{'id': actual[0]['id'], 'title': actual[0]['title']}]
    assert payload['reg'] == '465304' and payload['uid'] == 'member'
    assert payload['psw'] == 'unused-test-password'
    assert 'grp' not in payload


def test_explicit_empty_department_snapshot_is_preserved():
    output = run_payload(user([]))
    assert output['ok'] and output['result']['departments'] == []


@pytest.mark.parametrize('departments', [None, {}, [{'id': '', 'title': 'A'}], [{'id': 'uuid', 'title': ''}], [None]])
def test_missing_or_invalid_relation_cannot_silently_become_empty_snapshot(departments):
    assert not run_payload(user(departments))['ok']
    absent = user([])
    del absent['userCompany']['departments']
    assert not run_payload(absent)['ok']
