import hashlib
import json
import os
import re
import subprocess
import tarfile
import tempfile
from pathlib import Path


DOCKERFILES = {
    name: (name, "Dockerfile")
    for name in (
        "api-gateway", "content", "entities", "feed", "frontend", "media",
        "recommendations", "task-hunter", "tasks", "users",
    )
}
DOCKERFILES.update({
    "sandbox": ("sandbox", "deployments/docker/Dockerfile.worker"),
    "gojudge": ("sandbox", "deployments/docker/Dockerfile.gojudge-runtime"),
})
STAGE_IMAGES = {"media-renderer": ("media", "Dockerfile", "renderer")}
REPOS = {"infra", *(repo for repo, _ in DOCKERFILES.values())}
UPSTREAM = {
    "kafka": "apache/kafka:4.3.1",
    "postgres": "postgres:17-alpine",
    "minio": "cgr.dev/chainguard/minio:latest",
    "clamav": "clamav/clamav:1.4_base",
}
SHA = re.compile(r"[a-f0-9]{40}")
DIGEST = re.compile(r"sha256:[a-f0-9]{64}")
COMPONENT = r"[a-z0-9]+(?:[._-][a-z0-9]+)*"
IMAGE = re.compile(rf"{COMPONENT}(?::[0-9]+)?(?:/{COMPONENT})*")
TAG = re.compile(r"[\w][\w.-]{0,127}", re.ASCII)


def git(repo, *args):
    return subprocess.check_output(["git", "--no-replace-objects", "-C", str(repo), *args])


def remote_source(name):
    return f"https://github.com/overmindv/{name}"


def verify_ref(name, repo, sha):
    urls = {
        remote_source(name),
        f"git@github.com:overmindv/{name}",
        f"ssh://git@github.com/overmindv/{name}",
    }
    origin = git(repo, "config", "--get", "remote.origin.url").decode().strip()
    if origin.lower().removesuffix(".git") not in urls:
        raise ValueError(f"{name}: origin must point to overmindv/{name}")
    if Path(git(repo, "rev-parse", "--show-toplevel").decode().strip()).resolve() != repo:
        raise ValueError(f"{name}: not a repository root")
    if git(repo, "cat-file", "-t", sha).strip() != b"commit":
        raise ValueError(f"{name}: SHA must identify a commit")
    if not git(repo, "for-each-ref", "--count=1", "--contains", sha,
               "refs/remotes/origin/").strip():
        raise ValueError(f"{name}: {sha} is not in a fetched origin branch")
    check_archive_attributes(repo)


def check_archive_attributes(repo):
    attributes = git(repo, "rev-parse", "--git-path", "info/attributes").decode().strip()
    if (repo / attributes).exists():
        raise ValueError(f"{repo.name}: local info/attributes may change the archive")


def metadata(repo, sha):
    migrations, schemas = [], []
    for entry in git(repo, "ls-tree", "-rz", sha).split(b"\0"):
        if not entry:
            continue
        header, raw_path = entry.split(b"\t", 1)
        mode, _, oid = header.split()
        path = raw_path.decode()
        if mode == b"160000":
            raise ValueError(f"{repo.name}: submodules are not supported ({path})")
        if mode == b"120000":
            raise ValueError(f"{repo.name}: symlinks are not supported ({path})")
        if path.rsplit("/", 1)[-1] == ".gitattributes":
            attributes = git(repo, "cat-file", "blob", oid.decode())
            if b"export-subst" in attributes:
                raise ValueError(f"{repo.name}: export-subst may depend on local refs ({path})")
            if re.search(rb"(?<!\S)filter=", attributes):
                raise ValueError(f"{repo.name}: Git filters may change the archive ({path})")
        migration = path.startswith("migrations/") and path.endswith(".sql")
        schema = (path.startswith("api/") and path.endswith(".json")
                  or path.endswith((".graphql", ".graphqls", ".proto")))
        if not migration and not schema:
            continue
        content = git(repo, "cat-file", "blob", oid.decode())
        record = {"path": path, "sha256": hashlib.sha256(content).hexdigest()}
        if migration:
            version = re.match(r"[0-9]+", path.rsplit("/", 1)[-1])
            if not version:
                raise ValueError(f"{repo.name}: missing migration version ({path})")
            migrations.append({**record, "version": version[0]})
        else:
            if path.endswith(".json"):
                spec = json.loads(content)
                if isinstance(spec, dict):
                    if isinstance(spec.get("$id"), str):
                        record["schema_id"] = spec["$id"]
                    properties = spec.get("properties")
                    field = properties.get("schema_version") if isinstance(properties, dict) else None
                    if isinstance(field, dict) and type(field.get("const")) in (int, str):
                        record["version"] = field["const"]
            schemas.append(record)
    return {"migrations": migrations, "schemas": schemas}


