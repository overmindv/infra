#!/usr/bin/env python3
import argparse
import hashlib
from ipaddress import ip_address
import json
import os
import re
import subprocess
from pathlib import Path
from urllib.parse import urlsplit


ROOT = Path(__file__).resolve().parents[1]
IMAGE_REF = re.compile(r"[a-z0-9][a-z0-9._:/-]*@sha256:[0-9a-f]{64}")
APPS = (
    "api-gateway", "content", "entities", "feed", "frontend", "media",
    "recommendations", "sandbox", "task-hunter", "tasks", "users",
)
DATABASES = tuple(name for name in APPS if name not in ("api-gateway", "frontend"))
APP_IMAGES = {
    **{name: name for name in APPS},
    **{f"{name}-postgres": "postgres" for name in DATABASES},
    **{f"{name}-migrate": name for name in DATABASES if name != "sandbox"},
    "sandbox-migrate": "postgres",
    "users-no-demo": "postgres",
    "kafka": "kafka",
    "kafka-topics": "kafka",
    "minio": "minio",
    "media-buckets": "minio",
    "media-renderer": "media-renderer",
    "clamav": "clamav",
}
PROJECTS = {"app": "overmindv-production", "runner": "overmindv-runner"}
RUNNER_IMAGES = {"gojudge": "gojudge", "gojudge-compiler": "gojudge"}
RUNNER_COMMON_ARGS = ["-file-timeout=2m", "-no-fallback"]
RUNNER_ARGS = {
    "gojudge": ["-parallelism=1", *RUNNER_COMMON_ARGS],
    "gojudge-compiler": [
        "-parallelism=1", "-container-cred-start=10000", "-tmp-fs-param=size=128m,nr_inodes=4096",
        "-output-limit=33554432", "-copy-out-limit=1048576", *RUNNER_COMMON_ARGS,
    ],
}
PORTS = {
    "app": {"frontend": (13000, 3000), "minio": (19000, 9000)},
    "runner": {"gojudge": (15050, 5050), "gojudge-compiler": (15051, 5050)},
}
MIGRATION = "migrations/001_init_runs.sql"
MEDIA_POLICY = ROOT / "config/media/public-policy.json"
RENDERER_VOLUME = "media-renderer-socket"
RENDERER_MOUNT = "/run/media-renderer"
RENDERER_VOLUME_OPTIONS = {
    "type": "tmpfs",
    "device": "tmpfs",
    "o": "size=1m,mode=0750,uid=10002,gid=10003,nosuid,nodev,noexec",
}


def image_env(manifest, target):
    plan = manifest["plan"]
    if plan["version"] != 1 or plan["platform"] != "linux/amd64":
        raise ValueError("expected a version 1 linux/amd64 release")
    refs = {
        name: manifest["images"][name]
        for name in ((*APPS, "media-renderer") if target == "app" else ("gojudge",))
    }
    if target == "app":
        refs.update({name: plan["upstream"][name] for name in ("kafka", "postgres", "minio", "clamav")})
    for name, ref in refs.items():
        if not isinstance(ref, str) or not IMAGE_REF.fullmatch(ref):
            raise ValueError(f"{name}: expected an image pinned by manifest digest")
    return refs


def check_renderer(services):
    renderer = services["media-renderer"]
    if (renderer.get("user") != "10002:10003" or not renderer.get("read_only")
            or not renderer.get("init") or renderer.get("cap_drop") != ["ALL"]
            or float(renderer["cpus"]) != 1 or renderer["pids_limit"] != 32
            or int(renderer["mem_limit"]) != 384 << 20
            or renderer.get("memswap_limit") != renderer["mem_limit"]
            or renderer.get("tmpfs") != ["/tmp:rw,noexec,nosuid,nodev,size=96m,mode=0700,uid=10002,gid=10003"]
            or any(renderer.get(key) for key in ("environment", "env_file", "secrets", "configs", "extra_hosts"))):
        raise ValueError("media-renderer: expected a restricted runtime without credentials")
    for name, service in services.items():
        socket_mounts = [mount for mount in service.get("volumes", [])
                         if mount.get("source") == RENDERER_VOLUME]
        if name not in ("media", "media-renderer"):
            if socket_mounts:
                raise ValueError(f"{name}: renderer socket must not be shared")
            continue
        if len(socket_mounts) != 1:
            raise ValueError(f"{name}: expected the renderer socket volume")
        mount = socket_mounts[0]
        if (mount["type"] != "volume" or mount["target"] != RENDERER_MOUNT
                or bool(mount.get("read_only")) != (name == "media")
                or not mount.get("volume", {}).get("nocopy")
                or len(service["volumes"]) != 1):
            raise ValueError(f"{name}: unexpected renderer socket mount")
    if services["media"]["environment"].get("MEDIA_ALLOW_UNSCANNED_LOCAL") != "false":
        raise ValueError("media: scan bypass is not allowed in production")
    if services["media"].get("user") != "10001:10003":
        raise ValueError("media: expected the socket client UID and group")


