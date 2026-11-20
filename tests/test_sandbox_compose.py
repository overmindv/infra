import json
import os
from pathlib import Path
import subprocess
import unittest


ROOT = Path(__file__).resolve().parents[1]
RUNTIME_SERVICES = {"gojudge", "gojudge-compiler"}


def compose_config(*profiles):
    command = [
        "docker", "compose", "--env-file", ".env.example",
        "-f", "docker-compose.yml", "--project-name", "sandbox-compose-test",
    ]
    for profile in profiles:
        command.extend(("--profile", profile))
    system_env = {
        "PATH", "HOME", "USERPROFILE", "SYSTEMROOT", "TEMP", "TMP",
        "SYSTEMDRIVE", "HOMEDRIVE", "HOMEPATH", "APPDATA", "LOCALAPPDATA",
        "PROGRAMDATA", "PROGRAMFILES", "PROGRAMFILES(X86)", "PROGRAMW6432",
    }
    env = {
        key: value for key, value in os.environ.items()
        if key.upper() in system_env or key.upper().startswith("DOCKER_")
    }
    result = subprocess.run(
        [*command, "config", "--format", "json"], cwd=ROOT, env=env,
        capture_output=True, text=True, check=True, timeout=20,
    )
    return json.loads(result.stdout)


class SandboxComposeTests(unittest.TestCase):
    def test_default_and_fake_profiles_do_not_start_gojudge(self):
        default = compose_config()["services"]
        self.assertFalse(RUNTIME_SERVICES.intersection(default))
        self.assertNotIn("sandbox", default)

        fake = compose_config("sandbox")["services"]
        self.assertFalse(RUNTIME_SERVICES.intersection(fake))
        self.assertEqual(fake["sandbox"]["environment"]["SANDBOX_EXECUTOR_TYPE"], "fake")

    def test_gojudge_profile_uses_one_image_on_an_internal_network(self):
        config = compose_config("sandbox", "gojudge")
        services = config["services"]
        runner = services["gojudge"]
        compiler = services["gojudge-compiler"]
        worker = services["sandbox"]
        self.assertEqual(compiler["image"], runner["image"])
        self.assertIn("build", runner)
        self.assertFalse(runner["build"].get("args"))
        self.assertNotIn("build", compiler)
        self.assertEqual(compiler["pull_policy"], "never")
        self.assertTrue(config["networks"]["sandbox-runtime"]["internal"])
        attached = {
            name for name, service in services.items()
            if "sandbox-runtime" in (service.get("networks") or {})
        }
        self.assertEqual(attached, RUNTIME_SERVICES | {"sandbox"})
        self.assertIn("default", worker["networks"])

        for name in RUNTIME_SERVICES:
            service = services[name]
            self.assertEqual(set(service["networks"]), {"sandbox-runtime"})
            self.assertFalse(service.get("ports"))
            self.assertFalse(service.get("environment"))
            self.assertFalse(service.get("volumes"))
            self.assertTrue(service["read_only"])
            self.assertIn("-no-fallback", service["command"])
            ttl = service["command"].index("-file-timeout")
            self.assertEqual(service["command"][ttl + 1], "2m")
            dependency = worker["depends_on"][name]
            self.assertEqual(dependency["condition"], "service_healthy")
            self.assertFalse(dependency["required"])

        env = worker["environment"]
        self.assertEqual(env["SANDBOX_GOJUDGE_BASE_URL"], "http://gojudge:5050")
        self.assertEqual(env["SANDBOX_GOJUDGE_COMPILER_URL"], "http://gojudge-compiler:5050")
        self.assertNotIn("-output-limit", runner["command"])
        output = compiler["command"].index("-output-limit")
        self.assertEqual(compiler["command"][output + 1], "33554432")
        self.assertTrue(compiler.get("mem_limit"))
        self.assertEqual(compiler["depends_on"]["gojudge"]["condition"], "service_started")


if __name__ == "__main__":
    unittest.main()
