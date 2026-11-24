import hashlib
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import ANY, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

import release_manifest as release
import release_scan as scanner
from release_scan_policy import release_images
from release_sources import archive, git, pin_dockerfile


DIGEST = "sha256:" + "a" * 64
PUSHED_DIGEST = "sha256:" + "b" * 64
BASE = "alpine:ovm165-missing-tag"


class ReleaseTest(unittest.TestCase):
    def setUp(self):
        work = tempfile.TemporaryDirectory()
        self.addCleanup(work.cleanup)
        self.workspace = Path(work.name)
        self.repositories = {"infra", "users"}
        self.catalog = {"users": ("users", "Dockerfile")}
        for attr, value in {
            "REPOS": self.repositories, "DOCKERFILES": self.catalog, "UPSTREAM": {},
        }.items():
            mock = patch.object(release, attr, value)
            mock.start()
            self.addCleanup(mock.stop)

        self.sources = {}
        self.repo = self.workspace / "users"
        for name in self.repositories:
            repo = self.workspace / name
            repo.mkdir()
            git(repo, "init", "-q")
            git(repo, "config", "user.email", "example@example.invalid")
            git(repo, "config", "user.name", "Release test")
            git(repo, "config", "core.autocrlf", "false")
            git(repo, "config", "commit.gpgsign", "false")
            git(repo, "remote", "add", "origin", f"https://github.com/overmindv/{name}.git")
            (repo / "source.txt").write_bytes(b"committed\n")
            if name == "users":
                (repo / "Dockerfile").write_bytes(
                    f"ARG BASE={BASE}\nFROM ${{BASE}}\nCOPY . /payload\n".encode()
                )
                migration = repo / "migrations" / "001_init.sql"
                migration.parent.mkdir()
                migration.write_bytes(b"SELECT 1;\n")
                schema = repo / "api" / "user.v1.json"
                schema.parent.mkdir()
                schema.write_text(json.dumps({
                    "$id": "user.v1",
                    "properties": {"schema_version": {"const": 1}},
                }), encoding="utf-8")
            git(repo, "add", ".")
            git(repo, "commit", "-qm", "Source fixture")
            sha = git(repo, "rev-parse", "HEAD").decode().strip()
            git(repo, "update-ref", "refs/remotes/origin/main", sha)
            self.sources[name] = sha
        (self.repo / "source.txt").write_bytes(b"dirty\n")
        (self.repo / ".env").write_bytes(b"FIXTURE_ONLY=untracked\n")

    def plan(self, pins=None, registry="example.invalid/overmindv", *, with_postgres=False):
        return release.make_plan(self.workspace, self.sources,
                                 {BASE: DIGEST} if pins is None else pins,
                                 registry, "v1", with_postgres=with_postgres)

    def test_reads_only_committed_sources(self):
        plan = self.plan()
        migrations = plan["metadata"]["users"]["migrations"]
        self.assertEqual(migrations, [{
            "path": "migrations/001_init.sql", "version": "001",
            "sha256": hashlib.sha256(b"SELECT 1;\n").hexdigest(),
        }])
        schema, = plan["metadata"]["users"]["schemas"]
        self.assertEqual((schema["schema_id"], schema["version"]), ("user.v1", 1))
        self.assertEqual(plan["images"]["users"]["bases"], {BASE: f"alpine@{DIGEST}"})
        git(self.repo, "config", "core.autocrlf", "true")
        destination = self.workspace / "clean"
        archive(self.repo, self.sources["users"], destination)
        self.assertEqual((destination / "source.txt").read_bytes(), b"committed\n")
        self.assertFalse((destination / ".env").exists())
        self.assertEqual((self.repo / "source.txt").read_bytes(), b"dirty\n")

    def test_rejects_short_sha_and_unpushed_commit(self):
        git(self.repo, "add", "source.txt")
        git(self.repo, "commit", "-qm", "Unpublished fixture")
        sha = git(self.repo, "rev-parse", "HEAD").decode().strip()
        self.sources["users"] = sha[:10]
        with self.assertRaisesRegex(ValueError, "full lowercase commit SHA"):
            self.plan()
        self.sources["users"] = sha
        with self.assertRaisesRegex(ValueError, "not in a fetched origin branch"):
            self.plan()

    def test_rejects_wrong_origin_and_local_attributes(self):
        (self.repo / ".git/info/attributes").write_bytes(b"source.txt export-ignore\n")
        with self.assertRaisesRegex(ValueError, "local info/attributes"):
            self.plan()
        git(self.repo, "remote", "set-url", "origin", "https://github.com/example/users")
        with self.assertRaisesRegex(ValueError, "origin must point to"):
            self.plan()

    def test_requires_base_and_runtime_pins(self):
        with self.assertRaisesRegex(ValueError, "missing or invalid digest for alpine"):
            self.plan({})
        with patch.object(release, "UPSTREAM", {"postgres": "postgres:17-alpine"}):
            with self.assertRaisesRegex(ValueError, "missing or invalid digest for postgres"):
                self.plan()

    def test_kafka_patch_pin_preserves_legacy_plans(self):
        original = "apache/kafka:4.3.1"
        updated = release.KAFKA_PATCH_REF
        patch_digest = release.KAFKA_PATCH_DIGEST
        with patch.object(release, "UPSTREAM", {"kafka": original}):
            old_pins = {BASE: DIGEST, original: DIGEST}
            old = self.plan(old_pins)
            release.validate_plan(old)
            self.assertEqual(
                self.plan({**old_pins, updated: patch_digest})["upstream"], old["upstream"]
            )
            patch_plan = self.plan({BASE: DIGEST, updated: patch_digest})
            release.validate_plan(patch_plan)
            self.assertEqual(patch_plan["upstream"], {"kafka": f"apache/kafka@{patch_digest}"})
            patch_plan["upstream"] = old["upstream"]
            with self.assertRaisesRegex(ValueError, "upstream images do not match"):
                release.validate_plan(patch_plan)
            with self.assertRaisesRegex(ValueError, "expected reviewed Kafka digest"):
                self.plan({BASE: DIGEST, updated: PUSHED_DIGEST})

    def test_rejects_changed_plan_before_docker(self):
        plan = self.plan()
        plan["metadata"]["users"]["migrations"].clear()
        with self.assertRaisesRegex(ValueError, "does not match the pinned source trees"):
            release.build(self.workspace, plan)

    def test_builds_optional_postgres_from_infra_only_for_a_private_candidate(self):
        postgres, path = release.POSTGRES_DOCKERFILE
        repo = self.workspace / postgres
        recipe = repo / path
        recipe.parent.mkdir(parents=True)
        recipe.write_bytes(f"FROM postgres@{DIGEST}\n".encode())
        git(repo, "add", path)
        git(repo, "commit", "-qm", "Postgres fixture")
        sha = git(repo, "rev-parse", "HEAD").decode().strip()
        git(repo, "update-ref", "refs/remotes/origin/main", sha)
        self.sources[postgres] = sha
        recipe.write_bytes(b"FROM scratch\n")
        upstream = "postgres:17-alpine"
        registry = release.CANDIDATE_REGISTRY
        with patch.object(release, "UPSTREAM", {"postgres": upstream}):
            legacy = self.plan({BASE: DIGEST, upstream: PUSHED_DIGEST}, registry)
            release.validate_plan(legacy)
            self.assertEqual(legacy["upstream"], {"postgres": f"postgres@{PUSHED_DIGEST}"})
            self.assertNotIn("postgres", legacy["images"])
            with self.assertRaisesRegex(ValueError, "private candidates"):
                self.plan(with_postgres=True)
            plan = self.plan(registry=registry, with_postgres=True)
            self.assertEqual(plan["upstream"], {})
            self.assertEqual(plan["images"]["postgres"], {
                "source": "infra", "dockerfile": path, "bases": {f"postgres@{DIGEST}": f"postgres@{DIGEST}"},
            })
            calls = []
            run = subprocess.run

            def check_build(command, **opts):
                if command[0] != "docker":
                    return run(command, **opts)
                dockerfile = Path(command[command.index("--file") + 1])
                if "io.overmindv.release.service=postgres" in command:
                    self.assertEqual(dockerfile.read_bytes(), f"FROM postgres@{DIGEST}\n".encode())
                    self.assertIn(f"org.opencontainers.image.revision={sha}", command)
                    calls.append(dockerfile)
                return subprocess.CompletedProcess(command, 0)

            with (patch("subprocess.run", side_effect=check_build),
                  patch.object(release, "inspect", return_value=DIGEST)):
                self.assertEqual(release.build(self.workspace, plan)["local_images"],
                                 {"postgres": DIGEST, "users": DIGEST})
            self.assertEqual(len(calls), 1)
            refs = {name: f"{registry}/{name}@{PUSHED_DIGEST}" for name in plan["images"]}
            manifest = {"plan": plan, "images": refs}
            self.assertEqual(release_images(manifest), refs)
            report = self.workspace / "scan"
            with (
                patch.object(scanner, "source_scan", return_value=[]),
                patch.object(scanner, "go_scan", return_value=[]),
                patch.object(scanner, "image_scan", return_value=[]) as scan_image,
                patch("builtins.print"),
            ):
                self.assertEqual(scanner.scan_release(manifest, self.workspace, refs, report), 0)
            scan_image.assert_any_call("postgres", refs["postgres"], release.PLATFORM, ANY, report)
            plan["upstream"] = legacy["upstream"]
            with self.assertRaisesRegex(ValueError, "upstream images do not match"):
                release.validate_plan(plan)

    def test_candidate_labels_link_private_docs_and_keep_source_provenance(self):
        plan = self.plan(registry=release.CANDIDATE_REGISTRY)
        plan_id = release.fingerprint(plan)
        labels = release.labels_for(plan, "users", plan_id)
        self.assertEqual(labels["org.opencontainers.image.source"], "https://github.com/overmindv/docs")
        self.assertEqual(labels["io.overmindv.release.source"], "https://github.com/overmindv/users")
        self.assertEqual(labels["org.opencontainers.image.revision"], self.sources["users"])
        self.assertEqual(labels["io.overmindv.release.plan"], plan_id)
        regular = release.labels_for(self.plan(), "users", plan_id)
        self.assertEqual(regular["org.opencontainers.image.source"], "https://github.com/overmindv/users")
        self.assertNotIn("io.overmindv.release.source", regular)

    def test_builds_declared_renderer_target_from_the_pinned_service_source(self):
        stage_images = {"users-renderer": ("users", "Dockerfile", "renderer")}
        with patch.object(release, "STAGE_IMAGES", stage_images):
            self.assertNotIn("users-renderer", self.plan()["images"])
            with (self.repo / "Dockerfile").open("ab") as spec:
                spec.write(b"FROM scratch AS renderer\nFROM scratch AS api\n")
            git(self.repo, "add", "Dockerfile")
            git(self.repo, "commit", "-qm", "Renderer fixture")
            sha = git(self.repo, "rev-parse", "HEAD").decode().strip()
            git(self.repo, "update-ref", "refs/remotes/origin/main", sha)
            self.sources["users"] = sha

            plan = self.plan()
            image = plan["images"]["users-renderer"]
            self.assertEqual(image, {**plan["images"]["users"], "target": "renderer"})
            image["target"] = "api"
            with self.assertRaisesRegex(ValueError, "unexpected image source"):
                release.validate_plan(plan)
            image["target"] = "renderer"
            commands = []
            run = subprocess.run

            def check_build(command, **opts):
                if command[0] != "docker":
                    return run(command, **opts)
                commands.append(command)
                path = Path(command[command.index("--file") + 1])
                self.assertIn(f"FROM alpine@{DIGEST}", path.read_text(encoding="utf-8"))
                return subprocess.CompletedProcess(command, 0)

            with patch("subprocess.run", side_effect=check_build), patch.object(
                release, "inspect", return_value=DIGEST
            ):
                receipt = release.build(self.workspace, plan)
            self.assertEqual(receipt["local_images"].keys(), {"users", "users-renderer"})
            direct, renderer = commands
            self.assertNotIn("--target", direct)
            self.assertEqual(renderer[renderer.index("--target") + 1], "renderer")

    def test_publish_refuses_replaced_image_before_tagging(self):
        receipt = {"plan": self.plan(), "local_images": {"users": DIGEST}}
        with patch.object(release, "inspect", return_value=PUSHED_DIGEST):
            with patch("subprocess.run") as run:
                with self.assertRaisesRegex(ValueError, "does not match build receipt"):
                    release.publish(receipt)
                run.assert_not_called()

    def test_publish_returns_distribution_digest_not_local_id(self):
        receipt = {"plan": self.plan(), "local_images": {"users": DIGEST}}
        tag = f"rel-v1-{'a' * 64}"
        with (
            patch.object(release, "inspect", return_value=DIGEST),
            patch("subprocess.run") as run,
            patch("subprocess.check_output", return_value=(
                f"{tag}: digest: {PUSHED_DIGEST} size: 42\n"
            )) as push,
            patch("builtins.print"),
        ):
            result = release.publish(receipt)
        repo = "example.invalid/overmindv/users"
        run.assert_called_once_with(
            ["docker", "image", "tag", DIGEST, f"{repo}:{tag}"], check=True
        )
        push.assert_called_once_with(
            ["docker", "image", "push", f"{repo}:{tag}"], text=True
        )
        self.assertEqual(result["images"], {"users": f"{repo}@{PUSHED_DIGEST}"})

    @unittest.skipUnless(os.getenv("RELEASE_TEST_BASE_DIGEST"), "needs Docker and an Alpine pin")
    def test_build_from_pinned_archive_with_docker(self):
        registry = os.getenv("RELEASE_TEST_REGISTRY")
        plan = self.plan({BASE: os.environ["RELEASE_TEST_BASE_DIGEST"]},
                         registry=registry or "example.invalid/overmindv")
        receipt = release.build(self.workspace, plan)
        image = receipt["local_images"]["users"]
        if registry:
            published = release.publish(receipt)
            image = published["images"]["users"]
            subprocess.run(["docker", "pull", image], check=True)
        subprocess.run([
            "docker", "run", "--rm", "--network", "none", "--entrypoint", "/bin/sh",
            image, "-ec", 'test "$(cat /payload/source.txt)" = committed; '
            "test ! -e /payload/.env",
        ], check=True)


class DockerfileTest(unittest.TestCase):
    def test_preserves_stage_references_and_replaces_argument_base(self):
        spec = (
            "ARG VERSION=3.22\nFROM\\\n alpine:${VERSION} AS build\n"
            "FROM build AS api\n"
            "COPY --from=build /source /target\nFROM api\n"
        )
        pinned, bases = pin_dockerfile(spec, {"alpine:3.22": DIGEST})
        self.assertIn(f"FROM alpine@{DIGEST} AS build\n", pinned)
        self.assertIn("FROM build AS api\n", pinned)
        self.assertIn("COPY --from=build /source /target\n", pinned)
        self.assertEqual(bases, {"alpine:3.22": f"alpine@{DIGEST}"})

    def test_rejects_hidden_external_image_sources(self):
        for spec in (
            "# syntax=docker/dockerfile:latest\nFROM scratch\n",
            "FROM scratch\nCOPY --from=alpine /x /y\n",
            "FROM scratch\nRUN --mount=type=bind,from=alpine,target=/x true\n",
            "FROM scratch\nADD https://example.invalid/foo /foo\n",
        ):
            with self.subTest(spec=spec), self.assertRaises(ValueError):
                pin_dockerfile(spec, {})


if __name__ == "__main__":
    unittest.main()
