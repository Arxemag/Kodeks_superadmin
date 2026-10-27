"""Exercise deployment control flow with fake git and docker, never a server."""
from __future__ import annotations

import os
from pathlib import Path
import shutil
import subprocess

import pytest


SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "deploy.sh"


def _bash():
    if os.name == "nt":
        candidate = Path(r"C:\Program Files\Git\bin\bash.exe")
        return str(candidate) if candidate.exists() else None
    return shutil.which("bash")


def _shell_path(path: Path) -> str:
    value = str(path.resolve())
    if os.name == "nt":
        return "/" + value[0].lower() + value[2:].replace("\\", "/")
    return value


@pytest.fixture
def fake_deploy(tmp_path):
    bash = _bash()
    if not bash:
        pytest.skip("bash is required to test deployment")
    app = tmp_path / "app"
    app.mkdir()
    (app / ".env").write_text("", encoding="utf-8")
    tools = tmp_path / "bin"
    tools.mkdir()
    git = tools / "git"
    git.write_text("""#!/bin/sh
echo "git $*" >> "$DEPLOY_TRACE"
if [ "$1" = rev-parse ]; then echo "$TEST_RELEASE_COMMIT"; fi
""", encoding="utf-8")
    docker = tools / "docker"
    docker.write_text("""#!/bin/sh
echo "docker $*" >> "$DEPLOY_TRACE"
if [ "$2" = exec ] && [ "${TEST_READY:-1}" != 1 ]; then exit 1; fi
""", encoding="utf-8")
    sleep = tools / "sleep"
    sleep.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    for executable in (git, docker, sleep):
        executable.chmod(0o755)
    trace = tmp_path / "trace.txt"
    # Git for Windows prepends /mingw64/bin to the inherited PATH; set PATH
    # from the non-interactive shell startup so the fakes always come first.
    bash_env = tmp_path / "bash_env"
    bash_env.write_text(f'export PATH="{_shell_path(tools)}:$PATH"\n', encoding="utf-8")
    environment = dict(os.environ)
    environment.update({
        "PATH": str(tools) + os.pathsep + environment.get("PATH", ""),
        "APP_DIR": _shell_path(app),
        "BASH_ENV": _shell_path(bash_env),
        "DEPLOY_TRACE": _shell_path(trace),
        "TEST_RELEASE_COMMIT": "a" * 40,
    })

    def run(**overrides):
        proc = subprocess.run([bash, _shell_path(SCRIPT)], env={**environment, **overrides},
                              text=True, capture_output=True, timeout=20)
        commands = trace.read_text(encoding="utf-8").splitlines() if trace.exists() else []
        return proc, commands
    return run


def test_moved_release_is_rejected_before_reset_and_docker(fake_deploy):
    proc, commands = fake_deploy(DEPLOY_COMMIT="b" * 40)
    assert proc.returncode != 0
    assert "refusing deploy" in proc.stdout
    assert not any(command.startswith("git reset") or command.startswith("docker ") for command in commands)


def test_successful_candidate_migrates_and_leaves_worker_stopped(fake_deploy):
    proc, commands = fake_deploy(DEPLOY_COMMIT="a" * 40)
    assert proc.returncode == 0, proc.stderr
    assert "docker compose stop worker" in commands
    assert "docker compose run --rm --no-deps auth-api python -m scripts.init_db" in commands
    assert "docker compose up -d chromium auth-api" in commands
    assert "docker compose up -d worker" not in commands


def test_failed_readiness_never_starts_worker(fake_deploy):
    proc, commands = fake_deploy(DEPLOY_COMMIT="a" * 40, START_WORKER="1", TEST_READY="0")
    assert proc.returncode != 0
    assert "worker remains stopped" in proc.stdout
    assert "docker compose up -d worker" not in commands


def test_manual_activation_only_after_successful_readiness(fake_deploy):
    proc, commands = fake_deploy(DEPLOY_COMMIT="a" * 40, START_WORKER="1")
    assert proc.returncode == 0, proc.stderr
    health = next(i for i, command in enumerate(commands) if command.startswith("docker compose exec "))
    assert commands.index("docker compose up -d worker") > health
