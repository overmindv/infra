#!/usr/bin/env python3
import argparse
import hashlib
import json
import re
import subprocess
import sys
import tempfile
import uuid
from pathlib import Path

from release_sources import (
    DIGEST, DOCKERFILES, IMAGE, REPOS, SHA, STAGE_IMAGES, UPSTREAM,
    archive, git, image_name, metadata, pin_dockerfile, pinned_image,
    remote_source, verify_ref,
)


PLATFORM = "linux/amd64"
RELEASE_ID = re.compile(r"[a-z0-9][a-z0-9.-]{0,47}")
CANDIDATE_REGISTRY = "ghcr.io/overmindv/rc"
KAFKA_PATCH_REF = "apache/kafka:4.3.2"
KAFKA_PATCH_DIGEST = "sha256:852a0d49fc4e656b022e7f23785411802c9a088c78a7c5e39f33969de9c227a7"
POSTGRES_DOCKERFILE = ("infra", "deploy/postgres/Dockerfile")


def unique_keys(pairs):
    data = {}
    for key, value in pairs:
        if key in data:
            raise ValueError(f"duplicate JSON key: {key}")
        data[key] = value
    return data


def read_json(path):
    with path.open(encoding="utf-8") as file:
        return json.load(file, object_pairs_hook=unique_keys)


def write_json(path, data):
    with path.open("x", encoding="utf-8", newline="\n") as file:
        file.write(json.dumps(data, indent=2, sort_keys=True) + "\n")


def fingerprint(plan):
    return hashlib.sha256(json.dumps(plan, sort_keys=True).encode()).hexdigest()


def plan_inputs(sources, pins, registry, release):
    if not isinstance(sources, dict) or sources.keys() != REPOS:
        raise ValueError(f"sources must contain exactly: {', '.join(sorted(REPOS))}")
    for name, sha in sources.items():
        if not isinstance(sha, str) or not SHA.fullmatch(sha):
            raise ValueError(f"{name}: expected a full lowercase commit SHA")
    if not isinstance(pins, dict):
        raise ValueError("pins must be an object mapping image refs to digests")
    for ref, digest in pins.items():
        image_name(ref)
        if not isinstance(digest, str) or not DIGEST.fullmatch(digest):
            raise ValueError(f"{ref}: expected sha256 digest")
    if not isinstance(registry, str) or not IMAGE.fullmatch(registry):
        raise ValueError("expected a registry namespace without a tag or scheme")
    if not isinstance(release, str) or not RELEASE_ID.fullmatch(release):
        raise ValueError("expected a lowercase release name (1-48 characters)")


def release_dockerfiles(registry, with_postgres):
    if not with_postgres:
        return DOCKERFILES
    if registry != CANDIDATE_REGISTRY:
        raise ValueError("the built Postgres image is only allowed in private candidates")
    return {**DOCKERFILES, "postgres": POSTGRES_DOCKERFILE}


def pinned_upstream(pins, *, with_postgres=False):
    refs = UPSTREAM
    if "kafka" in refs and refs["kafka"] not in pins and KAFKA_PATCH_REF in pins:
        if pins[KAFKA_PATCH_REF] != KAFKA_PATCH_DIGEST:
            raise ValueError(f"{KAFKA_PATCH_REF}: expected reviewed Kafka digest")
        refs = {**refs, "kafka": KAFKA_PATCH_REF}
    return {service: pinned_image(ref, pins) for service, ref in refs.items()
            if service != "postgres" or not with_postgres}


