import json
import os
from pathlib import Path
import subprocess
import unittest


ROOT = Path(__file__).resolve().parents[1]


def render_compose():
    env = os.environ.copy()
    env.pop("MEDIA_ALLOW_UNSCANNED_LOCAL", None)
    rendered = subprocess.run(
        ["docker", "compose", "--env-file", ".env.example", "--profile", "antivirus",
         "config", "--format", "json"],
        cwd=ROOT, env=env, capture_output=True, text=True, check=True, timeout=20,
    )
    return json.loads(rendered.stdout)


class MediaComposeTest(unittest.TestCase):
    def test_scanner_and_public_bucket_are_restricted(self):
        services = render_compose()["services"]
        self.assertEqual(services["media"]["environment"]["MEDIA_ALLOW_UNSCANNED_LOCAL"], "false")
        self.assertEqual(services["media"]["environment"]["MEDIA_CLAMAV_ADDR"], "clamav:3310")
        scanner = services["clamav"]
        self.assertNotIn("ports", scanner)
        self.assertEqual(scanner["environment"]["CLAMD_CONF_AlertExceedsMax"], "yes")
        self.assertEqual(scanner["environment"]["CLAMD_CONF_AlertEncrypted"], "yes")

        buckets = services["media-buckets"]
        script = "\n".join(buckets["command"])
        self.assertIn("mc anonymous set none local/media-quarantine", script)
        mount = next(volume for volume in buckets["volumes"] if volume["target"] == "/etc/media/public-policy.json")
        self.assertTrue(mount["read_only"])
        self.assertIn(f"mc anonymous set-json {mount['target']} local/media", script)
        policy = json.loads(Path(mount["source"]).read_text(encoding="utf-8"))
        self.assertEqual(policy["Statement"], [{
            "Effect": "Allow", "Principal": "*", "Action": "s3:GetObject",
            "Resource": "arn:aws:s3:::media/public/verified/variants/*/*.webp",
        }])

    def test_renderer_mounts_only_the_socket_without_network_or_secrets(self):
        compose = render_compose()
        services = compose["services"]
        renderer = services["media-renderer"]
        self.assertEqual(renderer["build"]["target"], "renderer")
        self.assertEqual(renderer["user"], "10002:10003")
        self.assertEqual(renderer["network_mode"], "none")
        self.assertTrue(renderer["read_only"])
        self.assertTrue(renderer["init"])
        self.assertEqual(renderer["cap_drop"], ["ALL"])
        self.assertEqual(renderer["security_opt"], ["no-new-privileges:true"])
        self.assertEqual(float(renderer["cpus"]), 1)
        self.assertEqual(int(renderer["mem_limit"]), 384 * (1 << 20))
        self.assertEqual(renderer["memswap_limit"], renderer["mem_limit"])
        self.assertEqual(renderer["pids_limit"], 32)
        for key in ("environment", "env_file", "secrets", "configs", "ports", "networks"):
            self.assertNotIn(key, renderer)

        self.assertEqual(len(renderer["tmpfs"]), 1)
        for flag in ("noexec", "nosuid", "nodev", "size=96m", "mode=0700"):
            self.assertIn(flag, renderer["tmpfs"][0])
        mount, = renderer["volumes"]
        self.assertEqual(mount["type"], "volume")
        self.assertEqual(mount["source"], "media-renderer-socket")
        self.assertEqual(mount["target"], "/run/media-renderer")
        socket = compose["volumes"][mount["source"]]["driver_opts"]
        self.assertEqual(socket["type"], "tmpfs")
        self.assertIn("size=1m", socket["o"])
        self.assertIn("gid=10003", socket["o"])
        media_mount, = services["media"]["volumes"]
        self.assertEqual(media_mount["source"], mount["source"])
        self.assertEqual(media_mount["target"], mount["target"])
        self.assertTrue(media_mount["read_only"])