def check_migration(manifest, path):
    migrations = manifest["plan"]["metadata"]["sandbox"]["migrations"]
    if len(migrations) != 1 or migrations[0]["path"] != MIGRATION:
        raise ValueError("unsupported Sandbox migration set")
    if hashlib.sha256(path.read_bytes()).hexdigest() != migrations[0]["sha256"]:
        raise ValueError("Sandbox migration does not match the release checksum")


def https_url(value, origin_only=False):
    url = urlsplit(value)
    return (
        url.scheme == "https" and url.hostname and not url.username and not url.password
        and not url.query and not url.fragment and (not origin_only or url.path in ("", "/"))
    )


def runner_origin(value):
    if not https_url(value, origin_only=True):
        raise ValueError("expected HTTPS endpoints for execution and compilation")
    parsed = urlsplit(value)
    host = parsed.hostname.rstrip(".")
    try:
        address = ip_address(host)
    except ValueError:
        if host == "localhost" or host.endswith(".localhost"):
            raise ValueError("runner endpoints must be on the separate host") from None
    else:
        address = getattr(address, "ipv4_mapped", None) or address
        if address.is_loopback or address.is_unspecified or address.is_link_local:
            raise ValueError("runner endpoints must be on the separate host")
        host = str(address)
    try:
        port = parsed.port
    except ValueError:
        raise ValueError("invalid runner endpoint port") from None
    return host, port if port is not None else 443


