#!/usr/bin/env python3
"""Локальный обзор и ограниченные проверки polyrepo без внешних Python-зависимостей."""

import argparse
import json
import os
from pathlib import Path
import re
import shlex
import signal
import subprocess
import sys


ALLOWED_COMMANDS = {
    ("make", target)
    for target in ("lint", "lint-check", "test", "build", "agent-harness-test")
} | {
    ("npm", "run", target)
    for target in ("lint", "typecheck", "test", "test:ci", "build", "check")
}


def relative_path(value):
    """Проверяет, что путь из manifest не выходит за каталог владельца."""
    if not isinstance(value, str) or not value or "\\" in value:
        raise ValueError("invalid relative path")
    path = Path(value)
    if path.is_absolute() or ".." in path.parts or value == ".":
        raise ValueError("invalid relative path")
    return value


def load_manifest(infra):
    """Читает единственный проверяемый manifest рядом с Makefile infra."""
    data = json.loads((infra / "agent-services.json").read_text())
    if not isinstance(data, dict) or data.get("version") != 1:
        raise ValueError("unsupported manifest version")
    services = data.get("services")
    if not isinstance(services, dict) or not services or "infra" not in services:
        raise ValueError("services must include infra")
    if data.get("excluded") != ["sandbox"] or "sandbox" in services:
        raise ValueError("sandbox must remain excluded")
    references = data.get("shared_references")
    if not isinstance(references, list):
        raise ValueError("shared_references must be a list")
    for reference in references:
        relative_path(reference)
    for name, service in services.items():
        if not re.fullmatch(r"[a-z][a-z0-9-]*", name) or not isinstance(service, dict):
            raise ValueError("invalid service entry")
        instructions = service.get("instructions")
        checks = service.get("checks")
        if not isinstance(instructions, list) or not instructions or not isinstance(checks, list):
            raise ValueError("instructions and checks must be lists")
        for path in instructions:
            relative_path(path)
        for check in checks:
            if not isinstance(check, dict):
                raise ValueError("invalid check entry")
            argv = check.get("argv")
            if not isinstance(argv, list) or not all(isinstance(arg, str) for arg in argv):
                raise ValueError("check argv must be a list of strings")
            if tuple(argv) not in ALLOWED_COMMANDS:
                raise ValueError("command is not an allowed verification command")
            requires = check.get("requires", [])
            if not isinstance(requires, list):
                raise ValueError("requires must be a list")
            for path in requires:
                relative_path(path)
    return data


def repo_path(infra, name):
    """Находит infra независимо от имени клона, а сервисы — среди его соседей."""
    return infra if name == "infra" else infra.parent / name


def git_summary(repo):
    """Возвращает ветку и счётчик изменений, не выводя имена и содержимое файлов."""
    if not (repo / ".git").exists():
        return "not a git checkout"
    try:
        branch = subprocess.run(
            ["git", "-C", str(repo), "branch", "--show-current"],
            capture_output=True, text=True, timeout=10, check=True,
        ).stdout.strip() or "detached HEAD"
        status = subprocess.run(
            ["git", "-C", str(repo), "status", "--porcelain=v1", "-z", "--untracked-files=normal"],
            capture_output=True, text=True, timeout=10, check=True,
        ).stdout
        # -z добавляет второе имя для переименований; считаем записи, а не пути.
        entries = iter(status.split("\0"))
        changed = 0
        for entry in entries:
            if entry:
                changed += 1
                if "R" in entry[:2] or "C" in entry[:2]:
                    next(entries, None)
        return "{}; {}".format(branch, "dirty ({} changes)".format(changed) if changed else "clean")
    except (OSError, subprocess.SubprocessError):
        return "git status unavailable"


def context(infra, data, names):
    """Печатает минимальный контекст без diff, env, секретов и логов запросов."""
    print("Workspace: {}".format(infra.parent))
    for name in names:
        repo = repo_path(infra, name)
        if not repo.is_dir():
            print("MISSING {}: {}".format(name, repo))
            continue
        service = data["services"][name]
        print("{}: {} [{}]".format(name, repo, git_summary(repo)))
        for instruction in service["instructions"]:
            path = repo / instruction
            print("  read: {}{}".format(path, "" if path.is_file() else " [MISSING]"))
        for check in service["checks"]:
            print("  verify: {}".format(shlex.join(check["argv"])))
    print("Shared references:")
    for reference in data["shared_references"]:
        print("  {}".format(infra.parent / reference))
    print("Excluded: sandbox (external; read-only, never verified by this harness)")
    return 0


def command_exists(repo, argv):
    """Проверяет декларацию make/npm команды, не исполняя Makefile или package scripts."""
    try:
        if argv[0] == "make":
            makefile = (repo / "Makefile").read_text()
            return re.search(r"^{}\s*:(?!=)".format(re.escape(argv[1])), makefile, re.MULTILINE) is not None
        package = json.loads((repo / "package.json").read_text())
        return argv[2] in package.get("scripts", {})
    except (OSError, ValueError, AttributeError):
        return False


