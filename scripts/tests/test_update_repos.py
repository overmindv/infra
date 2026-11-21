import os
import json
from pathlib import Path
import subprocess
import tempfile
import unittest


SCRIPT = Path(__file__).resolve().parents[1] / "update-repos.sh"


class UpdateReposTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name) / "workspace"
        self.root.mkdir()
        self.bin = Path(self.temp.name) / "bin"
        self.bin.mkdir()
        self.codegraph_log = Path(self.temp.name) / "codegraph.log"
        codegraph = self.bin / "codegraph"
        codegraph.write_text(
            "#!/bin/sh\nprintf '%s\\n' \"$*\" >> \"$CODEGRAPH_LOG\"\n"
        )
        codegraph.chmod(0o755)

    def tearDown(self):
        self.temp.cleanup()

    def git(self, directory, *args):
        return subprocess.run(
            ["git", "-C", str(directory), *args],
            check=True,
            capture_output=True,
            text=True,
        )

    def create_repo(self, name):
        bare = Path(self.temp.name) / f"{name}.git"
        seed = Path(self.temp.name) / f"{name}-seed"
        local = self.root / name
        subprocess.run(["git", "init", "--bare", str(bare)], check=True, capture_output=True)
        subprocess.run(["git", "init", "-b", "main", str(seed)], check=True, capture_output=True)
        self.git(seed, "config", "user.name", "Test")
        self.git(seed, "config", "user.email", "test@example.com")
        (seed / "state.txt").write_text("initial\n")
        self.git(seed, "add", "state.txt")
        self.git(seed, "commit", "-m", "initial")
        self.git(seed, "remote", "add", "origin", str(bare))
        self.git(seed, "push", "-u", "origin", "main")
        self.git(bare, "symbolic-ref", "HEAD", "refs/heads/main")
        subprocess.run(["git", "clone", str(bare), str(local)], check=True, capture_output=True)
        self.git(local, "config", "user.name", "Test")
        self.git(local, "config", "user.email", "test@example.com")

        return local, seed

    def advance_remote(self, seed, value="remote"):
        (seed / "state.txt").write_text(value + "\n")
        self.git(seed, "add", "state.txt")
        self.git(seed, "commit", "-m", value)
        self.git(seed, "push", "origin", "main")

    def run_update(self, *args):
        env = os.environ.copy()
        env["PATH"] = str(self.bin) + os.pathsep + env["PATH"]
        env["CODEGRAPH_LOG"] = str(self.codegraph_log)
        return subprocess.run(
            [str(SCRIPT), "--root", str(self.root), *args],
            capture_output=True,
            text=True,
            env=env,
        )

    def test_dirty_repository_stops_before_any_update(self):
        clean, clean_seed = self.create_repo("clean")
        dirty, _ = self.create_repo("dirty")
        self.advance_remote(clean_seed)
        (dirty / "untracked.txt").write_text("local\n")

        result = self.run_update()

        self.assertNotEqual(result.returncode, 0)
        self.assertIn("dirty", result.stdout)
        self.assertEqual((clean / "state.txt").read_text(), "initial\n")
        self.assertFalse(self.codegraph_log.exists())

    def test_fast_forward_then_sync_existing_codegraph(self):
        local, seed = self.create_repo("service")
        self.advance_remote(seed)
        (self.root / ".codegraph").mkdir()

        result = self.run_update()

        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual((local / "state.txt").read_text(), "remote\n")
        self.assertEqual(self.codegraph_log.read_text(), f"sync {self.root}\n")
        self.assertNotIn("Updating ", result.stdout)
        self.assertNotIn("Fast-forward", result.stdout)

    def test_initializes_missing_codegraph(self):
        self.create_repo("service")

        result = self.run_update()

        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(self.codegraph_log.read_text(), f"init {self.root}\n")

    def test_diverged_main_fails_without_codegraph(self):
        local, seed = self.create_repo("service")
        (local / "local.txt").write_text("local\n")
        self.git(local, "add", "local.txt")
        self.git(local, "commit", "-m", "local")
        self.advance_remote(seed)

        result = self.run_update()

        self.assertNotEqual(result.returncode, 0)
        self.assertIn("fast-forward", result.stdout)
        self.assertTrue((local / "local.txt").exists())
        self.assertFalse(self.codegraph_log.exists())

    def test_dry_run_changes_nothing(self):
        local, seed = self.create_repo("service")
        self.advance_remote(seed)

        result = self.run_update("--dry-run")

        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual((local / "state.txt").read_text(), "initial\n")
        self.assertFalse(self.codegraph_log.exists())

    def test_workspace_manifest_limits_repository_scope(self):
        infra, _ = self.create_repo("infra")
        self.create_repo("service")
        self.create_repo("sandbox")
        (infra / "agent-services.json").write_text(
            json.dumps({"services": {"infra": {}, "service": {}}, "excluded": ["sandbox"]})
        )
        self.git(infra, "add", "agent-services.json")
        self.git(infra, "commit", "-m", "manifest")

        result = self.run_update("--dry-run")

        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("infra:", result.stdout)
        self.assertIn("service:", result.stdout)
        self.assertNotIn("sandbox:", result.stdout)


if __name__ == "__main__":
    unittest.main()
