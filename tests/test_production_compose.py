import copy
import hashlib
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import production_compose as deploy


MODERATION_TOKEN = "CONTENT_GRPC_MODERATION_TOKEN"
SNAPSHOT_TOKEN = "ENTITIES_SNAPSHOT_TOKEN"


def fixture_release(sql):
    ref = "fixture.invalid/{name}@sha256:" + "a" * 64
    return {
        "images": {name: ref.format(name=name) for name in (*deploy.APPS, "gojudge", "media-renderer")},
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


class MaterialActivityComposeTest(unittest.TestCase):
    def test_cards_require_the_publisher_and_the_public_subscription(self):
        content = {
            "CONTENT_ACTIVITY_ENABLED": "true",
            "KAFKA_ENABLED": "true",
            "KAFKA_CONTENT_MATERIAL_TOPIC": "content.material.v1",
            "KAFKA_CONTENT_LEARNING_TOPIC": "content.learning.v1",
            "KAFKA_CONTENT_CONTRIBUTION_TOPIC": "content.contribution.v1",
            "KAFKA_TOPIC": "content.content-item-events",
        }
        feed = {"KAFKA_CONTENT_MATERIAL_TOPIC": "content.material.v1", "FEED_MATERIAL_CARDS_ENABLED": "true"}
        services = {"content": {"environment": content}, "feed": {"environment": feed}}
        deploy.check_material_activity(services)
        content["CONTENT_ACTIVITY_ENABLED"] = "false"
        with self.assertRaisesRegex(ValueError, "cards require"):
            deploy.check_material_activity(services)
        feed["FEED_MATERIAL_CARDS_ENABLED"] = "false"
        deploy.check_material_activity(services)
        for private in ["content.learning.v1", "content.contribution.v1", "content.content-item-events"]:
            with self.subTest(topic=private), self.assertRaisesRegex(ValueError, "public Content material"):
                feed["KAFKA_CONTENT_MATERIAL_TOPIC"] = private
                content["KAFKA_CONTENT_MATERIAL_TOPIC"] = private
                deploy.check_material_activity(services)

    def test_editorial_activity_requires_a_separate_provisioned_topic(self):
        key = "KAFKA_CONTENT_CONTRIBUTION_TOPIC"
        content = {
            "CONTENT_ACTIVITY_ENABLED": "true",
            "CONTENT_CONTRIBUTION_ENABLED": "true",
            "KAFKA_ENABLED": "true",
            "KAFKA_CONTENT_MATERIAL_TOPIC": "content.material.v1",
            "KAFKA_CONTENT_LEARNING_TOPIC": "content.learning.v1",
            key: "content.contribution.v1",
        }
        feed = {"KAFKA_TASK_ACTIVITY_TOPIC": "task.activity.v1"}
        plan = {key: content[key]}
        services = {"content": {"environment": content}, "feed": {"environment": feed}, "kafka-topics": {"environment": plan}}
        deploy.check_material_activity(services)
        content["CONTENT_ACTIVITY_ENABLED"] = "false"
        with self.assertRaisesRegex(ValueError, "requires Content material publishing"):
            deploy.check_material_activity(services)
        content["CONTENT_ACTIVITY_ENABLED"] = "true"
        plan[key] = "not-provisioned"
        with self.assertRaisesRegex(ValueError, "separate topic in the Kafka plan"):
            deploy.check_material_activity(services)
        for shared in ("content_material.v1", "task.activity.v1", "content.learning.v1"):
            with self.subTest(topic=shared), self.assertRaisesRegex(ValueError, "separate topic in the Kafka plan"):
                content[key] = plan[key] = shared
                deploy.check_material_activity(services)


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
        cls.content_tls = root / "content-tls"
        cls.content_tls.mkdir()
        for name in deploy.CONTENT_TLS_FILES:
            (cls.content_tls / name).write_bytes(b"unused-fixture\n")
        cls.migration = root / "001_init_runs.sql"
        sql = b"SELECT 1;\n"
        cls.migration.write_bytes(sql)
        cls.release = fixture_release(sql)
        fixture = root / "app.env"
        passwords = [
            f"{name.upper().replace('-', '_')}_POSTGRES_{role}={index * 3 + offset + 1:032x}"
            for index, name in enumerate(deploy.DATABASES)
            for offset, role in enumerate(("PASSWORD", "ADMIN_PASSWORD", "MIGRATOR_PASSWORD"))
        ]
        fixture.write_text("\n".join([
            *passwords,
            *(f"{name}={index + 100:064x}" for index, name in enumerate(deploy.SERVICE_TOKENS)),
            "BOOTSTRAP_SUPERUSER_EMAIL=operator@example.org",
            "BOOTSTRAP_SUPERUSER_PASSWORD=unused-fixture",
            "MEDIA_INTERNAL_TOKEN=",
            "MEDIA_ENABLE_UPLOAD_CLEANUP=true",
            "MEDIA_MAX_MULTIPART_SESSIONS=6",
            "MINIO_ROOT_USER=fixture-root",
            f"MINIO_ROOT_PASSWORD={101:040x}",
            "MEDIA_S3_ACCESS_KEY=ovm-media-00000001",
            f"MEDIA_S3_SECRET_KEY={102:040x}",
            f"CONTENT_GRPC_SERVICE_TOKEN={700:064x}",
            f"MEDIA_CONTENT_TOKEN={701:064x}",
            "MEDIA_S3_PUBLIC_ENDPOINT=https://s3.fixture.example.org",
            "MEDIA_PUBLIC_BASE_URL=https://s3.fixture.example.org/media",
            "CORS_ORIGINS=https://app.fixture.example.org",
            "SANDBOX_WORKER_ID=fixture-worker",
            "SANDBOX_GOJUDGE_BASE_URL=https://runner.fixture.invalid:8443",
            "SANDBOX_GOJUDGE_COMPILER_URL=https://runner.fixture.invalid:8444",
            "SANDBOX_TESTS_ACCESS_KEY_ID=ovm-tests-00000001",
            f"SANDBOX_TESTS_SECRET_ACCESS_KEY={103:040x}",
            "SANDBOX_ARTIFACTS_ACCESS_KEY_ID=ovm-art-00000001",
            f"SANDBOX_ARTIFACTS_SECRET_ACCESS_KEY={104:040x}",
        ]) + "\n", encoding="utf-8")
        cls.images = {}
        cls.model = {}
        for target in ("app", "runner"):
            cls.images[target] = deploy.image_env(cls.release, target)
            env = deploy.compose_env(cls.images[target], cls.migration, cls.runner_tls) if target == "app" else (
                deploy.compose_env(cls.images[target])
            )
            cls.model[target] = deploy.check_compose(deploy.compose_args(target, fixture), env)
        env = deploy.compose_env(cls.images["app"], cls.migration, cls.runner_tls, cls.content_tls)
        cls.tls_model = deploy.check_compose(deploy.compose_args("app", fixture, cls.content_tls), env)
        cls.snapshot_token = f"{703:064x}"
        env[SNAPSHOT_TOKEN] = cls.snapshot_token
        cls.snapshot_model = deploy.check_compose(deploy.compose_args("app", fixture, cls.content_tls), env)
        env.pop(SNAPSHOT_TOKEN)
        env["RECOMMENDATIONS_RESOURCE_PROJECTION_ENABLED"] = "true"
        env["CONTENT_GRPC_CATALOG_TOKEN"] = f"{704:064x}"
        cls.resource_model = deploy.check_compose(deploy.compose_args("app", fixture, cls.content_tls), env)
        env.pop("RECOMMENDATIONS_RESOURCE_PROJECTION_ENABLED")
        env.pop("CONTENT_GRPC_CATALOG_TOKEN")
        env.update({MODERATION_TOKEN: f"{902:064x}", "CONTENT_ACTIVITY_ENABLED": "true"})
        cls.moderation_model = deploy.check_compose(deploy.compose_args("app", fixture, cls.content_tls), env)
        cls.moderation_without_tls = deploy.check_compose(deploy.compose_args("app", fixture), env)

    def check_app(self, model):
        deploy.check_model(model, "app", self.images["app"], self.migration, self.runner_tls)

    def test_app_and_runner_are_separate(self):
        deploy.check_migration(self.release, self.migration)
        deploy.check_runner_tls_files(self.runner_tls)
        self.check_app(self.model["app"])
        deploy.check_model(self.model["runner"], "runner", self.images["runner"])
        self.assertNotEqual(self.model["app"]["networks"], self.model["runner"]["networks"])
        self.assertEqual(self.model["runner"]["services"].keys(), deploy.RUNNER_IMAGES.keys())
        self.assertTrue(self.model["runner"]["services"].keys().isdisjoint(self.model["app"]["services"]))

    def test_learner_goals_use_the_internal_topic_catalog(self):
        services = self.model["app"]["services"]
        recommendations = services["recommendations"]
        entities = services["entities"]
        self.assertEqual(recommendations["environment"]["ENTITIES_URL"], "http://entities:8080")
        self.assertTrue(recommendations["networks"].keys() & entities["networks"].keys())
        self.assertFalse(recommendations.get("ports"))
        self.assertFalse(entities.get("ports"))

    def test_catalog_snapshot_token_is_opt_in_and_scoped(self):
        current = self.model["app"]["services"]
        self.assertFalse(current["recommendations"]["environment"].get(SNAPSHOT_TOKEN))
        self.assertFalse(current["entities"]["environment"].get(SNAPSHOT_TOKEN))
        recipients = {
            name: svc["environment"][SNAPSHOT_TOKEN]
            for name, svc in self.snapshot_model["services"].items()
            if SNAPSHOT_TOKEN in svc.get("environment", {})
        }
        self.assertEqual(recipients, {"entities": self.snapshot_token, "recommendations": self.snapshot_token})

    def test_resource_projection_stays_off_without_production_transports(self):
        services = self.resource_model["services"]
        self.assertEqual(services["recommendations"]["environment"]["RECOMMENDATIONS_RESOURCE_PROJECTION_ENABLED"], "false")
        self.assertTrue(all("CONTENT_GRPC_CATALOG_TOKEN" not in svc.get("environment", {}) for svc in services.values()))

    def test_image_tag_is_rejected(self):
        release = copy.deepcopy(self.release)
        release["images"]["gojudge"] = "fixture.invalid/gojudge:latest"
        with self.assertRaisesRegex(ValueError, "manifest digest"):
            deploy.image_env(release, "runner")

    def test_wrong_migration_is_rejected(self):
        with self.assertRaisesRegex(ValueError, "release checksum"):
            deploy.check_migration(fixture_release(b"-- different"), self.migration)

    def test_renderer_rejects_credentials_and_writable_api_socket(self):
        for service, key, value in (
            ("media-renderer", "environment", {"FIXTURE": "not-a-secret"}),
            ("media-renderer", "network_mode", "host"),
            ("media", "volumes", [{**self.model["app"]["services"]["media"]["volumes"][0], "read_only": False}]),
        ):
            with self.subTest(service=service, key=key):
                model = copy.deepcopy(self.model["app"])
                model["services"][service][key] = value
                with self.assertRaises(ValueError):
                    self.check_app(model)

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

    def test_runner_rejects_overridden_parallelism(self):
        model = copy.deepcopy(self.model["runner"])
        model["services"]["gojudge-compiler"]["command"].append("-parallelism=3")
        with self.assertRaisesRegex(ValueError, "runner limits"):
            deploy.check_model(model, "runner", self.images["runner"])

    def test_other_project_on_host_blocks_start(self):
        with patch.object(deploy.subprocess, "check_output", side_effect=["29.7.2\n", "peer-network-id\n"]):
            with self.assertRaisesRegex(ValueError, "require separate VMs"):
                deploy.check_host("runner", {})

    def test_users_waits_for_demo_account_check(self):
        check_env = self.model["app"]["services"]["users-no-demo"]["environment"]
        self.assertIn("default_transaction_read_only=on", check_env["PGOPTIONS"])
        model = copy.deepcopy(self.model["app"])
        model["services"]["users"]["depends_on"]["users-no-demo"]["condition"] = "service_started"
        with self.assertRaisesRegex(ValueError, "demo account check"):
            self.check_app(model)

    def test_http_runner_is_rejected(self):
        model = copy.deepcopy(self.model["app"])
        model["services"]["sandbox"]["environment"]["SANDBOX_GOJUDGE_BASE_URL"] = "http://fixture.invalid"
        with self.assertRaisesRegex(ValueError, "HTTPS endpoints"):
            self.check_app(model)

    def test_gateway_does_not_trust_the_full_subnet(self):
        model = copy.deepcopy(self.model["app"])
        model["services"]["api-gateway"]["environment"]["TRUSTED_PROXY_CIDRS"] = "172.29.163.0/24"
        with self.assertRaisesRegex(ValueError, "exact host proxy address"):
            self.check_app(model)

    def test_gateway_does_not_inherit_local_content_credentials(self):
        gateway = self.model["app"]["services"]["api-gateway"]["environment"]
        for key in ("CONTENT_GRPC_ADDR", "CONTENT_GRPC_SERVICE_TOKEN", "CONTENT_GRPC_INSECURE", "CONTENT_GRPC_TIMEOUT"):
            with self.subTest(key=key):
                self.assertFalse(gateway.get(key))

    def test_production_does_not_inherit_local_media_binding_credentials(self):
        model = copy.deepcopy(self.model["app"])
        services = model["services"]
        self.assertFalse(services["content"]["environment"].get("CONTENT_MEDIA_URL"))
        self.assertFalse(services["content"]["environment"].get("CONTENT_MEDIA_TOKEN"))
        self.assertFalse(services["media"]["environment"].get("MEDIA_CONTENT_TOKEN"))
        services["content"]["environment"]["CONTENT_MEDIA_URL"] = "http://media:8080"
        with self.assertRaisesRegex(ValueError, "local Media binding credentials"):
            self.check_app(model)

    def test_content_tls_overlay_keeps_private_key_out_of_gateway(self):
        deploy.check_content_tls_files(self.content_tls)
        deploy.check_model(self.tls_model, "app", self.images["app"], self.migration, self.runner_tls, self.content_tls)
        services = self.tls_model["services"]
        gateway = services["api-gateway"]
        self.assertEqual(gateway["environment"]["CONTENT_GRPC_INSECURE"], "false")
        self.assertEqual([Path(volume["source"]).name for volume in gateway["volumes"] if volume["type"] == "bind"],
                         ["server-ca.pem"])
        self.assertIn("10004", services["content"]["group_add"])

    def test_content_tls_overlay_rejects_plaintext_and_reused_token(self):
        for key, value in (("CONTENT_GRPC_INSECURE", "true"),
                           ("CONTENT_GRPC_SERVICE_TOKEN", self.tls_model["services"]["api-gateway"]["environment"]["JWT_SECRET"])):
            with self.subTest(key=key):
                model = copy.deepcopy(self.tls_model)
                services = model["services"]
                services["api-gateway"]["environment"][key] = value
                if key == "CONTENT_GRPC_SERVICE_TOKEN":
                    services["content"]["environment"][key] = value
                with self.assertRaisesRegex(ValueError, "Content TLS connection|unique 64-digit"):
                    deploy.check_model(model, "app", self.images["app"], self.migration, self.runner_tls, self.content_tls)

    def test_moderation_requires_tls_and_material_activity(self):
        self.check_app(self.moderation_without_tls)
        for name in ("content", "api-gateway"):
            self.assertFalse(self.moderation_without_tls["services"][name]["environment"].get(MODERATION_TOKEN))
        services = self.moderation_model["services"]
        self.assertTrue(bool(services["api-gateway"]["environment"].get(MODERATION_TOKEN)), "missing moderation token")
        self.assertTrue(bool(services["content"]["environment"].get(MODERATION_TOKEN)), "missing moderation token")
        deploy.check_model(self.moderation_model, "app", self.images["app"], self.migration, self.runner_tls, self.content_tls)

        for kind, reason in (
            ("reused", "unique 64-digit moderation token"),
            ("mismatched", "unique 64-digit moderation token"),
            ("leaked", "only Content and Gateway"),
            ("no-events", "requires Kafka material activity"),
        ):
            with self.subTest(kind=kind):
                model = copy.deepcopy(self.moderation_model)
                peers = model["services"]
                gateway = peers["api-gateway"]["environment"]
                content = peers["content"]["environment"]
                if kind == "reused":
                    content[MODERATION_TOKEN] = gateway[MODERATION_TOKEN] = gateway["CONTENT_GRPC_SERVICE_TOKEN"]
                elif kind == "mismatched":
                    content[MODERATION_TOKEN] = ""
                elif kind == "leaked":
                    peers["sandbox"]["environment"][MODERATION_TOKEN] = gateway[MODERATION_TOKEN]
                else:
                    content["CONTENT_ACTIVITY_ENABLED"] = "false"
                with self.assertRaisesRegex(ValueError, reason):
                    deploy.check_model(model, "app", self.images["app"], self.migration, self.runner_tls, self.content_tls)

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

    def test_database_roles_need_distinct_passwords(self):
        model = copy.deepcopy(self.model["app"])
        env = model["services"]["tasks-postgres"]["environment"]
        env["POSTGRES_PASSWORD"] = env["OVM_DB_APP_PASSWORD"]
        with self.assertRaisesRegex(ValueError, "credentials must not be reused"):
            self.check_app(model)

    def test_runtime_cannot_use_the_migration_dsn(self):
        model = copy.deepcopy(self.model["app"])
        services = model["services"]
        services["feed"]["environment"]["DATABASE_URL"] = services["feed-migrate"]["environment"]["DATABASE_URL"]
        with self.assertRaisesRegex(ValueError, "runtime database role"):
            self.check_app(model)

    def test_runtime_must_wait_for_grants(self):
        model = copy.deepcopy(self.model["app"])
        model["services"]["content"]["depends_on"]["content-grants"]["required"] = False
        with self.assertRaisesRegex(ValueError, "grants must finish"):
            self.check_app(model)

    def test_database_cannot_use_another_services_data_volume(self):
        model = copy.deepcopy(self.model["app"])
        data = next(volume for volume in model["services"]["media-postgres"]["volumes"] if volume["type"] == "volume")
        data["source"] = "users-postgres"
        with self.assertRaisesRegex(ValueError, "unexpected database data volume"):
            self.check_app(model)

    def test_minio_root_secret_cannot_reach_media(self):
        model = copy.deepcopy(self.model["app"])
        model["services"]["media"]["environment"]["MINIO_ROOT_PASSWORD"] = "unexpected"
        with self.assertRaisesRegex(ValueError, "root credentials must stay out"):
            self.check_app(model)

    def test_upload_cleanup_requires_retention_rules(self):
        model = copy.deepcopy(self.model["app"])
        model["services"]["media"]["environment"]["MEDIA_ENABLE_UPLOAD_CLEANUP"] = "true"
        with self.assertRaisesRegex(ValueError, "approved retention rules"):
            self.check_app(model)

    def test_multipart_session_limit_is_configurable(self):
        model = copy.deepcopy(self.model["app"])
        media = model["services"]["media"]["environment"]
        self.assertEqual(media["MEDIA_MAX_MULTIPART_SESSIONS"], "6")
        self.check_app(model)
        for value in ("0", "-1", "1001", "unlimited"):
            with self.subTest(value=value), self.assertRaisesRegex(ValueError, "multipart session limit"):
                media["MEDIA_MAX_MULTIPART_SESSIONS"] = value
                self.check_app(model)

    def test_minio_bootstrap_and_runtime_must_agree(self):
        model = copy.deepcopy(self.model["app"])
        model["services"]["media-buckets"]["environment"]["MEDIA_S3_ACCESS_KEY"] = "ovm-media-00000002"
        with self.assertRaisesRegex(ValueError, "dedicated MinIO identity"):
            self.check_app(model)

    def test_service_secrets_must_be_distinct(self):
        model = copy.deepcopy(self.model["app"])
        gateway = model["services"]["api-gateway"]["environment"]
        model["services"]["media"]["environment"]["MEDIA_GATEWAY_TOKEN"] = gateway["FEED_TOKEN"]
        gateway["MEDIA_TOKEN"] = gateway["FEED_TOKEN"]
        with self.assertRaisesRegex(ValueError, "unique 64-digit"):
            self.check_app(model)

    def test_service_token_must_match_both_ends(self):
        model = copy.deepcopy(self.model["app"])
        model["services"]["feed"]["environment"]["FEED_GATEWAY_TOKEN"] = f"{1000:064x}"
        with self.assertRaisesRegex(ValueError, "both services must use the same token"):
            self.check_app(model)

    def test_compiler_cannot_use_the_execution_endpoint(self):
        model = copy.deepcopy(self.model["app"])
        model["services"]["sandbox"]["environment"]["SANDBOX_GOJUDGE_COMPILER_URL"] = "https://RUNNER.fixture.invalid:8443"
        with self.assertRaisesRegex(ValueError, "distinct runner endpoints"):
            self.check_app(model)

    def test_compiler_cannot_be_local(self):
        for endpoint in ("https://127.0.0.1:15051", "https://[::ffff:127.0.0.1]:15051"):
            with self.subTest(endpoint=endpoint):
                model = copy.deepcopy(self.model["app"])
                model["services"]["sandbox"]["environment"]["SANDBOX_GOJUDGE_COMPILER_URL"] = endpoint
                with self.assertRaisesRegex(ValueError, "separate host"):
                    self.check_app(model)


if __name__ == "__main__":
    unittest.main()
