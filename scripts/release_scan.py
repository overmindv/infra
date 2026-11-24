#!/usr/bin/env python3
import argparse
import base64
import json
import os
import subprocess
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path

from release_manifest import fingerprint, read_json, verify_source_plan, write_json
from release_scan_policy import read_exceptions, release_images, report_findings
from release_scan_results import go_findings, image_findings, npm_findings, source_secrets
from release_sources import archive, remote_source


ROOT = Path(__file__).resolve().parents[1]
EXCEPTIONS = ROOT / "security" / "release-exceptions.json"
TOOL_TIMEOUT = 600
GITLEAKS_FINDINGS = 42


class ScanCommandError(ValueError):
    pass


def run(argv, cwd, env=None, accepted=(0,)):
    result = subprocess.run(
        argv, cwd=cwd, env=env, capture_output=True, encoding="utf-8",
        errors="replace", timeout=TOOL_TIMEOUT, check=False,
    )
    if result.returncode not in accepted:
        raise ScanCommandError(f"{argv[0]} exited with status {result.returncode}")
    return result


def fetch_sources(plan, workspace):
    workspace.mkdir()
    env = {**os.environ, "GIT_TERMINAL_PROMPT": "0"}
    token = env.pop("RELEASE_SOURCE_TOKEN", "")
    if token:
        basic = base64.b64encode(f"x-access-token:{token}".encode()).decode()
        env.update({
            "GIT_CONFIG_COUNT": "1",
            "GIT_CONFIG_KEY_0": "http.https://github.com/.extraheader",
            "GIT_CONFIG_VALUE_0": f"Authorization: Basic {basic}",
        })
    for name, sha in sorted(plan["sources"].items()):
        print(f"Fetch {name} at {sha}", flush=True)
        repo = workspace / name
        repo.mkdir()
        run(["git", "init", "--quiet"], repo)
        run(["git", "remote", "add", "origin", remote_source(name)], repo)
        run(["git", "fetch", "--quiet", "--no-tags", "--depth=1", "origin",
             f"{sha}:refs/remotes/origin/release-scan"], repo, env)


def go_scan(target, source):
    if not all((source / name).is_file() for name in ("go.mod", "go.sum")):
        raise ValueError("missing go.mod/go.sum")
    env = {**os.environ, "GOWORK": "off", "GOFLAGS": "-mod=readonly",
           "GOTOOLCHAIN": "local", "GOOS": "linux", "GOARCH": "amd64", "CGO_ENABLED": "0"}
    output = run(["govulncheck", "-json", "./..."], source, env).stdout
    return go_findings(target, output)


def npm_scan(target, source):
    if not (source / "package-lock.json").is_file():
        raise ValueError("missing package-lock.json")
    result = run([
        "npm", "audit", "--json", "--ignore-scripts", "--package-lock-only",
        "--include=dev", "--include=optional", "--include=peer",
        "--registry", "https://registry.npmjs.org",
    ], source, accepted=(0, 1))
    return npm_findings(target, json.loads(result.stdout), result.returncode)


def source_scan(target, source, scratch):
    config = scratch / "gitleaks.toml"
    config.write_text("[extend]\nuseDefault = true\n", encoding="utf-8")
    ignore = scratch / "empty-ignore"
    ignore.touch()
    report = scratch / f"{target}-gitleaks.json"
    result = run([
        "gitleaks", "dir", ".", "--no-banner", "--redact=100",
        "--config", str(config), "--gitleaks-ignore-path", str(ignore),
        "--ignore-gitleaks-allow", "--report-path", str(report), "--report-format", "json",
        "--exit-code", str(GITLEAKS_FINDINGS),
    ], source, accepted=(0, GITLEAKS_FINDINGS))
    return source_secrets(target, read_json(report) or [], result.returncode)


def image_scan(target, ref, platform, scratch, output):
    env = {key: value for key, value in os.environ.items() if not key.startswith("TRIVY_")}
    report_path = scratch / f"{target}-trivy.json"
    run([
        "trivy", "image", "--quiet", "--image-src", "remote", "--platform", platform,
        "--scanners", "vuln,secret", "--list-all-pkgs", "--format", "json",
        "--output", str(report_path), ref,
    ], scratch, env)
    report = read_json(report_path)
    if report.get("ArtifactName") != ref:
        raise ValueError("trivy: unexpected image reference")
    findings = image_findings(target, report)
    for result in report.get("Results") or []:
        result.pop("Secrets", None)
    report.get("Metadata", {}).pop("ImageConfig", None)
    sanitized = scratch / f"{target}-packages.json"
    write_json(sanitized, report)
    sbom = scratch / f"{target}.cdx.json"
    run(["trivy", "convert", "--quiet", "--format", "cyclonedx",
         "--output", str(sbom), str(sanitized)], scratch, env)
    bom = read_json(sbom)
    if bom.get("bomFormat") != "CycloneDX" or not bom.get("specVersion"):
        raise ValueError("trivy: invalid CycloneDX SBOM")
    write_json(output / sbom.name, bom)
    return findings