def check_model(model, target, images, migration=None):
    project = PROJECTS[target]
    services = model["services"]
    expected = APP_IMAGES if target == "app" else RUNNER_IMAGES
    if model["name"] != project or services.keys() != expected.keys():
        raise ValueError(f"{target}: unexpected project or service set")
    network = model["networks"]["default"]
    if (set(model["networks"]) != {"default"} or network["name"] != f"{project}_default"
            or network.get("external") or network.get("enable_ipv6") or network["driver"] != "bridge"
            or network["driver_opts"] != {"com.docker.network.bridge.gateway_mode_ipv4": "nat"}):
        raise ValueError(f"{target}: expected a separate bridge network")
    for name, volume in model.get("volumes", {}).items():
        expected_opts = RENDERER_VOLUME_OPTIONS if target == "app" and name == RENDERER_VOLUME else None
        if (volume["name"] != f"{project}_{name}" or volume.get("external")
                or volume.get("driver_opts") != expected_opts
                or (expected_opts and volume.get("driver") != "local")):
            raise ValueError(f"{name}: data volume must belong to the production project")

    allowed_mounts = {
        "sandbox-migrate": (migration, "/app/001_init_runs.sql"),
        "media-buckets": (MEDIA_POLICY, "/etc/media/public-policy.json"),
        "kafka-topics": (ROOT / "scripts/kafka", "/opt/overmind/kafka"),
    }
    for name, service in services.items():
        if service["image"] != images[expected[name]] or service["platform"] != "linux/amd64":
            raise ValueError(f"{name}: image does not match the release")
        if any(service.get(key) for key in ("build", "profiles", "pid", "ipc",
                                            "devices", "volumes_from", "cap_add")):
            raise ValueError(f"{name}: local build, profile, or host access is enabled")
        offline = name == "media-renderer"
        if (set(service.get("networks", {})) != (set() if offline else {"default"})
                or service.get("network_mode") != ("none" if offline else None)
                or bool(service.get("privileged")) != (target == "runner")):
            raise ValueError(f"{name}: unexpected network or privileges")
        if (not service.get("restart")
                or any(float(service.get(key, 0)) <= 0 for key in ("mem_limit", "cpus", "pids_limit"))):
            raise ValueError(f"{name}: resource limits or restart policy are missing")
        logging = service["logging"]
        if logging["driver"] != "json-file" or logging["options"] != {"max-size": "10m", "max-file": "3"}:
            raise ValueError(f"{name}: log rotation is missing")
        ports = service.get("ports", [])
        binding = PORTS[target].get(name)
        if len(ports) != int(binding is not None) or (
            binding and any(port.get("host_ip") != "127.0.0.1" or port.get("protocol") != "tcp"
                            or (int(port["published"]), port["target"]) != binding for port in ports)
        ):
            raise ValueError(f"{name}: unexpected published port")

        if target == "runner":
            if any(service.get(key) for key in ("environment", "env_file", "volumes", "configs", "secrets", "extra_hosts")):
                raise ValueError("runner: data and secrets must stay on the application host")
            expected_tmpfs = ["/tmp:size=128m" if name == "gojudge" else "/tmp:size=256m", "/w:size=128m"]
            if (not service.get("read_only") or service.get("tmpfs") != expected_tmpfs
                    or service["command"] != RUNNER_ARGS[name]
                    or int(service["mem_limit"]) != 1536 << 20
                    or service.get("memswap_limit") != service["mem_limit"]):
                raise ValueError(f"{name}: runner limits or read-only filesystem changed")
            continue

        if "no-new-privileges:true" not in service["security_opt"]:
            raise ValueError(f"{name}: no-new-privileges is missing")
        for volume in service.get("volumes", []):
            if volume["type"] != "volume" and not (
                volume["type"] == "bind" and volume.get("read_only")
                and (Path(volume["source"]).resolve(), volume["target"]) == allowed_mounts.get(name)
                and not volume.get("bind", {}).get("create_host_path", True)
            ):
                raise ValueError(f"{name}: unexpected host mount")
        if any("__GENERATE__" in str(value) for value in service.get("environment", {}).values()):
            raise ValueError(f"{name}: local secret placeholder in the configuration")

    if target == "runner":
        return
    check_renderer(services)
    if set(services["sandbox"].get("depends_on", {})) != {"kafka-topics", "sandbox-migrate", "media-buckets"}:
        raise ValueError("sandbox: unexpected application dependencies")
    if services["users"]["depends_on"]["users-no-demo"]["condition"] != "service_completed_successfully":
        raise ValueError("users: demo account check must succeed before startup")
    email = services["users"]["environment"]["BOOTSTRAP_SUPERUSER_EMAIL"].lower()
    if "@" not in email or email.endswith((".local", ".invalid", ".test", ".example")):
        raise ValueError("users: expected an explicit administrator email")
    sandbox = services["sandbox"]["environment"]
    media = services["media"]["environment"]
    if runner_origin(sandbox["SANDBOX_GOJUDGE_BASE_URL"]) == runner_origin(sandbox["SANDBOX_GOJUDGE_COMPILER_URL"]):
        raise ValueError("execution and compilation require distinct runner endpoints")
    if not (https_url(media["MEDIA_S3_PUBLIC_ENDPOINT"], origin_only=True)
            and https_url(media["MEDIA_PUBLIC_BASE_URL"])
            and all(https_url(origin.strip(), origin_only=True)
                    for origin in services["api-gateway"]["environment"]["CORS_ORIGINS"].split(","))):
        raise ValueError("expected HTTPS URLs for media and frontend")
    keys = {
        services["minio"]["environment"]["MINIO_ROOT_USER"],
        media["MEDIA_S3_ACCESS_KEY"],
        sandbox["SANDBOX_TESTS_ACCESS_KEY_ID"],
        sandbox["SANDBOX_ARTIFACTS_ACCESS_KEY_ID"],
    }
    if len(keys) != 4:
        raise ValueError("MinIO, media, tests and artifacts must use separate access keys")
    for name in DATABASES:
        password = services[f"{name}-postgres"]["environment"]["POSTGRES_PASSWORD"]
        if not re.fullmatch(r"[a-f0-9]{32,}", password):
            raise ValueError(f"{name}-postgres: use a random hex password for the connection URL")


