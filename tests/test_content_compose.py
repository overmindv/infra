import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]
TOKEN = "CONTENT_GRPC_SERVICE_TOKEN"
MODERATION_TOKEN = "CONTENT_GRPC_MODERATION_TOKEN"
CATALOG_URL = "CONTENT_ENTITIES_URL"


class ContentComposeTest(unittest.TestCase):
    def test_existing_env_gets_a_token_and_grpc_binds_to_loopback(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "scripts").mkdir()
            script = Path(shutil.copyfile(ROOT / "scripts/prepare-env.sh", root / "scripts/prepare-env.sh"))
            template = (ROOT / ".env.example").read_text(encoding="utf-8")
            self.assertIn(f"{TOKEN}=__GENERATE__", template)
            env_file = root / "existing.env"
            env_file.write_text(
                "\n".join(line for line in template.splitlines() if not line.startswith(f"{TOKEN}=")) + "\n",
                encoding="utf-8",
            )

            def prepare():
                subprocess.run(
                    ["sh", str(script), str(env_file)], cwd=root,
                    check=True, capture_output=True, timeout=20,
                )
                entries = env_file.read_text(encoding="utf-8").splitlines()
                values = [line.partition("=")[2] for line in entries if line.startswith(f"{TOKEN}=")]
                self.assertEqual(len(values), 1)
                self.assertTrue(re.fullmatch(r"[a-f0-9]{64}", values[0]), "invalid generated token")
                return values[0]

            token = prepare()
            self.assertTrue(token == prepare(), "reinitialization changed the token")
            env = {key: value for key, value in os.environ.items() if key not in {TOKEN, CATALOG_URL, MODERATION_TOKEN}}

            def render():
                rendered = subprocess.run(
                    [
                        "docker", "compose", "--env-file", str(env_file), "--project-name", "content-compose-test",
                        "-f", str(ROOT / "docker-compose.yml"), "--profile", "sandbox", "config", "--format", "json",
                    ],
                    cwd=root, env=env, check=True, capture_output=True, text=True, timeout=20,
                )
                return json.loads(rendered.stdout)["services"]

            services = render()
            self.assertTrue(services["content"]["environment"].get(TOKEN) == token, "Content did not get its token")
            gateway = services["api-gateway"]["environment"]
            self.assertTrue(gateway.get(TOKEN) == token, "Gateway did not get the Content token")
            self.assertEqual(gateway.get("CONTENT_GRPC_ADDR"), "content:9090")
            self.assertEqual(gateway.get("CONTENT_GRPC_INSECURE"), "true")
            self.assertEqual(services["content"]["environment"].get(CATALOG_URL), "http://entities:8080")
            grpc_ports = [port for port in services["content"]["ports"] if port["target"] == 9090]
            self.assertEqual(len(grpc_ports), 1)
            self.assertEqual(grpc_ports[0]["host_ip"], "127.0.0.1")
            self.assertNotIn(TOKEN, services["sandbox"]["environment"])
            self.assertFalse(gateway.get(MODERATION_TOKEN))
            self.assertFalse(services["content"]["environment"].get(MODERATION_TOKEN))
            env[MODERATION_TOKEN] = f"{901:064x}"
            services = render()
            for name, service in services.items():
                with self.subTest(service=name):
                    value = service.get("environment", {}).get(MODERATION_TOKEN)
                    if name in {"content", "api-gateway"}:
                        self.assertTrue(value == env[MODERATION_TOKEN], "moderation token did not reach the peer")
                    else:
                        self.assertFalse(value, "moderation token reached an unrelated service")


if __name__ == "__main__":
    unittest.main()
