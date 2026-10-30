"""Install the SSO location in the six existing TLS virtual hosts.

Defaults to a read-only plan and nginx validation. Run as root with --apply
and an explicit --backup-dir to save originals, validate, then reload nginx.
"""
from __future__ import annotations

import argparse
from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import stat
import subprocess
import tempfile


MANAGED_HEADER = "# Managed by Kodeks superadmin SSO installer."
INCLUDE = "/etc/nginx/snippets/kodeks-sso.conf"
DEFAULT_SITES = tuple(f"cabinet-{number:02d}.kodeks.expert" for number in range(2, 8))
DEFAULT_SNIPPET = Path(__file__).resolve().parents[1] / "deploy/nginx/kodeks-sso.conf"


@dataclass
class Directive:
    words: list[str]
    children: list["Directive"] | None
    start: int
    close: int


def _tokens(source: str):
    """Lex braces and semicolons without treating comments/quoted text as blocks."""
    position = 0
    while position < len(source):
        if source[position].isspace():
            position += 1
            continue
        if source[position] == "#":
            end = source.find("\n", position)
            position = len(source) if end < 0 else end + 1
            continue
        start = position
        if source[position] in "{};":
            yield source[position], start, position + 1
            position += 1
            continue
        value = []
        quote = None
        while position < len(source):
            character = source[position]
            if character == "\\":
                if position + 1 >= len(source):
                    raise ValueError("Unterminated nginx escape")
                value.append(source[position + 1])
                position += 2
                continue
            if quote:
                if character == quote:
                    quote = None
                else:
                    value.append(character)
            elif character in "\"'":
                quote = character
            elif character.isspace() or character in "{};#":
                break
            else:
                value.append(character)
            position += 1
        if quote:
            raise ValueError("Unterminated nginx string")
        yield "".join(value), start, position


def _parse(source: str) -> list[Directive]:
    tokens = iter(_tokens(source))

    def block(nested=False):
        nodes, words, start = [], [], 0
        for value, begin, end in tokens:
            punctuation = end == begin + 1 and source[begin] in "{};"
            if punctuation and value == "}":
                if not nested or words:
                    raise ValueError("Unexpected nginx closing brace")
                return nodes, begin
            if punctuation and value in {"{", ";"}:
                if not words:
                    raise ValueError("Empty nginx directive")
                children, close = block(True) if value == "{" else (None, begin)
                nodes.append(Directive(words, children, start, close))
                words = []
            else:
                if not words:
                    start = begin
                words.append(value)
        if nested or words:
            raise ValueError("Incomplete nginx directive or block")
        return nodes, len(source)

    return block()[0]


def _tls(server: Directive) -> bool:
    return any(
        node.words[0] == "listen" and len(node.words) > 1
        and re.search(r"(?:^|:)443$", node.words[1])
        for node in server.children or []
    )


def _descendants(nodes):
    for node in nodes:
        yield node
        yield from _descendants(node.children or [])


def add_include(source: str, domain: str) -> str:
    servers = [node for node in _parse(source) if node.words == ["server"]
               and _tls(node) and any(child.words[0] == "server_name" and domain in child.words[1:]
                                     for child in node.children or [])]
    if len(servers) != 1:
        raise ValueError(f"Expected one TLS server for {domain}, found {len(servers)}")
    server = servers[0]
    for node in _descendants(server.children or []):
        if node.words[0] == "location" and any("/sso" in word for word in node.words[1:]):
            raise ValueError(f"Existing SSO location for {domain}; refusing to replace it")
    includes = [node for node in _descendants(server.children or [])
                if node.words[0] == "include" and INCLUDE in node.words[1:]]
    if includes:
        if len(includes) != 1 or includes[0] not in (server.children or []):
            raise ValueError(f"SSO include is duplicated or nested for {domain}")
        return source
    # Insert before the closing brace, leaving every existing byte intact.
    line_start = source.rfind("\n", 0, server.close) + 1
    prefix = source[line_start:server.close]
    if prefix.strip():
        return source[:server.close] + f"\n    include {INCLUDE};\n" + source[server.close:]
    return source[:line_start] + prefix + f"    include {INCLUDE};\n" + source[line_start:]


def _inside(root: Path, path: Path, *, exists=False) -> Path:
    resolved = path.resolve(strict=exists)
    if not resolved.is_relative_to(root):
        raise ValueError(f"Nginx target escapes {root}: {path}")
    return resolved