def check_workspace(infra, data, names, strict=False, selected=False):
    """Проверяет инструкции и команды; неполный клон явно помечается PARTIAL."""
    errors = 0
    missing = set()
    for name in names:
        repo = repo_path(infra, name)
        if not repo.is_dir():
            print("MISSING {}: {}".format(name, repo))
            missing.add(name)
            if selected:
                errors += 1
            continue
        for instruction in data["services"][name]["instructions"]:
            if not (repo / instruction).is_file():
                print("ERROR missing instruction: {}".format(repo / instruction))
                errors += 1
        for check in data["services"][name]["checks"]:
            if not command_exists(repo, check["argv"]):
                print("ERROR missing command in {}: {}".format(repo, shlex.join(check["argv"])))
                errors += 1
    for reference in data["shared_references"]:
        owner = Path(reference).parts[0]
        if not (infra.parent / owner).is_dir():
            if owner not in missing:
                print("MISSING {}: shared reference {}".format(owner, reference))
            missing.add(owner)
        elif not (infra.parent / reference).is_file():
            print("ERROR missing shared reference: {}".format(reference))
            errors += 1
    if strict:
        errors += len(missing)
    print("{}: {} error(s), {} missing repository/repositories".format(
        "FAILED" if errors else ("PARTIAL" if missing else "OK"), errors, len(missing),
    ))
    return 1 if errors else 0


def run_command(argv, repo, timeout):
    """Исполняет argv без shell и останавливает группу процессов по таймауту."""
    try:
        process = subprocess.Popen(argv, cwd=str(repo), start_new_session=True)
    except OSError:
        print("ERROR executable unavailable: {}".format(argv[0]), flush=True)
        return 127
    try:
        code = process.wait(timeout=timeout)
        return code if code >= 0 else 128 - code
    except (subprocess.TimeoutExpired, KeyboardInterrupt) as error:
        # make запускает дочерние процессы: таймаут должен остановить и их.
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        process.wait()
        code = 124 if isinstance(error, subprocess.TimeoutExpired) else 130
        print("ERROR {}: {}".format("timeout" if code == 124 else "interrupted", shlex.join(argv)), flush=True)
        return code


def verify(infra, data, name, dry_run, timeout):
    """После проверки файлов выполняет только команды выбранного сервиса."""
    if check_workspace(infra, data, [name], selected=True):
        return 1
    repo = repo_path(infra, name)
    checks = data["services"][name]["checks"]
    for check in checks:
        print("cwd={} $ {}".format(shlex.quote(str(repo)), shlex.join(check["argv"])), flush=True)
    if dry_run:
        print("DRY RUN: commands were not executed; tool prerequisites were not checked")
        return 0
    # Проверяем все инструменты заранее, чтобы make lint не устанавливал их сам.
    for check in checks:
        for requirement in check.get("requires", []):
            if not (repo / requirement).exists():
                print("ERROR missing prerequisite: {}; install explicitly before verification".format(repo / requirement))
                return 1
    for check in checks:
        print("RUN cwd={} $ {}".format(shlex.quote(str(repo)), shlex.join(check["argv"])), flush=True)
        code = run_command(check["argv"], repo, timeout)
        if code:
            print("FAILED (exit {}): {}".format(code, shlex.join(check["argv"])), flush=True)
            return code
    print("VERIFIED {} ({} commands)".format(name, len(checks)))
    return 0


def main():
    """Разбирает CLI; поиск workspace всегда начинается с расположения скрипта."""
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)
    for command in ("context", "check", "verify"):
        subparser = subparsers.add_parser(command)
        subparser.add_argument("--service", required=command == "verify")
        if command == "check":
            subparser.add_argument("--strict-workspace", action="store_true")
        if command == "verify":
            subparser.add_argument("--dry-run", action="store_true")
            subparser.add_argument("--timeout", type=int, default=300, help="per-command seconds (1..3600)")
    args = parser.parse_args()
    infra = Path(__file__).resolve().parents[1]
    try:
        data = load_manifest(infra)
    except (OSError, ValueError, TypeError) as error:
        parser.error("invalid manifest: {}".format(error))
    if args.service and args.service not in data["services"]:
        parser.error("unknown service: {}; choose {}".format(args.service, ", ".join(data["services"])))
    names = [args.service] if args.service else list(data["services"])
    if args.command == "context":
        return context(infra, data, names)
    if args.command == "check":
        return check_workspace(infra, data, names, args.strict_workspace, bool(args.service))
    if not 1 <= args.timeout <= 3600:
        parser.error("--timeout must be between 1 and 3600 seconds")
    return verify(infra, data, args.service, args.dry_run, args.timeout)


if __name__ == "__main__":
    sys.exit(main())