def archive(repo, sha, destination):
    check_archive_attributes(repo)
    with tempfile.TemporaryFile() as tar:
        subprocess.run(
            ["git", "--no-replace-objects", "-c", "core.attributesFile=",
             "-c", "core.autocrlf=false", "-c", "core.eol=lf",
             "-C", str(repo), "archive", sha],
            stdout=tar, check=True, env={**os.environ, "GIT_ATTR_NOSYSTEM": "1"},
        )
        tar.seek(0)
        with tarfile.open(fileobj=tar) as source:
            source.extractall(destination, filter="data")


def image_name(ref):
    without_tag, sep, tag = ref.rpartition(":")
    name = without_tag if sep and "/" not in tag else ref
    if not IMAGE.fullmatch(name) or (name != ref and not TAG.fullmatch(tag)):
        raise ValueError(f"invalid image reference: {ref}")
    return name


def pinned_image(ref, pins):
    name, sep, digest = ref.partition("@")
    if not sep:
        digest = pins.get(ref)
    if not isinstance(digest, str) or not DIGEST.fullmatch(digest):
        raise ValueError(f"missing or invalid digest for {ref}")
    return f"{image_name(name)}@{digest}"


def pin_dockerfile(text, pins):
    stages, bases, defaults = set(), {}, {}
    result, pending = [], []
    variables = re.compile(r"\${([A-Za-z_][A-Za-z0-9_]*)}|\$([A-Za-z_][A-Za-z0-9_]*)")
    seen_from = False
    for line in text.splitlines(keepends=True):
        stripped = line.strip()
        if stripped.startswith("#") or not stripped:
            if re.match(r"#\s*(syntax|escape)\s*=", stripped, re.I):
                raise ValueError("custom Dockerfile syntax/escape is not supported")
            (pending if pending else result).append(line)
            continue
        pending.append(line)
        if stripped.endswith("\\"):
            continue
        logical = "".join(
            part.rstrip().removesuffix("\\") for part in pending
            if part.strip() and not part.lstrip().startswith("#")
        ).strip()
        op, _, args = re.sub(r"\s+", " ", logical).partition(" ")
        op = op.upper()
        if "<<" in args or op in {"ADD", "ONBUILD"}:
            raise ValueError("Dockerfile ADD, ONBUILD and heredocs are not supported")
        if op == "ARG" and not seen_from:
            name, sep, value = args.partition("=")
            if sep:
                defaults[name] = value
        if op == "FROM":
            seen_from = True
            match = re.fullmatch(r"(\S+)(?: AS ([A-Za-z0-9_.-]+))?", args, re.I)
            if not match:
                raise ValueError("expected FROM <image> [AS <stage>]")
            ref, stage = match.groups()
            ref = variables.sub(lambda m: defaults.get(m[1] or m[2], m[0]), ref)
            if ref.lower() in stages or ref == "scratch":
                pinned = ref
            else:
                pinned = bases[ref] = pinned_image(ref, pins)
            pending = [f"FROM {pinned}" + (f" AS {stage}" if stage else "") + "\n"]
            if stage:
                stages.add(stage.lower())
        if op in {"COPY", "RUN"}:
            flags = []
            for word in args.split():
                if not word.startswith("--"):
                    break
                flags.append(word)
            for flag in flags:
                if flag.startswith("--mount") and not re.fullmatch(
                    r"--mount=type=cache(?:,[a-z-]+=[A-Za-z0-9_./-]+)*", flag
                ):
                    raise ValueError("only plain cache mounts are supported")
                if flag.startswith("--mount") and ",from=" in flag:
                    raise ValueError("external Dockerfile mount is not supported")
                if not flag.startswith("--from="):
                    continue
                name = flag.removeprefix("--from=").lower()
                if name not in stages and not name.isdecimal():
                    raise ValueError(f"external Dockerfile stage is not supported: {name}")
        result.extend(pending)
        pending = []
    if not seen_from or pending:
        raise ValueError("incomplete Dockerfile")
    return "".join(result), bases
