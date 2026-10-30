from pathlib import Path
import subprocess

import pytest

from scripts.install_sso_nginx import (
    DEFAULT_SITES, DEFAULT_SNIPPET, INCLUDE, add_include, install, plan_changes,
)


DOMAIN = "cabinet-02.kodeks.expert"
HTTP = f"server {{ listen 80; server_name {DOMAIN}; return 301 https://$host$request_uri; }}\n"
TLS = f'''server {{
    listen 443 ssl;
    server_name {DOMAIN};
    include /etc/nginx/ssl-nginx.conf;
    location / {{ proxy_pass http://82.148.21.243:7020; }}
}}
'''


@pytest.fixture
def nginx(tmp_path):
    root = tmp_path / "nginx"
    (root / "sites-enabled").mkdir(parents=True)
    (root / "nginx.conf").write_text("events {}\nhttp {}\n", encoding="utf-8")
    (root / "sites-enabled" / DOMAIN).write_text(HTTP + TLS, encoding="utf-8")
    return root


def run_ok(command, **kwargs):
    return subprocess.CompletedProcess(command, 0)


def test_only_matching_tls_server_changes_and_existing_bytes_are_preserved():
    another = TLS.replace(DOMAIN, "other.example")
    updated = add_include(HTTP + TLS + another, DOMAIN)
    assert updated.startswith(HTTP)
    assert updated.endswith(another)
    assert updated.count(f"include {INCLUDE};") == 1
    assert updated.replace(f"    include {INCLUDE};\n", "") == HTTP + TLS + another
    assert add_include(updated, DOMAIN) == updated


def test_comments_quotes_and_escaped_braces_do_not_change_server_selection():
    source = '# server { listen 443; }\n' + HTTP + TLS.replace(
        '    location /', '    set $left "{"; set $right "}"; set $escaped \\{; # } }\n    location /')
    updated = add_include(source, DOMAIN)
    assert updated.replace(f"    include {INCLUDE};\n", "") == source


@pytest.mark.parametrize("location", ["location = /sso {}", "location /sso {}", "location ~ ^/sso$ {}"])
def test_foreign_sso_location_is_refused(location):
    with pytest.raises(ValueError, match="Existing SSO"):
        add_include(HTTP + TLS.replace("    location /", location + "\n    location /"), DOMAIN)


@pytest.mark.parametrize("source", [HTTP, TLS + TLS, TLS[:-2]])
def test_missing_ambiguous_or_malformed_tls_server_is_refused(source):
    with pytest.raises(ValueError):
        add_include(source, DOMAIN)


def test_ipv6_tls_and_quoted_server_name_are_recognized():
    source = TLS.replace("listen 443 ssl", "listen [::]:443 ssl").replace(
        f"server_name {DOMAIN}", f'server_name "{DOMAIN}"')
    assert f"include {INCLUDE};" in add_include(source, DOMAIN)


def test_dry_run_validates_without_writes_or_reload(nginx):
    before = (nginx / "sites-enabled" / DOMAIN).read_bytes()
    commands = []
    result = install(nginx, sites=[DOMAIN], runner=lambda cmd, **kw: commands.append(cmd))
    assert not result["applied"]
    assert len(result["changed_files"]) == 2
    assert len(commands) == 1 and commands[0][:2] == ["nginx", "-t"]
    assert (nginx / "sites-enabled" / DOMAIN).read_bytes() == before
    assert not (nginx / "snippets").exists()


def test_apply_backs_up_original_then_validates_reloads_and_is_idempotent(nginx, tmp_path):
    backup = tmp_path / "backups"
    before = (nginx / "sites-enabled" / DOMAIN).read_bytes()
    commands = []
    runner = lambda cmd, **kw: commands.append(cmd)
    result = install(nginx, sites=[DOMAIN], apply=True, backup_dir=backup, runner=runner)
    assert result["applied"]
    assert (backup / "sites-enabled" / DOMAIN).read_bytes() == before
    assert (backup / "manifest.json").is_file()
    assert (nginx / "snippets/kodeks-sso.conf").read_bytes() == DEFAULT_SNIPPET.read_bytes()
    assert [cmd[:2] for cmd in commands] == [["nginx", "-t"], ["nginx", "-t"], ["systemctl", "reload"]]
    commands.clear()
    assert not install(nginx, sites=[DOMAIN], apply=True, backup_dir=backup, runner=runner)["applied"]
    assert len(commands) == 1


