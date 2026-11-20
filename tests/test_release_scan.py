import json
import os
import secrets
import sys
import tempfile
import unittest
from datetime import date
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

import release_scan as scanner
from release_manifest import DOCKERFILES, PLATFORM, REPOS, UPSTREAM
from release_scan import ScanCommandError, image_scan, run, source_scan
from release_scan_policy import read_exceptions, release_images, report_findings
from release_scan_results import go_findings, image_findings, npm_findings, source_secrets
from release_sources import pinned_image


SHA = "1" * 40
DIGEST = "sha256:" + "a" * 64
REGISTRY = "ghcr.io/overmindv"
TODAY = date(2030, 1, 2)


def release_fixture():
    pins = dict.fromkeys(UPSTREAM.values(), DIGEST)
    return {
        "plan": {
            "version": 1, "release": "fixture", "registry": REGISTRY, "platform": PLATFORM,
            "sources": dict.fromkeys(REPOS, SHA), "pins": pins,
            "images": {name: {"source": repo, "dockerfile": path}
                       for name, (repo, path) in DOCKERFILES.items()},
            "upstream": {name: pinned_image(ref, pins) for name, ref in UPSTREAM.items()},
        },
        "images": {name: f"{REGISTRY}/{name}@{DIGEST}" for name in DOCKERFILES},
    }


def container_fixture(severity="HIGH"):
    return {
        "SchemaVersion": 2, "ArtifactType": "container_image",
        "Results": [{"Vulnerabilities": [{
            "VulnerabilityID": "CVE-2030-0001", "PkgName": "example",
            "Severity": severity,
        }]}],
    }


class ReleasePolicyTest(unittest.TestCase):
    def test_requires_complete_canonical_digest_set(self):
        release = release_fixture()
        self.assertEqual(release_images(release).keys(), DOCKERFILES.keys() | UPSTREAM.keys())
        for target in ("users", "sandbox"):
            current = release["images"][target]
            release["images"][target] = f"{REGISTRY}/{target}:latest"
            with self.assertRaisesRegex(ValueError, "expected published image digest"):
                release_images(release)
            release["images"][target] = current
        del release["images"]["gojudge"]
        with self.assertRaisesRegex(ValueError, "every published image"):
            release_images(release)

    def test_expiring_exception_applies_only_to_exact_revision_and_finding(self):
        sources = release_fixture()["plan"]["sources"]
        images = release_images(release_fixture())
        findings = image_findings("sandbox", container_fixture())
        entry = {
            "scanner": "container", "target": "sandbox", "id": "CVE-2030-0001",
            "subject": "example", "revision": DIGEST, "owner": "infra",
            "ticket": "OVM-171", "reason": "fixture decision", "expires": "2030-01-20",
        }
        allowed = read_exceptions([entry], sources.keys(), TODAY)
        report, = report_findings(findings, allowed, sources, images)
        self.assertEqual(report["decision"], "excepted")
        images["sandbox"] = f"{REGISTRY}/sandbox@sha256:{'b' * 64}"
        report, = report_findings(findings, allowed, sources, images)
        self.assertEqual(report["decision"], "blocked")
        for updates in (
            {"expires": "2030-01-02"}, {"expires": "2031-01-02"},
            {"id": "*"}, {"revision": "latest"}, {"ticket": "not-jira"},
        ):
            with self.subTest(updates=updates), self.assertRaises(ValueError):
                read_exceptions([{**entry, **updates}], sources.keys(), TODAY)

    def test_infra_command_failure_does_not_echo_stderr(self):
        with tempfile.TemporaryDirectory() as work:
            with self.assertRaisesRegex(ScanCommandError, r"exited with status 3$") as raised:
                run([sys.executable, "-c",
                     "import sys; print('fixture output', file=sys.stderr); sys.exit(3)"], work)
            self.assertNotIn("fixture output", str(raised.exception))


