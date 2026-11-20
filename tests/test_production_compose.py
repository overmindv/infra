import copy
import hashlib
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import production_compose as deploy


def fixture_release(sql):
    ref = "fixture.invalid/{name}@sha256:" + "a" * 64
    return {
        "images": {name: ref.format(name=name) for name in (*deploy.APPS, "gojudge")},
        "plan": {
            "version": 1,
            "platform": "linux/amd64",
            "upstream": {
                name: ref.format(name=name) for name in ("postgres", "kafka", "minio", "clamav")
            },
            "metadata": {"sandbox": {"migrations": [{
                "path": deploy.MIGRATION,
                "sha256": hashlib.sha256(sql).hexdigest(),
            }]}},
        },
    }


class ProductionComposeTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        temp = tempfile.TemporaryDirectory()
        cls.addClassCleanup(temp.cleanup)
        root = Path(temp.name).resolve()
        cls.runner_tls = root / "worker-tls"
        cls.runner_tls.mkdir()
        for name in deploy.RUNNER_TLS_FILES.values():
            (cls.runner_tls / name).write_bytes(b"unused-fixture\n")
        cls.migration = root / "001_init_runs.sql"
        sql = b"SELECT 1;\n"
        cls.migration.write_bytes(sql)
        cls.release = fixture_release(sql)
        fixture = root / "app.env"
        passwords = [
            f"{name.upper().replace('-', '_')}_POSTGRES_PASSWORD={'a' * 32}"
            for name in deploy.DATABASES
        ]
        fixture.write_text("\n".join([
            *passwords,
            "BOOTSTRAP_SUPERUSER_EMAIL=operator@example.org",
            "BOOTSTRAP_SUPERUSER_PASSWORD=unused-fixture",
            "JWT_SECRET=unused-fixture",
            "FEED_GATEWAY_TOKEN=unused-fixture",
            "TASK_HUNTER_INGEST_TOKEN=unused-fixture",
            "TASK_HUNTER_GATEWAY_TOKEN=unused-fixture",
            "RECOMMENDATIONS_SERVICE_TOKEN=unused-fixture",
            "MEDIA_INTERNAL_TOKEN=",
            "MEDIA_GATEWAY_TOKEN=unused-fixture",
            "MEDIA_USERS_TOKEN=unused-fixture",
            "MINIO_ROOT_USER=fixture-root",
            "MINIO_ROOT_PASSWORD=unused-fixture",
            "MEDIA_S3_ACCESS_KEY=fixture-media",
            "MEDIA_S3_SECRET_KEY=unused-fixture",
            "MEDIA_S3_PUBLIC_ENDPOINT=https://s3.fixture.example.org",
            "MEDIA_PUBLIC_BASE_URL=https://s3.fixture.example.org/media",
            "CORS_ORIGINS=https://app.fixture.example.org",
            "SANDBOX_WORKER_ID=fixture-worker",
            "SANDBOX_GOJUDGE_BASE_URL=https://runner.fixture.invalid:8443",
            "SANDBOX_TESTS_ACCESS_KEY_ID=fixture-tests",
            "SANDBOX_TESTS_SECRET_ACCESS_KEY=unused-fixture",
            "SANDBOX_ARTIFACTS_ACCESS_KEY_ID=fixture-artifacts",
            "SANDBOX_ARTIFACTS_SECRET_ACCESS_KEY=unused-fixture",
        ]) + "\n", encoding="utf-8")
        cls.images = {}
        cls.model = {}
        for target in ("app", "runner"):
            cls.images[target] = deploy.image_env(cls.release, target)
            env = deploy.compose_env(cls.images[target], cls.migration, cls.runner_tls) if target == "app" else (
                deploy.compose_env(cls.images[target])
            )
            cls.model[target] = deploy.check_compose(deploy.compose_args(target, fixture), env)

    def check_app(self, model):
        deploy.check_model(model, "app", self.images["app"], self.migration, self.runner_tls)

    def test_app_and_runner_are_separate(self):
        deploy.check_migration(self.release, self.migration)
        deploy.check_runner_tls_files(self.runner_tls)
        self.check_app(self.model["app"])
        deploy.check_model(self.model["runner"], "runner", self.images["runner"])
        self.assertNotEqual(self.model["app"]["networks"], self.model["runner"]["networks"])

    def test_image_tag_is_rejected(self):
        release = copy.deepcopy(self.release)
        release["images"]["gojudge"] = "fixture.invalid/gojudge:latest"
        with self.assertRaisesRegex(ValueError, "manifest digest"):
            deploy.image_env(release, "runner")

    def test_wrong_migration_is_rejected(self):
        with self.assertRaisesRegex(ValueError, "release checksum"):
            deploy.check_migration(fixture_release(b"-- different"), self.migration)

    def test_public_binding_is_rejected(self):
        model = copy.deepcopy(self.model["app"])
        model["services"]["frontend"]["ports"][0]["host_ip"] = "0.0.0.0"
        with self.assertRaisesRegex(ValueError, "unexpected published port"):
            self.check_app(model)

    def test_shared_data_volume_is_rejected(self):
        model = copy.deepcopy(self.model["app"])
        model["volumes"]["users-postgres"]["name"] = "overmindv_users-postgres"
        with self.assertRaisesRegex(ValueError, "production project"):
            self.check_app(model)

    def test_runner_rejects_credentials_and_hosts(self):
        for key, value in (("environment", {"FIXTURE": "not-a-secret"}), ("network_mode", "host")):
            with self.subTest(key=key):
                model = copy.deepcopy(self.model["runner"])
                model["services"]["gojudge"][key] = value
                with self.assertRaises(ValueError):
                    deploy.check_model(model, "runner", self.images["runner"])

    def test_other_project_on_host_blocks_start(self):
        with patch.object(deploy.subprocess, "check_output", side_effect=["29.7.2\n", "peer-network-id\n"]):
            with self.assertRaisesRegex(ValueError, "require separate VMs"):
                deploy.check_host("runner", {})

    def test_users_waits_for_demo_account_check(self):
        model = copy.deepcopy(self.model["app"])
        model["services"]["users"]["depends_on"]["users-no-demo"]["condition"] = "service_started"
        with self.assertRaisesRegex(ValueError, "demo account check"):
            self.check_app(model)

    def test_http_runner_is_rejected(self):
        model = copy.deepcopy(self.model["app"])
        model["services"]["sandbox"]["environment"]["SANDBOX_GOJUDGE_BASE_URL"] = "http://fixture.invalid"
        with self.assertRaisesRegex(ValueError, "HTTPS URLs"):
            self.check_app(model)

    def test_gateway_does_not_trust_the_full_subnet(self):
        model = copy.deepcopy(self.model["app"])
        model["services"]["api-gateway"]["environment"]["TRUSTED_PROXY_CIDRS"] = "172.29.163.0/24"
        with self.assertRaisesRegex(ValueError, "exact host proxy address"):
            self.check_app(model)

    def test_object_origin_cannot_share_the_web_hostname(self):
        model = copy.deepcopy(self.model["app"])
        model["services"]["media"]["environment"]["MEDIA_S3_PUBLIC_ENDPOINT"] = "https://app.fixture.example.org"
        with self.assertRaisesRegex(ValueError, "origin distinct from object storage"):
            self.check_app(model)

    def test_worker_tls_mount_must_be_read_only(self):
        model = copy.deepcopy(self.model["app"])
        model["services"]["sandbox"]["volumes"][0]["read_only"] = False
        with self.assertRaisesRegex(ValueError, "unexpected host mount"):
            self.check_app(model)

    def test_runner_cannot_receive_worker_key_directory(self):
        model = copy.deepcopy(self.model["runner"])
        model["services"]["gojudge"]["volumes"] = self.model["app"]["services"]["sandbox"]["volumes"]
        with self.assertRaisesRegex(ValueError, "data and secrets"):
            deploy.check_model(model, "runner", self.images["runner"])

    def test_worker_tls_directory_rejects_unrelated_files(self):
        with tempfile.TemporaryDirectory() as path:
            directory = Path(path)
            for name in (*deploy.RUNNER_TLS_FILES.values(), "other.key"):
                (directory / name).write_bytes(b"unused-fixture\n")
            with self.assertRaisesRegex(ValueError, "must contain only"):
                deploy.check_runner_tls_files(directory)


if __name__ == "__main__":
    unittest.main()