def plan_changes(nginx_root: Path, snippet: Path = DEFAULT_SNIPPET, sites=DEFAULT_SITES) -> dict[Path, bytes]:
    root = nginx_root.resolve(strict=True)
    target = _inside(root, root / "snippets/kodeks-sso.conf")
    desired = snippet.read_bytes()
    if not desired.decode("utf-8").startswith(MANAGED_HEADER + "\n"):
        raise ValueError("Source snippet is missing its ownership header")
    if target.exists() and target.read_bytes() != desired:
        if not target.read_text(encoding="utf-8").startswith(MANAGED_HEADER + "\n"):
            raise ValueError("Existing SSO snippet is not managed by this installer")
    planned = {}
    if not target.exists() or target.read_bytes() != desired:
        planned[target] = desired
    for domain in sites:
        if not re.fullmatch(r"[a-zA-Z0-9.-]+", domain):
            raise ValueError("Site must be a domain filename, without path components")
        site = _inside(root, root / "sites-enabled" / domain, exists=True)
        original = planned.get(site, site.read_bytes())
        updated = add_include(original.decode("utf-8"), domain).encode("utf-8")
        if updated != original:
            planned[site] = updated
    return planned


def _write(path: Path, content: bytes, metadata=None):
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, name = tempfile.mkstemp(prefix=".kodeks-sso-", dir=path.parent)
    temporary = Path(name)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        os.chmod(temporary, stat.S_IMODE(metadata.st_mode) if metadata else 0o644)
        if metadata and hasattr(os, "chown"):
            os.chown(temporary, metadata.st_uid, metadata.st_gid)
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def install(nginx_root: Path, *, snippet=DEFAULT_SNIPPET, sites=DEFAULT_SITES,
            apply=False, backup_dir: Path | None = None, runner=subprocess.run) -> dict:
    root = nginx_root.resolve(strict=True)
    planned = plan_changes(root, snippet, sites)
    check_command = ["nginx", "-t", "-p", str(root) + "/", "-c", str(root / "nginx.conf")]

    def check():
        runner(check_command, check=True)

    check()
    result = {"applied": False, "changed_files": [str(path) for path in planned]}
    if not apply or not planned:
        return result
    if backup_dir is None or not backup_dir.is_absolute():
        raise ValueError("--apply requires an absolute --backup-dir")
    backups = backup_dir.resolve()
    backups.mkdir(parents=True, exist_ok=True)
    if (backups / "manifest.json").exists():
        raise ValueError("Backup directory already contains an installation manifest")
    originals = {}
    for path in planned:
        metadata = path.stat() if path.exists() else None
        originals[path] = (path.read_bytes(), metadata) if metadata else (None, None)
        if metadata:
            destination = backups / path.relative_to(root)
            if destination.exists():
                raise ValueError(f"Backup file already exists: {destination}")
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(path, destination)
    manifest = [{"path": str(path), "existed": content is not None,
                 "sha256": hashlib.sha256(content).hexdigest() if content is not None else None}
                for path, (content, _) in originals.items()]
    (backups / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    written, reload_attempted = [], False
    try:
        for path, content in planned.items():
            _write(path, content, originals[path][1])
            written.append(path)
        check()
        reload_attempted = True
        runner(["systemctl", "reload", "nginx"], check=True)
    except Exception as error:
        restore_errors = []
        for path in reversed(written):
            content, metadata = originals[path]
            try:
                if content is None:
                    path.unlink(missing_ok=True)
                else:
                    _write(path, content, metadata)
            except OSError as restore_error:
                restore_errors.append((path, restore_error))
        if restore_errors:
            raise RuntimeError(f"Installation failed; some files could not be restored; backups: {backups}") from restore_errors[0][1]
        try:
            check()
            if reload_attempted:
                runner(["systemctl", "reload", "nginx"], check=True)
        except Exception as rollback_error:
            raise RuntimeError(f"Installation failed and rollback validation/reload failed; backups: {backups}") from rollback_error
        raise RuntimeError(f"Installation failed; original files restored; backups: {backups}") from error
    result["applied"] = True
    result["backup_dir"] = str(backups)
    return result


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--backup-dir", type=Path)
    parser.add_argument("--nginx-root", type=Path, default=Path("/etc/nginx"))
    parser.add_argument("--snippet", type=Path, default=DEFAULT_SNIPPET)
    parser.add_argument("--sites", nargs="+", default=DEFAULT_SITES)
    args = parser.parse_args(argv)
    if args.apply and (args.backup_dir is None or not args.backup_dir.is_absolute()):
        parser.error("--apply requires an absolute --backup-dir")
    if args.apply and hasattr(os, "geteuid") and os.geteuid() != 0:
        parser.error("--apply must run as root; this script does not invoke sudo")
    try:
        print(json.dumps(install(args.nginx_root, snippet=args.snippet, sites=args.sites,
                                 apply=args.apply, backup_dir=args.backup_dir), indent=2))
    except (OSError, ValueError, RuntimeError, subprocess.SubprocessError) as error:
        parser.exit(1, f"SSO nginx installation failed: {error}\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