def test_foreign_snippet_is_not_overwritten(nginx):
    (nginx / "snippets").mkdir()
    (nginx / "snippets/kodeks-sso.conf").write_text("location = /sso { return 200; }", encoding="utf-8")
    with pytest.raises(ValueError, match="not managed"):
        plan_changes(nginx, sites=[DOMAIN])


def test_failed_precheck_does_not_create_backups_or_change_files(nginx, tmp_path):
    before = (nginx / "sites-enabled" / DOMAIN).read_bytes()
    def fail(command, **kwargs):
        raise subprocess.CalledProcessError(1, command)
    with pytest.raises(subprocess.CalledProcessError):
        install(nginx, sites=[DOMAIN], apply=True, backup_dir=tmp_path / "backup", runner=fail)
    assert (nginx / "sites-enabled" / DOMAIN).read_bytes() == before
    assert not (tmp_path / "backup").exists()


@pytest.mark.parametrize("fail_step,expected_reloads", [("postcheck", 0), ("reload", 2)])
def test_validation_or_reload_failure_restores_originals(nginx, tmp_path, fail_step, expected_reloads):
    site = nginx / "sites-enabled" / DOMAIN
    before = site.read_bytes()
    snippet = nginx / "snippets/kodeks-sso.conf"
    commands = []
    def runner(command, **kwargs):
        commands.append(command)
        if (fail_step == "postcheck" and len(commands) == 2) or (fail_step == "reload" and len(commands) == 3):
            assert snippet.exists()
            assert f"include {INCLUDE};" in site.read_text(encoding="utf-8")
            raise subprocess.CalledProcessError(1, command)
    with pytest.raises(RuntimeError, match="original files restored"):
        install(nginx, sites=[DOMAIN], apply=True, backup_dir=tmp_path / "backup", runner=runner)
    assert site.read_bytes() == before
    assert not snippet.exists()
    assert sum(command[0] == "systemctl" for command in commands) == expected_reloads


def test_modified_owned_snippet_is_backed_up_and_restored_on_error(nginx, tmp_path):
    target = nginx / "snippets/kodeks-sso.conf"
    target.parent.mkdir()
    old = DEFAULT_SNIPPET.read_bytes() + b"# previous revision\n"
    target.write_bytes(old)
    calls = []
    def runner(command, **kwargs):
        calls.append(command)
        if len(calls) == 2:
            raise subprocess.CalledProcessError(1, command)
    with pytest.raises(RuntimeError):
        install(nginx, sites=[DOMAIN], apply=True, backup_dir=tmp_path / "backup", runner=runner)
    assert target.read_bytes() == old
    assert (tmp_path / "backup/snippets/kodeks-sso.conf").read_bytes() == old


def test_target_symlink_cannot_escape_nginx_root(nginx, tmp_path):
    site = nginx / "sites-enabled" / DOMAIN
    outside = tmp_path / "outside"
    outside.write_bytes(site.read_bytes())
    site.unlink()
    try:
        site.symlink_to(outside)
    except OSError:
        pytest.skip("Creating symlinks requires platform privileges")
    with pytest.raises(ValueError, match="escapes"):
        plan_changes(nginx, sites=[DOMAIN])


def test_snippet_has_one_time_ticket_proxy_settings():
    snippet = DEFAULT_SNIPPET.read_text(encoding="utf-8")
    assert "location = /sso {" in snippet
    assert "proxy_pass http://127.0.0.1:8000/api/expert/sso;" in snippet
    assert "proxy_next_upstream off;" in snippet
    assert "access_log off;" in snippet
    assert "error_log /dev/null crit;" in snippet
    assert "proxy_cache off;" in snippet


def test_default_plan_covers_all_six_existing_tls_sites(nginx):
    for domain in DEFAULT_SITES[1:]:
        (nginx / "sites-enabled" / domain).write_text((HTTP + TLS).replace(DOMAIN, domain), encoding="utf-8")
    changes = plan_changes(nginx)
    assert len(changes) == 7
    for domain in DEFAULT_SITES:
        updated = changes[nginx / "sites-enabled" / domain].decode("utf-8")
        assert updated.replace("\r\n", "\n").startswith(HTTP.replace(DOMAIN, domain))
        assert updated.count(f"include {INCLUDE};") == 1
