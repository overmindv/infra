#!/usr/bin/env python3
import argparse
import hashlib
import ipaddress
import json
import os
import re
import stat
import subprocess
from pathlib import Path
from urllib.parse import urlsplit


ROOT = Path(__file__).resolve().parents[1]
IMAGE_REF = re.compile(r"[a-z0-9][a-z0-9._:/-]*@sha256:[0-9a-f]{64}")
DNS_LABEL = r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?"
PUBLIC_ORIGIN = re.compile(rf"https://(?:{DNS_LABEL}\.)+{DNS_LABEL}")
PRIVATE_NETWORKS = tuple(map(ipaddress.IPv4Network, ("10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16")))
APPS = (
    "api-gateway", "content", "entities", "feed", "frontend", "media",
    "recommendations", "sandbox", "task-hunter", "tasks", "users",
)
DATABASES = tuple(name for name in APPS if name not in ("api-gateway", "frontend"))
APP_IMAGES = {
    **{name: name for name in APPS},
    **{f"{name}-postgres": "postgres" for name in DATABASES},
    **{f"{name}-grants": "postgres" for name in DATABASES},
    **{f"{name}-migrate": name for name in DATABASES if name != "sandbox"},
    "sandbox-migrate": "postgres",
    "users-no-demo": "postgres",
    "kafka": "kafka",
    "kafka-topics": "kafka",
    "minio": "minio",
    "media-buckets": "minio",
    "clamav": "clamav",
}
PROJECTS = {"app": "overmindv-production", "runner": "overmindv-runner"}
PORTS = {
    "app": {"frontend": (13000, 3000), "api-gateway": (18081, 8081), "minio": (19000, 9000)},
    "runner": {"gojudge": (15050, 5050)},
}
MIGRATION = "migrations/001_init_runs.sql"
POSTGRES_INIT = (ROOT / "deploy/postgres/init-roles.sql", "/docker-entrypoint-initdb.d/20-roles.sql")
POSTGRES_GRANTS = (ROOT / "deploy/postgres/runtime-grants.sql", "/app/runtime-grants.sql")
RUNNER_TLS_PATH = "/run/sandbox/gojudge-tls"
RUNNER_TLS_FILES = {
    "SANDBOX_GOJUDGE_TLS_CA_FILE": "runner-ca.pem",
    "SANDBOX_GOJUDGE_TLS_CERT_FILE": "worker.pem",
    "SANDBOX_GOJUDGE_TLS_KEY_FILE": "worker-key.pem",
}


def check_private_env(path):
    if os.name != "posix":
        raise ValueError("production secrets require a Linux application host")
    if path.is_symlink():
        raise ValueError("app env file must not be a symlink")
    resolved = path.resolve(strict=True)
    metadata = resolved.lstat()
    if (not stat.S_ISREG(metadata.st_mode) or metadata.st_uid != 0
            or stat.S_IMODE(metadata.st_mode) != 0o600):
        raise ValueError("app env file must be a regular root-owned file with mode 0600")
    if resolved.is_relative_to(ROOT):
        raise ValueError("app env file must be outside the repository")
    for directory in resolved.parents:
        metadata = directory.stat()
        if metadata.st_uid != 0 or metadata.st_mode & 0o022:
            raise ValueError("app env parent directories must be root-owned and not writable by group or others")
    return resolved


def image_env(manifest, target):
    plan = manifest["plan"]
    if plan["version"] != 1 or plan["platform"] != "linux/amd64":
        raise ValueError("expected a version 1 linux/amd64 release")
    refs = {
        name: manifest["images"][name]
        for name in (APPS if target == "app" else ("gojudge",))
    }
    if target == "app":
        refs.update({name: plan["upstream"][name] for name in ("kafka", "postgres", "minio", "clamav")})
    for name, ref in refs.items():
        if not isinstance(ref, str) or not IMAGE_REF.fullmatch(ref):
            raise ValueError(f"{name}: expected an image pinned by manifest digest")
    return refs


def check_migration(manifest, path):
    migrations = manifest["plan"]["metadata"]["sandbox"]["migrations"]
    if len(migrations) != 1 or migrations[0]["path"] != MIGRATION:
        raise ValueError("unsupported Sandbox migration set")
    if hashlib.sha256(path.read_bytes()).hexdigest() != migrations[0]["sha256"]:
        raise ValueError("Sandbox migration does not match the release checksum")