def compose_args(target, env_file):
    args = ["docker", "compose", "--project-directory", str(ROOT)]
    if target == "app":
        args.extend(("--env-file", str(env_file), "-f", "docker-compose.yml", "-f", "deploy/compose.production.yml"))
    else:
        args.extend(("-f", "deploy/compose.runner.yml"))
    return args


def compose_env(images, migration=None):
    environment = {
        key: value for key, value in os.environ.items()
        if key.upper().startswith("DOCKER_") or key.upper() in (
            "PATH", "PATHEXT", "HOME", "SYSTEMROOT", "USERPROFILE", "APPDATA", "LOCALAPPDATA",
            "PROGRAMFILES", "PROGRAMFILES(X86)", "PROGRAMW6432", "PROGRAMDATA",
            "TEMP", "TMP", "TMPDIR", "XDG_CONFIG_HOME", "XDG_RUNTIME_DIR", "SSH_AUTH_SOCK",
        )
    }
    environment["COMPOSE_DISABLE_ENV_FILE"] = "1"
    environment.update({f"RELEASE_{name.upper().replace('-', '_')}_IMAGE": value
                        for name, value in images.items()})
    if migration is not None:
        environment["RELEASE_SANDBOX_MIGRATION"] = str(migration)
    return environment


def check_compose(args, environment):
    result = subprocess.run([*args, "--profile", "*", "config", "--format", "json"], capture_output=True,
                            cwd=ROOT, env=environment, encoding="utf-8")
    if result.returncode:
        missing = re.search(r"required variable ([A-Za-z0-9_]+)", result.stderr)
        raise ValueError(f"missing {missing[1]}" if missing else "Docker Compose rejected the configuration")
    return json.loads(result.stdout)


def check_host(target, environment):
    engine = subprocess.check_output(["docker", "version", "--format", "{{.Server.Version}}"],
                                     env=environment, text=True).strip()
    major = engine.split(".", 1)[0]
    if not major.isdecimal() or int(major) < 28:
        raise ValueError("Docker Engine 28+ is required for loopback-only port publishing")
    peer = PROJECTS["runner" if target == "app" else "app"]
    resources = subprocess.check_output([
        "docker", "network", "ls", "--filter", f"label=com.docker.compose.project={peer}", "--quiet",
    ], env=environment, text=True)
    if resources.strip():
        raise ValueError(f"{peer} is on this Docker host; app and runner require separate VMs")


def main():
    parser = argparse.ArgumentParser(description="Validate or start a pinned Overmindv release")
    parser.add_argument("target", choices=PROJECTS)
    parser.add_argument("action", choices=("check", "up"))
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--env-file", type=Path)
    parser.add_argument("--sandbox-migration", type=Path)
    args = parser.parse_args()
    if args.target == "app" and (not args.env_file or not args.sandbox_migration):
        parser.error("app requires --env-file and --sandbox-migration")
    if args.target == "runner" and (args.env_file or args.sandbox_migration):
        parser.error("runner does not accept application files")

    try:
        with args.manifest.open(encoding="utf-8") as file:
            manifest = json.load(file)
        images = image_env(manifest, args.target)
        migration = None
        env_file = None
        if args.target == "app":
            env_file = args.env_file.resolve(strict=True)
            migration = args.sandbox_migration.resolve(strict=True)
            check_migration(manifest, migration)
        environment = compose_env(images, migration)
        compose = compose_args(args.target, env_file)
        check_model(check_compose(compose, environment), args.target, images, migration)
        if args.action == "up":
            check_host(args.target, environment)
            subprocess.run([*compose, "up", "-d", "--no-build"], check=True, cwd=ROOT, env=environment)
        print(f"{args.target}: production configuration accepted")
    except (OSError, ValueError, KeyError, TypeError, AttributeError, subprocess.SubprocessError) as error:
        parser.exit(1, f"production compose: {error}\n")


if __name__ == "__main__":
    main()