def make_plan(workspace, sources, pins, registry, release, *, with_postgres=False):
    plan_inputs(sources, pins, registry, release)
    dockerfiles = release_dockerfiles(registry, with_postgres)
    upstream = pinned_upstream(pins, with_postgres=with_postgres)
    repositories = {name: (workspace / name).resolve() for name in sources}
    records = {}
    for name, repo in sorted(repositories.items()):
        verify_ref(name, repo, sources[name])
        records[name] = metadata(repo, sources[name])

    images = {}
    for service, (name, path) in sorted(dockerfiles.items()):
        spec = git(repositories[name], "show", f"{sources[name]}:{path}").decode()
        pinned, bases = pin_dockerfile(spec, pins)
        images[service] = {"source": name, "dockerfile": path, "bases": bases}
        for stage_service, (repo, dockerfile, stage) in STAGE_IMAGES.items():
            if (repo, dockerfile) == (name, path) and any(
                line.startswith("FROM ") and line.endswith(f" AS {stage}")
                for line in pinned.splitlines()
            ):
                images[stage_service] = {**images[service], "target": stage}
    return {
        "version": 1, "release": release, "registry": registry, "platform": PLATFORM,
        "sources": sources, "pins": pins, "metadata": records,
        "upstream": upstream, "images": images,
    }


def validate_plan(plan):
    if not isinstance(plan, dict) or plan.get("version") != 1:
        raise ValueError("unsupported release plan")
    plan_inputs(plan["sources"], plan["pins"], plan["registry"], plan["release"])
    images = plan["images"]
    with_postgres = "postgres" in images
    dockerfiles = release_dockerfiles(plan["registry"], with_postgres)
    staged = {service: (repo, path, target) for service, (repo, path, target)
              in STAGE_IMAGES.items() if service in images}
    if plan["platform"] != PLATFORM or images.keys() != dockerfiles.keys() | staged.keys():
        raise ValueError("unexpected platform or image set in release plan")
    expected = {service: (repo, path, None) for service, (repo, path) in dockerfiles.items()}
    for service, (name, path, target) in (expected | staged).items():
        image = images[service]
        if (image["source"], image["dockerfile"], image.get("target")) != (name, path, target):
            raise ValueError(f"{service}: unexpected image source")
    if plan["upstream"] != pinned_upstream(plan["pins"], with_postgres=with_postgres):
        raise ValueError("upstream images do not match the pins")


def labels_for(plan, service, plan_id):
    name = plan["images"][service]["source"]
    labels = {
        "org.opencontainers.image.source": remote_source(name),
        "org.opencontainers.image.revision": plan["sources"][name],
        "io.overmindv.release.plan": plan_id,
        "io.overmindv.release.service": service,
    }
    if plan["registry"] == CANDIDATE_REGISTRY:
        # GHCR uses the OCI source label to inherit access from the linked repo.
        labels["io.overmindv.release.source"] = labels["org.opencontainers.image.source"]
        labels["org.opencontainers.image.source"] = remote_source("docs")
    return labels


def inspect(image, labels):
    result = subprocess.check_output(["docker", "image", "inspect", image])
    data, = json.loads(result)
    actual = data["Config"].get("Labels") or {}
    if any(actual.get(key) != value for key, value in labels.items()):
        raise ValueError(f"{image}: local image does not match the release plan")
    if f'{data["Os"]}/{data["Architecture"]}' != PLATFORM:
        raise ValueError(f"{image}: unexpected image platform")
    image_id = data["Id"]
    if not DIGEST.fullmatch(image_id):
        raise ValueError(f"{image}: invalid local image ID")
    return image_id


def verify_source_plan(workspace, plan):
    validate_plan(plan)
    if plan != make_plan(workspace, plan["sources"], plan["pins"],
                         plan["registry"], plan["release"], with_postgres="postgres" in plan["images"]):
        raise ValueError("plan does not match the pinned source trees")