def check_runner_tls_files(directory, production=False):
    if {file.name for file in directory.iterdir()} != set(RUNNER_TLS_FILES.values()):
        raise ValueError("worker TLS directory must contain only runner-ca.pem, worker.pem and worker-key.pem")
    if production and os.name != "posix":
        raise ValueError("apply production TLS permissions on the Linux application VM")
    if production:
        metadata = directory.stat()
        if metadata.st_uid != 0 or metadata.st_gid != 10001 or stat.S_IMODE(metadata.st_mode) != 0o750:
            raise ValueError("worker TLS directory must be root:10001 with mode 0750")
    for name in RUNNER_TLS_FILES.values():
        file = directory / name
        metadata = file.lstat()
        if not stat.S_ISREG(metadata.st_mode) or not metadata.st_size:
            raise ValueError(f"{name}: expected a nonempty regular PEM file")
        if production and (metadata.st_uid != 0 or metadata.st_gid != 10001
                           or stat.S_IMODE(metadata.st_mode) != 0o640):
            raise ValueError(f"{name}: expected root:10001 with mode 0640")


def https_url(value, origin_only=False):
    url = urlsplit(value)
    return (
        url.scheme == "https" and url.hostname and not url.username and not url.password
        and not url.query and not url.fragment and (not origin_only or not url.path)
    )


def check_public_edge(network, gateway, object_origin):
    config = network["ipam"]["config"]
    if len(config) != 1:
        raise ValueError("expected one production app bridge subnet")
    subnet = ipaddress.IPv4Network(config[0]["subnet"])
    address = ipaddress.IPv4Address(config[0]["gateway"])
    if subnet.prefixlen > 26 or not any(subnet.subnet_of(private) for private in PRIVATE_NETWORKS):
        raise ValueError("expected a private app bridge with at least 62 addresses")
    if address not in subnet or address in (subnet.network_address, subnet.broadcast_address):
        raise ValueError("bridge gateway is outside the usable subnet")
    if (gateway["APP_ENV"] != "production" or gateway["SESSION_COOKIE_SECURE"] != "true"
            or gateway["TRUSTED_PROXY_CIDRS"] != f"{address}/32"):
        raise ValueError("gateway requires Secure cookies and the exact host proxy address")
    web_origin = gateway["CORS_ORIGINS"]
    if (not PUBLIC_ORIGIN.fullmatch(web_origin) or not PUBLIC_ORIGIN.fullmatch(object_origin)
            or any(origin.endswith((".localhost", ".local", ".internal", ".invalid", ".test", ".example"))
                   for origin in (web_origin, object_origin))
            or web_origin == object_origin):
        raise ValueError("expected one HTTPS frontend origin distinct from object storage")


def must_wait_for(service, job):
    dependency = service.get("depends_on", {}).get(job, {})
    return dependency.get("condition") == "service_completed_successfully" and dependency.get("required", True)


