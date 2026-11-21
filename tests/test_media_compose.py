import json
import os
from pathlib import Path
import subprocess
import unittest


ROOT = Path(__file__).resolve().parents[1]


class MediaComposeTest(unittest.TestCase):
    def test_scanner_and_public_bucket_are_restricted(self):
        env = os.environ.copy()
        env.pop("MEDIA_ALLOW_UNSCANNED_LOCAL", None)
        rendered = subprocess.run(
            ["docker", "compose", "--env-file", ".env.example", "--profile", "antivirus",
             "config", "--format", "json"],
            cwd=ROOT, env=env, capture_output=True, text=True, check=True, timeout=20,
        )
        services = json.loads(rendered.stdout)["services"]
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