class ResultsTest(unittest.TestCase):
    def test_go_reachable_wins_over_package_only_finding(self):
        module = {"osv": "GO-2030-0010", "trace": [{"module": "example.invalid/lib"}]}
        source = "\n".join(json.dumps(item, indent=2) for item in (
            {"config": {"scan_level": "symbol"}},
            {"finding": module},
            {"finding": {**module, "trace": [{**module["trace"][0], "function": "Work"}]}},
            {"finding": {**module, "osv": "GO-2030-0020"}},
        ))
        report = report_findings(go_findings("users", source), {}, {"users": SHA}, {})
        self.assertEqual([row["decision"] for row in report], ["blocked", "reported"])
        with self.assertRaisesRegex(ValueError, "missing symbol scan"):
            go_findings("users", json.dumps({"config": {"scan_level": "module"}}))

    def test_npm_high_and_report_error_are_not_clean(self):
        report = {"auditReportVersion": 2, "vulnerabilities": {
            "fixture": {"via": [{"url": "https://example.invalid/GHSA-id", "severity": "high"}]},
        }}
        finding, = npm_findings("frontend", report, 1)
        self.assertEqual((finding.id, finding.severity, finding.blocking), ("GHSA-id", "HIGH", True))
        for malformed in ({"error": {"code": "E503"}}, {"auditReportVersion": 2, "vulnerabilities": {}}):
            with self.subTest(report=malformed), self.assertRaises(ValueError):
                npm_findings("frontend", malformed, 1)

    def test_secret_report_never_copies_raw_value(self):
        finding, = source_secrets("infra", [{
            "RuleID": "example", "File": "notes.txt", "StartLine": 3,
            "Secret": "fixture raw match", "Match": "fixture raw match",
        }], 42)
        self.assertEqual(finding.subject, "notes.txt:3")
        self.assertNotIn("fixture raw match", repr(finding))
        with self.assertRaises(ValueError):
            source_secrets("infra", [], 42)

    def test_image_json_findings_keep_highest_severity(self):
        findings = image_findings("sandbox", container_fixture("MEDIUM"))
        findings += image_findings("sandbox", container_fixture("CRITICAL"))
        result, = report_findings(findings, {}, {}, {"sandbox": f"repo@{DIGEST}"})
        self.assertEqual((result["severity"], result["decision"]), ("CRITICAL", "blocked"))
        with self.assertRaises(ValueError):
            image_findings("sandbox", {})


class ScanReportTest(unittest.TestCase):
    def test_gates_the_whole_candidate_and_keeps_failed_scan_evidence(self):
        manifest = release_fixture()
        images = release_images(manifest)
        failing_finding = image_findings("sandbox", container_fixture())

        def image_result(target, *_):
            if target == "sandbox":
                return failing_finding
            if target == "gojudge":
                raise ScanCommandError("trivy exited with status 1")
            return []

        with (
            tempfile.TemporaryDirectory() as work,
            patch.object(scanner, "make_plan", return_value=manifest["plan"]),
            patch.object(scanner, "archive"),
            patch.object(scanner, "source_scan", return_value=[]),
            patch.object(scanner, "go_scan", return_value=[]),
            patch.object(scanner, "npm_scan", return_value=[]),
            patch.object(scanner, "image_scan") as image,
            patch("builtins.print"),
        ):
            root = Path(work)
            image.return_value = []
            clean = root / "clean"
            self.assertEqual(scanner.scan_release(manifest, root, images, clean), 0)
            report = json.loads((clean / "report.json").read_text())
            self.assertEqual(report["status"], "passed")
            self.assertEqual(len(report["scans"]), 2 * len(REPOS) - 1 + len(images))
            self.assertEqual({scan["target"] for scan in report["scans"] if scan["scanner"] == "image"},
                             images.keys())
            image.side_effect = lambda target, *_: failing_finding if target == "sandbox" else []
            self.assertEqual(scanner.scan_release(manifest, root, images, root / "blocked"), 1)
            image.side_effect = image_result
            failed = root / "failed"
            self.assertEqual(scanner.scan_release(manifest, root, images, failed), 2)
            report = json.loads((failed / "report.json").read_text())
            self.assertEqual(report["findings"][0]["decision"], "blocked")
            self.assertEqual([record["error"] for record in report["scans"] if record["status"] == "error"],
                             ["trivy exited with status 1"])


class LiveScannerTest(unittest.TestCase):
    @unittest.skipUnless(os.getenv("RELEASE_TEST_GITLEAKS"), "needs installed Gitleaks")
    def test_gitleaks_clean_and_generated_noncredential(self):
        with tempfile.TemporaryDirectory() as work:
            scratch = Path(work)
            source = scratch / "source"
            source.mkdir()
            self.assertEqual(source_scan("clean", source, scratch), [])
            fake_value = secrets.token_hex(20)
            (source / "fixture.txt").write_text(f'example_api_key = "{fake_value}"\n', encoding="utf-8")
            findings = source_scan("fixture", source, scratch)
            self.assertTrue(any(item.blocking and item.subject.endswith(":1") for item in findings))
            self.assertNotIn(fake_value, repr(findings))

    @unittest.skipUnless(os.getenv("RELEASE_TEST_TRIVY_IMAGE"), "needs Trivy and pinned fixture image")
    def test_trivy_vulnerable_image_and_sbom(self):
        with tempfile.TemporaryDirectory() as work:
            scratch = Path(work)
            output = scratch / "out"
            output.mkdir()
            findings = image_scan("fixture", os.environ["RELEASE_TEST_TRIVY_IMAGE"],
                                  PLATFORM, scratch, output)
            self.assertTrue(any(item.scanner == "container" and item.blocking for item in findings))
            bom = json.loads((output / "fixture.cdx.json").read_text())
            self.assertEqual(bom["bomFormat"], "CycloneDX")
            self.assertTrue(bom["components"])


if __name__ == "__main__":
    unittest.main()