def check_database_roles(services):
    used_passwords = set()
    for name in DATABASES:
        db = name.replace("-", "_")
        host = f"{name}-postgres"
        init = services[host]["environment"]
        admin, migrator, app = (init[key] for key in (
            "POSTGRES_PASSWORD", "OVM_DB_MIGRATOR_PASSWORD", "OVM_DB_APP_PASSWORD",
        ))
        candidates = (admin, migrator, app)
        if any(not isinstance(value, str) or not re.fullmatch(r"[a-f0-9]{32,128}", value)
               for value in candidates):
            raise ValueError(f"{host}: use random hex passwords for the three database roles")
        if len(set(candidates)) != 3 or used_passwords.intersection(candidates):
            raise ValueError(f"{host}: database credentials must not be reused")
        used_passwords.update(candidates)
        if (init["POSTGRES_USER"], init["POSTGRES_DB"], init["POSTGRES_INITDB_ARGS"]) != (
            "postgres", db, "--auth-local=peer --auth-host=scram-sha-256",
        ):
            raise ValueError(f"{host}: unexpected database bootstrap settings")
        data_mounts = [volume for volume in services[host]["volumes"] if volume["type"] == "volume"]
        if len(data_mounts) != 1 or (
            data_mounts[0]["source"], data_mounts[0]["target"]
        ) != (host, "/var/lib/postgresql/data"):
            raise ValueError(f"{host}: unexpected database data volume")

        migrator_name = f"{name}-migrate"
        grants = f"{name}-grants"
        pg_migrator = {"PGUSER": "overmind_migrator", "PGHOST": host, "PGDATABASE": db, "PGPASSWORD": migrator}
        job = services[migrator_name]["environment"]
        valid_migration = (
            all(job.get(key) == value for key, value in pg_migrator.items()) if name == "sandbox"
            else job.get("DATABASE_URL") == f"postgres://overmind_migrator:{migrator}@{host}:5432/{db}?sslmode=disable"
        )
        if not valid_migration or any(
            services[grants]["environment"].get(key) != value for key, value in pg_migrator.items()
        ):
            raise ValueError(f"{name}: migration and grants must use the migration role")
        app_var = "SANDBOX_POSTGRES_DSN" if name == "sandbox" else "DATABASE_URL"
        if services[name]["environment"].get(app_var) != (
            f"postgres://overmind_app:{app}@{host}:5432/{db}?sslmode=disable"
        ):
            raise ValueError(f"{name}: app must use the runtime database role")
        if not must_wait_for(services[grants], migrator_name) or not must_wait_for(services[name], grants):
            raise ValueError(f"{name}: migrations and grants must finish before runtime starts")
    gate = services["users-no-demo"]
    if not must_wait_for(gate, "users-grants") or any(
        gate["environment"].get(key) != value for key, value in {
            "PGUSER": "overmind_app", "PGHOST": "users-postgres", "PGDATABASE": "users",
            "PGPASSWORD": services["users-postgres"]["environment"]["OVM_DB_APP_PASSWORD"],
        }.items()
    ):
        raise ValueError("users: the demo account check must use the runtime role after grants")


def check_model(model, target, images, migration=None, runner_tls=None):
    project = PROJECTS[target]
    services = model["services"]
    expected = APP_IMAGES if target == "app" else {"gojudge": "gojudge"}
    if model["name"] != project or services.keys() != expected.keys():
        raise ValueError(f"{target}: unexpected project or service set")
    network = model["networks"]["default"]
    if (set(model["networks"]) != {"default"} or network["name"] != f"{project}_default"
            or network.get("external") or network.get("enable_ipv6") or network["driver"] != "bridge"
            or network["driver_opts"] != {"com.docker.network.bridge.gateway_mode_ipv4": "nat"}):
        raise ValueError(f"{target}: expected a separate bridge network")
    for name, volume in model.get("volumes", {}).items():
        if (volume["name"] != f"{project}_{name}" or volume.get("external") or volume.get("driver_opts")):
            raise ValueError(f"{name}: data volume must belong to the production project")

    mounts = {"sandbox-migrate": (migration, "/app/001_init_runs.sql"),
              "sandbox": (runner_tls, RUNNER_TLS_PATH),
              **{f"{name}-postgres": POSTGRES_INIT for name in DATABASES},
              **{f"{name}-grants": POSTGRES_GRANTS for name in DATABASES}}
    for name, service in services.items():
        if service["image"] != images[expected[name]] or service["platform"] != "linux/amd64":
            raise ValueError(f"{name}: image does not match the release")
        if any(service.get(key) for key in ("build", "profiles", "network_mode", "pid", "ipc",
                                            "devices", "volumes_from", "cap_add")):
            raise ValueError(f"{name}: local build, profile, or host access is enabled")
        if set(service["networks"]) != {"default"} or bool(service.get("privileged")) != (target == "runner"):
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
            if any(service.get(key) for key in ("environment", "env_file", "volumes", "configs", "secrets")):
                raise ValueError("runner: data and secrets must stay on the application host")
            continue

        if "no-new-privileges:true" not in service["security_opt"]:
            raise ValueError(f"{name}: no-new-privileges is missing")
        files = [volume for volume in service.get("volumes", []) if volume["type"] != "volume"]
        expected_mount = mounts.get(name)
        if len(files) != int(expected_mount is not None) or any(
            volume["type"] != "bind" or not volume.get("read_only") or volume["bind"].get("create_host_path")
            or (Path(volume["source"]).resolve(), volume["target"]) != expected_mount for volume in files
        ):
            raise ValueError(f"{name}: unexpected host mount")
        if any("__GENERATE__" in str(value) for value in service.get("environment", {}).values()):
            raise ValueError(f"{name}: local secret placeholder in the configuration")

    if target == "runner":
        if services["gojudge"]["command"] != ["-parallelism=1"]:
            raise ValueError("runner: expected one execution slot")
        return
    if not must_wait_for(services["users"], "users-no-demo"):
        raise ValueError("users: demo account check must succeed before startup")
    check_database_roles(services)
    email = services["users"]["environment"]["BOOTSTRAP_SUPERUSER_EMAIL"].lower()
    if "@" not in email or email.endswith((".local", ".invalid", ".test", ".example")):
        raise ValueError("users: expected an explicit administrator email")
    sandbox = services["sandbox"]["environment"]
    if services["sandbox"]["user"] != "10001:10001" or any(
        sandbox.get(key) != f"{RUNNER_TLS_PATH}/{file}" for key, file in RUNNER_TLS_FILES.items()
    ):
        raise ValueError("sandbox: expected worker TLS files on the private read-only mount")
    media = services["media"]["environment"]
    if not (https_url(sandbox["SANDBOX_GOJUDGE_BASE_URL"], origin_only=True)
            and https_url(media["MEDIA_S3_PUBLIC_ENDPOINT"], origin_only=True)
            and https_url(media["MEDIA_PUBLIC_BASE_URL"])):
        raise ValueError("expected HTTPS URLs for the runner, media and frontend")
    if urlsplit(sandbox["SANDBOX_GOJUDGE_BASE_URL"]).port != 8443:
        raise ValueError("runner: expected the private TLS proxy on port 8443")
    check_public_edge(network, services["api-gateway"]["environment"], media["MEDIA_S3_PUBLIC_ENDPOINT"])
    keys = {
        services["minio"]["environment"]["MINIO_ROOT_USER"],
        media["MEDIA_S3_ACCESS_KEY"],
        sandbox["SANDBOX_TESTS_ACCESS_KEY_ID"],
        sandbox["SANDBOX_ARTIFACTS_ACCESS_KEY_ID"],
    }
    if len(keys) != 4:
        raise ValueError("MinIO, media, tests and artifacts must use separate access keys")


