"""Поведенческие проверки harness на изолированных временных workspace."""

import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest


SOURCE = Path(__file__).resolve().parents[1] / "scripts" / "agent_harness.py"


class HarnessTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.infra = self.root / "infra"
        self.script = self.infra / "scripts" / "agent_harness.py"
        self.script.parent.mkdir(parents=True)
        if SOURCE.exists():
            shutil.copyfile(SOURCE, self.script)
        self.manifest = {
            "version": 1,
            "shared_references": ["docs/harness/README.md"],
            "excluded": ["sandbox"],
            "services": {
                "infra": {"instructions": ["AGENTS.md"], "checks": []},
                "users": {
                    "instructions": ["AGENTS.md"],
                    "checks": [{"argv": ["make", "test"]}],
                },
            },
        }
        self.write_manifest()
        (self.infra / "AGENTS.md").write_text("# Infra\n")

    def write_manifest(self):
        (self.infra / "agent-services.json").write_text(json.dumps(self.manifest))

    def add_users(self):
        users = self.root / "users"
        users.mkdir()
        (users / "AGENTS.md").write_text("# Users\n")
        (users / "Makefile").write_text("test:\n\t@true\n")
        return users

    def run_cli(self, *args, env=None):
        return subprocess.run(
            [sys.executable, str(self.script), *args],
            cwd="/", env=env, text=True, capture_output=True, timeout=15,
        )

    def fake_make(self, body):
        tools = self.root / "tools"
        tools.mkdir()
        command = tools / "make"
        command.write_text("#!/bin/sh\n" + body)
        command.chmod(0o755)
        env = os.environ.copy()
        env["PATH"] = str(tools) + os.pathsep + env.get("PATH", "")
        return env

    def test_unknown_service_is_actionable_error_without_traceback(self):
        result = self.run_cli("context", "--service", "unknown")
        self.assertEqual(result.returncode, 2)
        self.assertIn("unknown service", result.stderr.lower())
        self.assertNotIn("Traceback", result.stderr)

    def test_partial_clone_reports_missing_siblings_and_strict_mode_fails(self):
        result = self.run_cli("check")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("MISSING users", result.stdout)
        self.assertIn("PARTIAL", result.stdout)
        strict = self.run_cli("check", "--strict-workspace")
        self.assertNotEqual(strict.returncode, 0)
        self.assertNotIn("Traceback", strict.stderr)

    def test_renamed_standalone_infra_clone_is_still_discovered(self):
        relocated = self.root / "deployment"
        self.infra.rename(relocated)
        self.script = relocated / "scripts" / "agent_harness.py"
        result = self.run_cli("check", "--service", "infra")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertNotIn("MISSING infra", result.stdout)

    def test_malformed_manifest_is_reported_without_traceback(self):
        (self.infra / "agent-services.json").write_text("{bad")
        result = self.run_cli("check")
        self.assertEqual(result.returncode, 2)
        self.assertIn("invalid manifest", result.stderr)
        self.assertNotIn("Traceback", result.stderr)

    def test_selected_missing_service_is_error_even_without_strict_flag(self):
        result = self.run_cli("check", "--service", "users")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("MISSING users", result.stdout)

    def test_discovery_uses_script_location_and_context_hides_file_contents(self):
        users = self.add_users()
        subprocess.run(["git", "init", "-q", str(users)], check=True)
        (users / ".env").write_text("SECRET=harness-test-secret\n")
        result = self.run_cli("context", "--service", "users")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn(str(users), result.stdout)
        self.assertIn("dirty", result.stdout)
        self.assertIn("make test", result.stdout)
        self.assertNotIn("harness-test-secret", result.stdout)
        self.assertNotIn(".env", result.stdout)

    def test_dry_run_prints_cwd_and_command_without_executing(self):
        users = self.add_users()
        marker = self.root / "executed"
        env = self.fake_make('touch "$HARNESS_MARKER"\nexit 0\n')
        env["HARNESS_MARKER"] = str(marker)
        result = self.run_cli("verify", "--service", "users", "--dry-run", env=env)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn(str(users), result.stdout)
        self.assertIn("make test", result.stdout)
        self.assertFalse(marker.exists())

    def test_shipped_docs_plan_includes_checks_tests_and_lint(self):
        shipped = json.loads((SOURCE.parents[1] / "agent-services.json").read_text())
        docs_config = shipped["services"]["docs"]
        self.manifest["services"]["docs"] = docs_config
        self.write_manifest()
        docs = self.root / "docs"
        (docs / "harness").mkdir(parents=True)
        (docs / "harness" / "README.md").write_text("# Harness\n")
        for instruction in docs_config["instructions"]:
            (docs / instruction).write_text("# Docs\n")
        (docs / "package.json").write_text(json.dumps({
            "scripts": {"check": "true", "test": "true", "lint": "true"},
        }))
        result = self.run_cli("verify", "--service", "docs", "--dry-run")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        for command in ("npm run check", "npm run test", "npm run lint"):
            self.assertIn("$ " + command, result.stdout)

    def test_failing_command_exit_is_propagated_and_later_commands_do_not_run(self):
        users = self.add_users()
        (users / "Makefile").write_text("test:\n\t@true\nbuild:\n\t@true\n")
        self.manifest["services"]["users"]["checks"].append({"argv": ["make", "build"]})
        self.write_manifest()
        marker = self.root / "executed"
        env = self.fake_make('printf "%s\\n" "$1" >> "$HARNESS_MARKER"\nexit 7\n')
        env["HARNESS_MARKER"] = str(marker)
        result = self.run_cli("verify", "--service", "users", env=env)
        self.assertEqual(result.returncode, 7, result.stdout + result.stderr)
        self.assertEqual(marker.read_text(), "test\n")
        self.assertNotIn("VERIFIED", result.stdout)

    def test_broken_instruction_stops_verify_before_execution(self):
        users = self.add_users()
        (users / "AGENTS.md").unlink()
        marker = self.root / "executed"
        env = self.fake_make('touch "$HARNESS_MARKER"\n')
        env["HARNESS_MARKER"] = str(marker)
        result = self.run_cli("verify", "--service", "users", env=env)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("AGENTS.md", result.stdout)
        self.assertFalse(marker.exists())

    def test_broken_shared_reference_in_present_repo_fails_check(self):
        (self.root / "docs").mkdir()
        result = self.run_cli("check")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("docs/harness/README.md", result.stdout)

    def test_absent_linter_is_reported_without_implicit_install(self):
        users = self.add_users()
        (users / "Makefile").write_text("lint:\n\t@true\n")
        self.manifest["services"]["users"]["checks"] = [
            {"argv": ["make", "lint"], "requires": ["bin/golangci-lint"]},
        ]
        self.write_manifest()
        marker = self.root / "executed"
        env = self.fake_make('touch "$HARNESS_MARKER"\n')
        env["HARNESS_MARKER"] = str(marker)
        result = self.run_cli("verify", "--service", "users", env=env)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("bin/golangci-lint", result.stdout)
        self.assertFalse(marker.exists())

    def test_manifest_rejects_traversal_and_destructive_command(self):
        for update in ({"instructions": ["../../secret"]}, {"checks": [{"argv": ["make", "clean"]}]}):
            with self.subTest(update=update):
                self.manifest["services"]["users"].update(update)
                self.write_manifest()
                result = self.run_cli("check")
                self.assertEqual(result.returncode, 2)
                self.assertIn("manifest", result.stderr.lower())
                self.assertNotIn("Traceback", result.stderr)
                self.manifest["services"]["users"] = {"instructions": ["AGENTS.md"], "checks": []}

    def test_missing_declared_target_fails_check(self):
        users = self.add_users()
        (users / "Makefile").write_text("build:\n\t@true\n")
        result = self.run_cli("check", "--service", "users")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("make test", result.stdout)

    def test_timeout_returns_124(self):
        self.add_users()
        env = self.fake_make("sleep 20\n")
        result = self.run_cli("verify", "--service", "users", "--timeout", "1", env=env)
        self.assertEqual(result.returncode, 124, result.stdout + result.stderr)


if __name__ == "__main__":
    unittest.main()