def build(workspace, plan):
    verify_source_plan(workspace, plan)
    plan_id = fingerprint(plan)
    built = {}
    for name in sorted({image["source"] for image in plan["images"].values()}):
        with tempfile.TemporaryDirectory(prefix="overmindv-release-") as payload:
            context = Path(payload)
            archive(workspace / name, plan["sources"][name], context)
            pinned_paths = set()
            for service, image in sorted(plan["images"].items()):
                if name != image["source"]:
                    continue
                path = image["dockerfile"]
                dockerfile = context / path
                if path not in pinned_paths:
                    spec = git(workspace / name, "show", f'{plan["sources"][name]}:{path}')
                    if dockerfile.read_bytes() != spec:
                        raise ValueError(f"{service}: archive changed the Dockerfile")
                    pinned, _ = pin_dockerfile(spec.decode(), plan["pins"])
                    dockerfile.write_bytes(pinned.encode())
                    pinned_paths.add(path)
                labels = labels_for(plan, service, plan_id)
                local = f"overmindv-release/{service}:{uuid.uuid4().hex}"
                command = [
                    "docker", "buildx", "build", "--load", "--platform", PLATFORM,
                    "--tag", local, "--file", str(dockerfile),
                ]
                if target := image.get("target"):
                    command.extend(("--target", target))
                for key, value in labels.items():
                    command.extend(("--label", f"{key}={value}"))
                subprocess.run([*command, str(context)], check=True)
                built[service] = inspect(local, labels)
    return {"plan": plan, "local_images": built}


def publish(receipt):
    plan, images = receipt["plan"], receipt["local_images"]
    validate_plan(plan)
    if images.keys() != plan["images"].keys():
        raise ValueError("build receipt must contain every release image")
    plan_id = fingerprint(plan)
    for service, image_id in images.items():
        if not isinstance(image_id, str) or not DIGEST.fullmatch(image_id):
            raise ValueError(f"{service}: invalid local image ID")
        if inspect(image_id, labels_for(plan, service, plan_id)) != image_id:
            raise ValueError(f"{service}: local image does not match build receipt")

    published = {}
    for service, image_id in sorted(images.items()):
        repository = f'{plan["registry"]}/{service}'
        tag = f'rel-{plan["release"]}-{image_id.removeprefix("sha256:")}'
        ref = f"{repository}:{tag}"
        subprocess.run(["docker", "image", "tag", image_id, ref], check=True)
        output = subprocess.check_output(["docker", "image", "push", ref], text=True)
        match = re.search(rf"^{re.escape(tag)}: digest: (sha256:[a-f0-9]{{64}}) size: \d+$",
                          output, re.M)
        if not match:
            raise ValueError(f"{service}: registry manifest digest not found in push output")
        published[service] = f"{repository}@{match[1]}"
        print(f"{service}: {published[service]}", flush=True)
    return {"plan": plan, "images": published}


def main():
    parser = argparse.ArgumentParser(description="Build and publish an immutable release")
    commands = parser.add_subparsers(dest="command", required=True)
    for name in ("plan", "build", "publish"):
        command = commands.add_parser(name)
        command.add_argument("--output", type=Path, required=True)
        if name != "publish":
            command.add_argument("--workspace", type=Path, required=True)
        if name == "plan":
            command.add_argument("--sources", type=Path, required=True)
            command.add_argument("--pins", type=Path, required=True)
            command.add_argument("--registry", required=True)
            command.add_argument("--release", required=True)
            command.add_argument("--with-postgres", action="store_true",
                                 help="build Postgres from the pinned Infra commit for a private candidate")
        elif name == "build":
            command.add_argument("--plan", type=Path, required=True)
        else:
            command.add_argument("--build", type=Path, required=True)
    args = parser.parse_args()
    try:
        if args.output.exists() or not args.output.parent.is_dir():
            raise ValueError("output must be a new file in an existing directory")
        if args.command == "plan":
            result = make_plan(args.workspace.resolve(), read_json(args.sources),
                               read_json(args.pins), args.registry, args.release,
                               with_postgres=args.with_postgres)
        elif args.command == "build":
            result = build(args.workspace.resolve(), read_json(args.plan))
        else:
            result = publish(read_json(args.build))
        write_json(args.output, result)
    except (OSError, ValueError, TypeError, KeyError, AttributeError,
            subprocess.CalledProcessError) as error:
        parser.exit(2, f"release: {error}\n")


if __name__ == "__main__":
    main()