def compose_args(target, env_file):
    args = ["docker", "compose", "--project-directory", str(ROOT)]
    if target == "app":
        args.extend(("--env-file", str(env_file), "-f", "docker-compose.yml", "-f", "deploy/compose.production.yml"))
    else:
        args.extend(("-f", "deploy/compose.runner.yml"))
    return args


def compose_env(images, migration=None, runner_tls=None):
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
    if runner_tls is not None:
        environment["RELEASE_RUNNER_TLS_DIR"] = str(runner_tls)
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
    parser.add_argument("--runner-tls-dir", type=Path)
    args = parser.parse_args()
    app_files = (args.env_file, args.sandbox_migration, args.runner_tls_dir)
    if args.target == "app" and not all(app_files):
        parser.error("app requires --env-file, --sandbox-migration and --runner-tls-dir")
    if args.target == "runner" and any(app_files):
        parser.error("runner does not accept application files")

    try:
        with args.manifest.open(encoding="utf-8") as file:
            manifest = json.load(file)
        images = image_env(manifest, args.target)
        migration = None
        runner_tls = None
        env_file = None
        if args.target == "app":
            env_file = (check_private_env(args.env_file) if args.action == "up"
                        else args.env_file.resolve(strict=True))
            migration = args.sandbox_migration.resolve(strict=True)
            check_migration(manifest, migration)
            runner_tls = args.runner_tls_dir.resolve(strict=True)
            check_runner_tls_files(runner_tls, production=args.action == "up")
        environment = compose_env(images, migration, runner_tls)
        compose = compose_args(args.target, env_file)
        check_model(check_compose(compose, environment), args.target, images, migration, runner_tls)
        if args.action == "up":
            check_host(args.target, environment)
            subprocess.run([*compose, "up", "-d", "--no-build"], check=True, cwd=ROOT, env=environment)
        print(f"{args.target}: production configuration accepted")
    except (OSError, ValueError, KeyError, TypeError, AttributeError, subprocess.SubprocessError) as error:
        parser.exit(1, f"production compose: {error}\n")


if __name__ == "__main__":
    main()