def timed_scan(scanner, target, scans, findings, func, *args):
    print(f"Scan {scanner}: {target}", flush=True)
    started = time.monotonic()
    record = {"scanner": scanner, "target": target}
    try:
        findings.extend(func(target, *args))
        record["status"] = "scanned"
    except (ValueError, OSError, KeyError, TypeError, AttributeError, IndexError,
            subprocess.TimeoutExpired) as error:
        record["status"] = "error"
        record["error"] = str(error) if isinstance(error, ScanCommandError) else type(error).__name__
        print(f'Scan failed: {scanner}/{target} ({record["error"]})', flush=True)
    record["seconds"] = round(time.monotonic() - started, 3)
    scans.append(record)


def scan_release(manifest, workspace, images, output, exceptions_path=EXCEPTIONS):
    plan = manifest["plan"]
    verify_source_plan(workspace, plan)
    timestamp = datetime.now(timezone.utc)
    entries = read_json(exceptions_path)
    exceptions = read_exceptions(entries, set(plan["sources"]), timestamp.date())
    output.mkdir()
    scans, findings = [], []
    with tempfile.TemporaryDirectory(prefix="overmindv-scan-") as work:
        scratch = Path(work)
        for name, sha in sorted(plan["sources"].items()):
            with tempfile.TemporaryDirectory(dir=scratch) as payload:
                source = Path(payload)
                archive(workspace / name, sha, source)
                timed_scan("source-secret", name, scans, findings, source_scan, source, scratch)
                if name == "frontend":
                    timed_scan("npm", name, scans, findings, npm_scan, source)
                elif name != "infra":
                    timed_scan("go", name, scans, findings, go_scan, source)
        for name, ref in sorted(images.items()):
            timed_scan("image", name, scans, findings, image_scan,
                       ref, plan["platform"], scratch, output)
    results = report_findings(findings, exceptions, plan["sources"], images)
    scan_error = any(scan["status"] != "scanned" for scan in scans)
    blocked = any(item["decision"] == "blocked" for item in results)
    status = "error" if scan_error else "blocked" if blocked else "passed"
    write_json(output / "report.json", {
        "version": 1, "status": status, "started_at": timestamp.isoformat(),
        "manifest_sha256": fingerprint(manifest), "sources": plan["sources"],
        "images": images, "platform": plan["platform"], "scans": scans, "findings": results,
        "exceptions_sha256": fingerprint(entries),
    })
    print(f"Release scan: {status}", flush=True)
    return {"passed": 0, "blocked": 1, "error": 2}[status]


def main():
    parser = argparse.ArgumentParser(description="Scan a published release by SHA and digest")
    commands = parser.add_subparsers(dest="command", required=True)
    for name in ("fetch-sources", "scan"):
        command = commands.add_parser(name)
        command.add_argument("--manifest", type=Path, required=True)
        command.add_argument("--workspace", type=Path, required=True)
        command.add_argument("--registry", help="Require this registry namespace in the manifest")
    commands.choices["scan"].add_argument("--output", type=Path, required=True)
    commands.choices["scan"].add_argument("--exceptions", type=Path, default=EXCEPTIONS)
    args = parser.parse_args()
    try:
        manifest = read_json(args.manifest)
        images = release_images(manifest)
        if args.registry is not None and args.registry != manifest["plan"]["registry"]:
            raise ValueError("release registry is not allowed")
        if args.command == "fetch-sources":
            fetch_sources(manifest["plan"], args.workspace.resolve())
            return 0
        return scan_release(manifest, args.workspace.resolve(), images, args.output.resolve(),
                            args.exceptions)
    except (ValueError, OSError, KeyError, TypeError, AttributeError, IndexError,
            subprocess.CalledProcessError, subprocess.TimeoutExpired) as error:
        parser.exit(2, f"release scan: {error}\n")


if __name__ == "__main__":
    raise SystemExit(main())
