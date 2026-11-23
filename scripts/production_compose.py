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
    "app": {"frontend": (13000, 3000), "api-gateway": (18081, 8081), "minio": (19000, 9000)},
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
POSTGRES_INIT = (ROOT / "deploy/postgres/init-roles.sql", "/docker-entrypoint-initdb.d/20-roles.sql")
POSTGRES_GRANTS = (ROOT / "deploy/postgres/runtime-grants.sql", "/app/runtime-grants.sql")
MINIO_POLICIES = (ROOT / "deploy/minio", "/app/minio")
MINIO_IDENTITIES = (
    ("media", "MEDIA_S3_ACCESS_KEY", "MEDIA_S3_SECRET_KEY", "ovm-media-"),
    ("sandbox", "SANDBOX_TESTS_ACCESS_KEY_ID", "SANDBOX_TESTS_SECRET_ACCESS_KEY", "ovm-tests-"),
    ("sandbox", "SANDBOX_ARTIFACTS_ACCESS_KEY_ID", "SANDBOX_ARTIFACTS_SECRET_ACCESS_KEY", "ovm-art-"),
)
SERVICE_TOKENS = {
    "JWT_SECRET": (("users", "JWT_SECRET"), ("api-gateway", "JWT_SECRET")),
    "TASK_HUNTER_INGEST_TOKEN": (("tasks", "TASK_HUNTER_INGEST_TOKEN"), ("task-hunter", "PARSER_TASKS_TOKEN")),
    "TASK_HUNTER_GATEWAY_TOKEN": (("task-hunter", "PARSER_SECURITY_GATEWAYTOKEN"), ("api-gateway", "TASK_HUNTER_TOKEN")),
    "FEED_GATEWAY_TOKEN": (("feed", "FEED_GATEWAY_TOKEN"), ("api-gateway", "FEED_TOKEN")),
    "MEDIA_GATEWAY_TOKEN": (("media", "MEDIA_GATEWAY_TOKEN"), ("api-gateway", "MEDIA_TOKEN")),
    "MEDIA_USERS_TOKEN": (("users", "MEDIA_USERS_TOKEN"), ("media", "MEDIA_USERS_TOKEN")),
    "RECOMMENDATIONS_SERVICE_TOKEN": (
        ("recommendations", "RECOMMENDATIONS_SERVICE_TOKEN"), ("api-gateway", "RECOMMENDATIONS_SERVICE_TOKEN"),
    ),
}
RUNNER_TLS_PATH = "/run/sandbox/gojudge-tls"
RUNNER_TLS_FILES = {
    "SANDBOX_GOJUDGE_TLS_CA_FILE": "runner-ca.pem",
    "SANDBOX_GOJUDGE_TLS_CERT_FILE": "worker.pem",
    "SANDBOX_GOJUDGE_TLS_KEY_FILE": "worker-key.pem",
}
CONTENT_TLS_PATH = "/run/content/tls"
CONTENT_TLS_GID = 10004
CONTENT_TLS_FILES = {"server.pem": 0o644, "server-ca.pem": 0o644, "server-key.pem": 0o640}
CATALOG_KAFKA_PATH = "/run/kafka"
CATALOG_KAFKA_FILES = {"broker.pem": (1000, 0o640), "ca.pem": (0, 0o644)}
CATALOG_KAFKA_CREDENTIALS = {
    "OVM_KAFKA_CATALOG_BROKER_PASSWORD": (),
    "OVM_KAFKA_CATALOG_ADMIN_PASSWORD": (("kafka-topics", "OVM_KAFKA_CATALOG_ADMIN_PASSWORD"),),
    "OVM_KAFKA_CATALOG_ENTITIES_PASSWORD": (("entities", "KAFKA_SASL_PASSWORD"),),
}
FEED_KAFKA_CREDENTIAL = "OVM_KAFKA_FEED_PASSWORD"
CONTENT_KAFKA_CREDENTIAL = "OVM_KAFKA_CONTENT_PASSWORD"
SANDBOX_KAFKA_PATH = f"{CATALOG_KAFKA_PATH}/worker"
SANDBOX_KAFKA_FILES = ("username", "password")
SANDBOX_KAFKA_SECURITY = {
    "SANDBOX_KAFKA_SECURITY_PROTOCOL": "SASL_SSL",
    "SANDBOX_KAFKA_TLS_CA_FILE": f"{CATALOG_KAFKA_PATH}/ca.pem",
    "SANDBOX_KAFKA_SASL_MECHANISM": "SCRAM-SHA-256",
    "SANDBOX_KAFKA_SASL_USERNAME_FILE": f"{SANDBOX_KAFKA_PATH}/username",
    "SANDBOX_KAFKA_SASL_PASSWORD_FILE": f"{SANDBOX_KAFKA_PATH}/password",
}
SANDBOX_SCRAM_ENV = {
    "KAFKA_LISTENER_NAME_SECURE_SASL_ENABLED_MECHANISMS": "PLAIN,SCRAM-SHA-256",
    "KAFKA_LISTENER_NAME_SECURE_SCRAM___SHA___256_SASL_JAAS_CONFIG": (
        "org.apache.kafka.common.security.scram.ScramLoginModule required;"
    ),
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
    media_env = services["media"]["environment"]
    if media_env.get("MEDIA_ALLOW_UNSCANNED_LOCAL") != "false":
        raise ValueError("media: scan bypass is not allowed in production")
    if media_env.get("MEDIA_ENABLE_UPLOAD_CLEANUP") != "false":
        raise ValueError("media: upload cleanup requires approved retention rules")
    limit = media_env.get("MEDIA_MAX_MULTIPART_SESSIONS", "")
    if not re.fullmatch(r"[1-9][0-9]{0,3}", limit) or int(limit) > 1000:
        raise ValueError("media: multipart session limit must be between 1 and 1000")
    if services["media"].get("user") != "10001:10003":
        raise ValueError("media: expected the socket client UID and group")


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


def check_content_tls_files(directory, production=False):
    if {file.name for file in directory.iterdir()} != CONTENT_TLS_FILES.keys():
        raise ValueError("Content TLS directory must contain server.pem, server-key.pem and server-ca.pem")
    if production:
        if os.name != "posix" or directory.is_relative_to(ROOT):
            raise ValueError("keep Content TLS files outside the repository on the Linux app host")
        for parent in (directory, *directory.parents):
            metadata = parent.stat()
            if metadata.st_uid != 0 or metadata.st_mode & 0o022:
                raise ValueError("Content TLS directories must be root-owned and not writable by group or others")
    for name, mode in CONTENT_TLS_FILES.items():
        metadata = (directory / name).lstat()
        if not stat.S_ISREG(metadata.st_mode) or not 0 < metadata.st_size <= 128 * 1024:
            raise ValueError(f"{name}: expected a regular Content TLS file of at most 128 KiB")
        group = CONTENT_TLS_GID if name == "server-key.pem" else 0
        if production and (metadata.st_uid != 0 or metadata.st_gid != group or stat.S_IMODE(metadata.st_mode) != mode):
            raise ValueError(f"{name}: wrong owner, group or permissions for Content TLS")


def https_url(value, origin_only=False):
    url = urlsplit(value)
    return (
        url.scheme == "https" and url.hostname and not url.username and not url.password
        and not url.query and not url.fragment and (not origin_only or not url.path)
    )


def check_catalog_kafka_files(directory, production=False):
    if {file.name for file in directory.iterdir()} != CATALOG_KAFKA_FILES.keys():
        raise ValueError("Kafka TLS directory must contain only broker.pem and ca.pem")
    if production:
        if os.name != "posix" or directory.is_relative_to(ROOT):
            raise ValueError("keep Kafka TLS files outside the repository on the Linux app host")
        for parent in (directory, *directory.parents):
            metadata = parent.stat()
            if metadata.st_uid != 0 or metadata.st_mode & 0o022:
                raise ValueError("Kafka TLS directories must be root-owned and not group or world writable")
    for name, (group, mode) in CATALOG_KAFKA_FILES.items():
        metadata = (directory / name).lstat()
        if not stat.S_ISREG(metadata.st_mode) or not 0 < metadata.st_size <= 128 * 1024:
            raise ValueError(f"{name}: expected a regular Kafka TLS file of at most 128 KiB")
        if production and (metadata.st_uid != 0 or metadata.st_gid != group or stat.S_IMODE(metadata.st_mode) != mode):
            raise ValueError(f"{name}: wrong owner, group or permissions for Kafka TLS")


def check_sandbox_kafka_files(directory, production=False):
    if {file.name for file in directory.iterdir()} != set(SANDBOX_KAFKA_FILES):
        raise ValueError("Sandbox Kafka directory must contain only username and password")
    if production:
        if os.name != "posix" or directory.is_relative_to(ROOT):
            raise ValueError("keep Sandbox Kafka files outside the repository on the Linux app host")
        if any(parent.stat().st_uid != 0 or parent.stat().st_mode & 0o022
               for parent in (directory, *directory.parents)):
            raise ValueError("Sandbox Kafka directories must be root-owned and not group or world writable")
    for name in SANDBOX_KAFKA_FILES:
        metadata = (directory / name).lstat()
        if not stat.S_ISREG(metadata.st_mode) or not 0 < metadata.st_size <= 65:
            raise ValueError(f"{name}: expected a small regular Sandbox Kafka file")
        if production and (metadata.st_uid != 0 or metadata.st_gid != 10001
                           or stat.S_IMODE(metadata.st_mode) != 0o640):
            raise ValueError(f"{name}: expected root:10001 with mode 0640 for Sandbox Kafka")
    if ((directory / "username").read_bytes() not in (b"sandbox", b"sandbox\n")
            or not re.fullmatch(rb"[a-fA-F0-9]{64}\n?", (directory / "password").read_bytes())):
        raise ValueError("Sandbox Kafka needs user sandbox and a 64-character hex password")


def runner_origin(value):
    if not https_url(value, origin_only=True):
        raise ValueError("expected HTTPS endpoints for execution and compilation")
    parsed = urlsplit(value)
    host = parsed.hostname.rstrip(".")
    try:
        address = ipaddress.ip_address(host)
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
    return used_passwords


def check_minio_credentials(services, used_secrets):
    root = services["minio"]["environment"]
    job = services["media-buckets"]
    bootstrap = job["environment"]
    if any(
        set(service.get("environment", {})) & {"MINIO_ROOT_USER", "MINIO_ROOT_PASSWORD"}
        for name, service in services.items() if name not in ("minio", "media-buckets")
    ):
        raise ValueError("MinIO root credentials must stay out of runtime services")
    if (job["entrypoint"] != ["/bin/sh", "/app/minio/init.sh"] or job.get("command")
            or not job.get("read_only") or not job.get("tmpfs")
            or bootstrap.get("OVM_MINIO_ENDPOINT", "http://minio:9000") != "http://minio:9000"):
        raise ValueError("expected private MinIO bootstrap with ephemeral client config")
    root_name = root["MINIO_ROOT_USER"]
    root_secret = root["MINIO_ROOT_PASSWORD"]
    if (not re.fullmatch(r"[a-z0-9][a-z0-9-]{2,19}", root_name)
            or any(root_name.startswith(prefix) for _, _, _, prefix in MINIO_IDENTITIES)
            or not re.fullmatch(r"[a-f0-9]{40}", root_secret)
            or root_secret in used_secrets
            or bootstrap["MINIO_ROOT_USER"] != root_name or bootstrap["MINIO_ROOT_PASSWORD"] != root_secret):
        raise ValueError("MinIO needs a separate root identity and a 40-digit random hex password")
    used_secrets.add(root_secret)
    for service, access_var, secret_var, prefix in MINIO_IDENTITIES:
        runtime = services[service]["environment"]
        access, secret = runtime[access_var], runtime[secret_var]
        if (not re.fullmatch(re.escape(prefix) + r"[0-9a-f]{8}", access)
                or not re.fullmatch(r"[a-f0-9]{40}", secret) or secret in used_secrets
                or bootstrap[access_var] != access or bootstrap[secret_var] != secret):
            raise ValueError(f"{access_var}: expected a dedicated MinIO identity and unique 40-digit hex secret")
        used_secrets.add(secret)
        if not must_wait_for(services[service], "media-buckets"):
            raise ValueError(f"{service}: must wait for MinIO policies")
    sandbox = services["sandbox"]["environment"]
    if any(sandbox.get(key) != value for key, value in {
        "SANDBOX_TESTS_BUCKET": "sandbox-tests",
        "SANDBOX_TESTS_KEY_PREFIX": "sandbox-tests/prod",
        "SANDBOX_ARTIFACTS_BUCKET": "sandbox-artifacts",
        "SANDBOX_ARTIFACTS_KEY_PREFIX": "sandbox-artifacts/prod",
    }.items()):
        raise ValueError("Sandbox object paths must match the MinIO policies")


def check_service_tokens(services, used_secrets):
    for name, peers in SERVICE_TOKENS.items():
        values = {services[service]["environment"].get(key) for service, key in peers}
        if len(values) != 1:
            raise ValueError(f"{name}: both services must use the same token")
        value, = values
        if not isinstance(value, str) or not re.fullmatch(r"[a-f0-9]{64}", value) or value in used_secrets:
            raise ValueError(f"{name}: expected a unique 64-digit random hex token")
        used_secrets.add(value)


def check_catalog_logo_connection(services, used_secrets):
    placement = {
        "ENTITIES_MEDIA_URL": "entities",
        "ENTITIES_MEDIA_TOKEN": "entities",
        "MEDIA_ENTITIES_TOKEN": "media",
    }
    if any(service.get("environment", {}).get(key)
           for name, service in services.items() for key, owner in placement.items() if name != owner):
        raise ValueError("catalog logo: unexpected Media binding credentials")
    entities = services["entities"]["environment"]
    origin = entities.get("ENTITIES_MEDIA_URL")
    token = entities.get("ENTITIES_MEDIA_TOKEN")
    media_token = services["media"]["environment"].get("MEDIA_ENTITIES_TOKEN")
    if not (origin or token or media_token):
        return
    if origin != "http://media:8080":
        raise ValueError("catalog logo: expected the internal Media endpoint")
    if (not isinstance(token, str) or not re.fullmatch(r"[a-f0-9]{64}", token)
            or token != media_token or token in used_secrets):
        raise ValueError("catalog logo: both services need the same unique 64-digit token")
    used_secrets.add(token)


def check_content_connection(services, enabled, used_secrets):
    gateway = services["api-gateway"]["environment"]
    content = services["content"]["environment"]
    binding_keys = {"CONTENT_MEDIA_URL", "CONTENT_MEDIA_TOKEN", "MEDIA_CONTENT_TOKEN"}
    if any(service.get("environment", {}).get(key) for service in services.values() for key in binding_keys):
        raise ValueError("content: local Media binding credentials cannot reach production")
    moderation_key = "CONTENT_GRPC_MODERATION_TOKEN"
    if any(service.get("environment", {}).get(moderation_key)
           for name, service in services.items() if name not in {"api-gateway", "content"}):
        raise ValueError("content: only Content and Gateway may receive the moderation token")
    gateway_moderation = gateway.get(moderation_key)
    content_moderation = content.get(moderation_key)
    gateway_keys = ("CONTENT_GRPC_ADDR", "CONTENT_GRPC_SERVICE_TOKEN", "CONTENT_GRPC_TLS_CA_FILE", "CONTENT_GRPC_INSECURE")
    if not enabled:
        if gateway_moderation or content_moderation or any(gateway.get(key) for key in gateway_keys):
            raise ValueError("gateway: Content connection requires its TLS overlay")
        return
    if any(gateway.get(key) != value for key, value in {
        "CONTENT_GRPC_ADDR": "content:9090",
        "CONTENT_GRPC_TLS_CA_FILE": f"{CONTENT_TLS_PATH}/server-ca.pem",
        "CONTENT_GRPC_INSECURE": "false",
    }.items()):
        raise ValueError("gateway: expected a verified Content TLS connection")
    if (content.get("CONTENT_GRPC_TLS_CERT_FILE") != f"{CONTENT_TLS_PATH}/server.pem"
            or content.get("CONTENT_GRPC_TLS_KEY_FILE") != f"{CONTENT_TLS_PATH}/server-key.pem"
            or services["content"].get("group_add") != [str(CONTENT_TLS_GID)]):
        raise ValueError("content: expected dedicated TLS certificate access")
    token = gateway.get("CONTENT_GRPC_SERVICE_TOKEN")
    if (not isinstance(token, str) or not re.fullmatch(r"[a-f0-9]{64}", token)
            or token != content.get("CONTENT_GRPC_SERVICE_TOKEN") or token in used_secrets):
        raise ValueError("content: both services need the same unique 64-digit random hex token")
    if not (gateway_moderation or content_moderation):
        return
    if (not isinstance(gateway_moderation, str) or not re.fullmatch(r"[a-f0-9]{64}", gateway_moderation)
            or gateway_moderation != content_moderation or gateway_moderation == token or gateway_moderation in used_secrets):
        raise ValueError("content: both services need the same unique 64-digit moderation token")
    if content.get("CONTENT_ACTIVITY_ENABLED") != "true" or content.get("KAFKA_ENABLED") != "true":
        raise ValueError("content: moderation requires Kafka material activity")


def check_material_activity(services):
    content = services["content"]["environment"]
    feed = services["feed"]["environment"]
    publisher = content.get("CONTENT_ACTIVITY_ENABLED", "false")
    editorial = content.get("CONTENT_CONTRIBUTION_ENABLED", "false")
    cards = feed.get("FEED_MATERIAL_CARDS_ENABLED", "false")
    if any(flag not in ("true", "false") for flag in (publisher, editorial, cards)):
        raise ValueError("material activity flags must be true or false")
    material_topic = content.get("KAFKA_CONTENT_MATERIAL_TOPIC")
    feed_topic = feed.get("KAFKA_CONTENT_MATERIAL_TOPIC")
    private_topics = (
        content.get("KAFKA_TOPIC"), content.get("KAFKA_CONTENT_LEARNING_TOPIC"),
        content.get("KAFKA_CONTENT_CONTRIBUTION_TOPIC"),
    )
    if feed_topic and (feed_topic != material_topic or feed_topic in private_topics):
        raise ValueError("Feed can subscribe only to the public Content material topic")
    publishing = publisher == "true" and content.get("KAFKA_ENABLED") == "true"
    if publisher == "true" and not publishing:
        raise ValueError("Content activity requires Kafka publishing")
    if cards == "true" and (not feed_topic or not publishing):
        raise ValueError("material cards require a matching subscription and Content publishing")
    if editorial != "true":
        return
    if not publishing:
        raise ValueError("editorial activity requires Content material publishing")
    topic_key = "KAFKA_CONTENT_CONTRIBUTION_TOPIC"
    editorial_topic = content.get(topic_key)
    plan = services.get("kafka-topics", {}).get("environment", {})
    other_topics = (material_topic, private_topics[0], private_topics[1],
                    feed.get("KAFKA_CATALOG_ACTIVITY_TOPIC"), feed.get("KAFKA_TASK_ACTIVITY_TOPIC"))
    if (not isinstance(editorial_topic, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,248}", editorial_topic)
            or editorial_topic != plan.get(topic_key)
            or any(isinstance(topic, str) and editorial_topic.replace(".", "_") == topic.replace(".", "_")
                   for topic in other_topics)):
        raise ValueError("editorial activity requires a separate topic in the Kafka plan")


def verified_kafka_client(environment, username):
    return all(environment.get(key) == value for key, value in {
        "KAFKA_BOOTSTRAP_SERVERS": "kafka:9094",
        "KAFKA_TLS_ENABLED": "true",
        "KAFKA_TLS_CA_FILE": f"{CATALOG_KAFKA_PATH}/ca.pem",
        "KAFKA_TLS_SERVER_NAME": "kafka",
        "KAFKA_SASL_MECHANISM": "PLAIN",
        "KAFKA_SASL_USERNAME": username,
    }.items())


def check_catalog_kafka(services, enabled, used_secrets, feed_kafka=None, content_kafka=None):
    envs = {name: service.get("environment", {}) for name, service in services.items()}
    broker = envs["kafka"]
    entities = envs["entities"]
    feed_tls = feed_kafka == "tls"
    credentials = CATALOG_KAFKA_CREDENTIALS.copy()
    if feed_tls:
        credentials[FEED_KAFKA_CREDENTIAL] = (("feed", "KAFKA_SASL_PASSWORD"),)
    if content_kafka == "tls":
        credentials[CONTENT_KAFKA_CREDENTIAL] = (("content", "KAFKA_SASL_PASSWORD"),)
    expected_keys = {(name, var) for key, peers in credentials.items() for name, var in (("kafka", key), *peers)}
    actual_keys = {
        (name, key) for name, env in envs.items() for key in env
        if (key in CATALOG_KAFKA_CREDENTIALS or key in {FEED_KAFKA_CREDENTIAL, CONTENT_KAFKA_CREDENTIAL}
            or (name in {"entities", "feed", "content"} and key == "KAFKA_SASL_PASSWORD"))
    }
    flags = {"OVM_KAFKA_FEED_TLS": feed_kafka, "OVM_KAFKA_CONTENT_TLS": content_kafka}
    mode_value = {"tls": "true", "rollback": "rollback"}
    expected_flags = {("kafka-topics", key, mode_value[mode]) for key, mode in flags.items() if mode}
    rollout_flags = {(name, key, env[key]) for name, env in envs.items() for key in flags if key in env}
    if actual_keys != (expected_keys if enabled else set()) or rollout_flags != expected_flags:
        raise ValueError("Kafka catalog credentials require the TLS overlay and must stay in their services")
    if not enabled:
        return
    entry = ["/bin/sh", "/opt/overmind/kafka/catalog-tls.sh"]
    job = services["kafka-topics"]
    if (services["kafka"]["entrypoint"] != [*entry, "broker"] or job["entrypoint"] != entry
            or job["command"] != ["provision"] or not job.get("read_only")
            or job.get("tmpfs") != ["/tmp:size=16m,mode=1777,nosuid,nodev,noexec"]
            or not must_wait_for(services["entities"], "kafka-topics")):
        raise ValueError("expected private Kafka provisioning before Entities startup")
    if any(broker.get(key) != value for key, value in {
        "KAFKA_LISTENER_SECURITY_PROTOCOL_MAP": (
            "CONTROLLER:SASL_PLAINTEXT,REPLICA:SASL_PLAINTEXT,INTERNAL:PLAINTEXT,SECURE:SASL_SSL"
        ),
        "KAFKA_LISTENERS": "CONTROLLER://127.0.0.1:9093,REPLICA://127.0.0.1:9095,INTERNAL://:9092,SECURE://:9094",
        "KAFKA_ADVERTISED_LISTENERS": "REPLICA://127.0.0.1:9095,INTERNAL://kafka:9092,SECURE://kafka:9094",
        "KAFKA_CONTROLLER_QUORUM_VOTERS": "1@127.0.0.1:9093",
        "KAFKA_INTER_BROKER_LISTENER_NAME": "REPLICA",
        "KAFKA_SSL_KEYSTORE_TYPE": "PEM",
        "KAFKA_SSL_KEYSTORE_LOCATION": f"{CATALOG_KAFKA_PATH}/broker.pem",
        "KAFKA_AUTHORIZER_CLASS_NAME": "org.apache.kafka.metadata.authorizer.StandardAuthorizer",
        "KAFKA_SUPER_USERS": "User:broker;User:catalog_admin",
        "KAFKA_ALLOW_EVERYONE_IF_NO_ACL_FOUND": "false",
        "KAFKA_AUTO_CREATE_TOPICS_ENABLE": "false",
    }.items()):
        raise ValueError("expected private Kafka listeners and default-deny ACLs")
    if not verified_kafka_client(entities, "entities") or any(
        envs[peer].get("KAFKA_CATALOG_ACTIVITY_TOPIC") != "catalog.activity.v1"
        for peer in ("entities", "kafka-topics", "feed")
    ):
        raise ValueError("Entities needs verified Kafka TLS and the shared catalog topic")
    if feed_tls:
        feed = envs["feed"]
        plan = envs["kafka-topics"]
        if (not verified_kafka_client(feed, "feed")
                or feed.get("FEED_CONSUMER_GROUP") != plan.get("FEED_CONSUMER_GROUP")
                or feed.get("KAFKA_TASK_ACTIVITY_TOPIC") != plan.get("KAFKA_TASK_ACTIVITY_TOPIC")
                or (feed.get("KAFKA_CONTENT_MATERIAL_TOPIC")
                    and feed["KAFKA_CONTENT_MATERIAL_TOPIC"] != plan.get("KAFKA_CONTENT_MATERIAL_TOPIC"))
                or not must_wait_for(services["feed"], "kafka-topics")):
            raise ValueError("Feed needs verified Kafka TLS and the consumer group and topics from the Kafka plan")
    content = envs["content"]
    if content_kafka == "tls":
        plan = envs["kafka-topics"]
        topic_keys = ("KAFKA_CONTENT_MATERIAL_TOPIC", "KAFKA_CONTENT_LEARNING_TOPIC", "KAFKA_CONTENT_CONTRIBUTION_TOPIC")
        if (not verified_kafka_client(content, "content")
                or "KAFKA_TLS_CERT_FILE" in content or "KAFKA_TLS_KEY_FILE" in content
                or content.get("KAFKA_TOPIC") != plan.get("KAFKA_CONTENT_TOPIC")
                or any(content.get(key) != plan.get(key) for key in topic_keys)
                or not must_wait_for(services["content"], "kafka-topics")):
            raise ValueError("Content needs verified Kafka TLS and the publisher topics from the Kafka plan")
    elif content_kafka == "rollback" and content.get("KAFKA_BOOTSTRAP_SERVERS") != "kafka:9092":
        raise ValueError("Content Kafka rollback requires the legacy listener")
    for key, peers in credentials.items():
        value = broker[key]
        if not isinstance(value, str) or not re.fullmatch(r"[a-fA-F0-9]{64}", value) or value in used_secrets:
            raise ValueError("Kafka catalog credentials must be distinct 64-character hex passwords")
        recipients = {(name, var) for name, env in envs.items() for var, secret in env.items() if secret == value}
        if recipients != {("kafka", key), *peers}:
            raise ValueError("Kafka catalog credential is missing or shared with an unexpected service")
        if key == CONTENT_KAFKA_CREDENTIAL and any(
            value in other for env in envs.values() for other in env.values()
            if isinstance(other, str) and other != value
        ):
            raise ValueError("Content Kafka password must not be embedded in other settings")
        used_secrets.add(value)


def check_sandbox_kafka(services, directory, rollback, used_secrets):
    envs = {name: service.get("environment", {}) for name, service in services.items()}
    sandbox = envs["sandbox"]
    broker = envs["kafka"]
    expected_mode = "true" if directory else "rollback" if rollback else None
    modes = {(name, env["OVM_KAFKA_SANDBOX_TLS"]) for name, env in envs.items() if "OVM_KAFKA_SANDBOX_TLS" in env}
    if modes != ({("kafka-topics", expected_mode)} if expected_mode else set()):
        raise ValueError("Sandbox Kafka ACL changes must use the selected rollout or rollback")
    security_keys = {*SANDBOX_KAFKA_SECURITY, "SANDBOX_KAFKA_TLS_CERT_FILE", "SANDBOX_KAFKA_TLS_KEY_FILE"}
    if (any(set(env) & security_keys for name, env in envs.items() if name != "sandbox")
            or (set(sandbox) & security_keys) != (set(SANDBOX_KAFKA_SECURITY) if directory else set())
            or any(broker.get(key) != (value if directory else None) for key, value in SANDBOX_SCRAM_ENV.items())):
        raise ValueError("Sandbox Kafka credentials and SCRAM require the worker TLS overlay")
    if not directory:
        if rollback and sandbox.get("SANDBOX_KAFKA_BROKERS") != "kafka:9092":
            raise ValueError("Sandbox Kafka rollback requires the legacy listener")
        return
    if (sandbox.get("SANDBOX_KAFKA_BROKERS") != "kafka:9094"
            or any(sandbox.get(key) != val for key, val in SANDBOX_KAFKA_SECURITY.items())
            or services["kafka-topics"].get("group_add") != ["10001"]):
        raise ValueError("Sandbox Kafka requires SCRAM over TLS and worker-only credentials")
    plan = envs["kafka-topics"]
    if (not sandbox.get("SANDBOX_KAFKA_GROUP_ID")
            or sandbox.get("SANDBOX_KAFKA_GROUP_ID") != plan.get("SANDBOX_KAFKA_GROUP_ID")
            or any(sandbox.get(f"SANDBOX_KAFKA_TOPIC_{topic}") != plan.get(f"KAFKA_{topic}_TOPIC")
                   for topic in ("REQUESTS", "RESULTS", "RETRY", "EVENTS", "DEADLETTER"))):
        raise ValueError("Sandbox Kafka group and topics must match the Kafka plan")
    password = (directory / "password").read_text(encoding="ascii").rstrip("\n")
    if password in used_secrets or any(
        password in value for env in envs.values() for value in env.values() if isinstance(value, str)
    ):
        raise ValueError("Sandbox Kafka password must be unique and stay out of container environment")


def check_model(model, target, images, migration=None, runner_tls=None, content_tls=None, catalog_kafka_tls=None,
                feed_kafka=None, sandbox_kafka_dir=None, sandbox_kafka_rollback=False, content_kafka=None):
    if feed_kafka is not None and catalog_kafka_tls is None:
        raise ValueError("Feed Kafka TLS requires the catalog Kafka TLS overlay")
    if (sandbox_kafka_dir is not None or sandbox_kafka_rollback) and catalog_kafka_tls is None:
        raise ValueError("Sandbox Kafka requires the catalog Kafka TLS overlay")
    if content_kafka is not None and catalog_kafka_tls is None:
        raise ValueError("Content Kafka requires the catalog Kafka TLS overlay")
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
        "sandbox-migrate": {(migration, "/app/001_init_runs.sql")},
        "sandbox": {(runner_tls, RUNNER_TLS_PATH)},
        "media-buckets": {MINIO_POLICIES, (MEDIA_POLICY, "/etc/media/public-policy.json")},
        "kafka-topics": {(ROOT / "scripts/kafka", "/opt/overmind/kafka")},
        **{f"{name}-postgres": {POSTGRES_INIT} for name in DATABASES},
        **{f"{name}-grants": {POSTGRES_GRANTS} for name in DATABASES},
    }
    if content_tls is not None:
        allowed_mounts["content"] = {(content_tls / name, f"{CONTENT_TLS_PATH}/{name}")
                                     for name in ("server.pem", "server-key.pem")}
        allowed_mounts["api-gateway"] = {(content_tls / "server-ca.pem", f"{CONTENT_TLS_PATH}/server-ca.pem")}
    if catalog_kafka_tls is not None:
        ca = (catalog_kafka_tls / "ca.pem", f"{CATALOG_KAFKA_PATH}/ca.pem")
        topic_scripts = (ROOT / "scripts/kafka", "/opt/overmind/kafka")
        allowed_mounts["kafka"] = {topic_scripts, (catalog_kafka_tls / "broker.pem", f"{CATALOG_KAFKA_PATH}/broker.pem")}
        allowed_mounts["kafka-topics"].add(ca)
        allowed_mounts["entities"] = {ca}
        if feed_kafka == "tls":
            allowed_mounts["feed"] = {ca}
        if content_kafka == "tls":
            allowed_mounts.setdefault("content", set()).add(ca)
        if sandbox_kafka_dir is not None:
            allowed_mounts["sandbox"].add(ca)
            allowed_mounts["sandbox"].update((sandbox_kafka_dir / name, f"{SANDBOX_KAFKA_PATH}/{name}")
                                             for name in SANDBOX_KAFKA_FILES)
            allowed_mounts["kafka-topics"].add((sandbox_kafka_dir / "password", f"{SANDBOX_KAFKA_PATH}/password"))
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
        files = [volume for volume in service.get("volumes", []) if volume["type"] != "volume"]
        expected_mounts = allowed_mounts.get(name, set())
        if (len(files) != len(expected_mounts) or any(
                volume["type"] != "bind" or not volume.get("read_only")
                or volume.get("bind", {}).get("create_host_path", True) for volume in files
        ) or {(Path(volume["source"]).resolve(), volume["target"]) for volume in files} != expected_mounts):
            raise ValueError(f"{name}: unexpected host mount")
        if any("__GENERATE__" in str(value) for value in service.get("environment", {}).values()):
            raise ValueError(f"{name}: local secret placeholder in the configuration")

    if target == "runner":
        return
    check_renderer(services)
    sandbox_jobs = {"kafka-topics", "sandbox-grants", "media-buckets"}
    if (set(services["sandbox"].get("depends_on", {})) != sandbox_jobs
            or not all(must_wait_for(services["sandbox"], job) for job in sandbox_jobs)):
        raise ValueError("sandbox: unexpected application dependencies")
    if not must_wait_for(services["users"], "users-no-demo"):
        raise ValueError("users: demo account check must succeed before startup")
    used_secrets = check_database_roles(services)
    email = services["users"]["environment"]["BOOTSTRAP_SUPERUSER_EMAIL"].lower()
    if "@" not in email or email.endswith((".local", ".invalid", ".test", ".example")):
        raise ValueError("users: expected an explicit administrator email")
    sandbox = services["sandbox"]["environment"]
    if services["sandbox"]["user"] != "10001:10001" or any(
        sandbox.get(key) != f"{RUNNER_TLS_PATH}/{file}" for key, file in RUNNER_TLS_FILES.items()
    ):
        raise ValueError("sandbox: expected worker TLS files on the private read-only mount")
    media = services["media"]["environment"]
    runner_host, runner_port = runner_origin(sandbox["SANDBOX_GOJUDGE_BASE_URL"])
    compiler_host, compiler_port = runner_origin(sandbox["SANDBOX_GOJUDGE_COMPILER_URL"])
    if runner_host != compiler_host or (runner_port, compiler_port) != (8443, 8444):
        raise ValueError("sandbox: expected distinct runner endpoints on ports 8443 and 8444")
    check_public_edge(network, services["api-gateway"]["environment"], media["MEDIA_S3_PUBLIC_ENDPOINT"])
    if media["MEDIA_PUBLIC_BASE_URL"] != media["MEDIA_S3_PUBLIC_ENDPOINT"] + "/media":
        raise ValueError("media: public URL must point to the media bucket on the object origin")
    check_minio_credentials(services, used_secrets)
    check_service_tokens(services, used_secrets)
    check_catalog_logo_connection(services, used_secrets)
    check_content_connection(services, content_tls is not None, used_secrets)
    check_material_activity(services)
    check_catalog_kafka(services, catalog_kafka_tls is not None, used_secrets, feed_kafka, content_kafka)
    check_sandbox_kafka(services, sandbox_kafka_dir, sandbox_kafka_rollback, used_secrets)


def compose_args(target, env_file, content_tls=None, catalog_kafka_tls=None, feed_kafka=None,
                 sandbox_kafka_dir=None, sandbox_kafka_rollback=False, content_kafka=None):
    args = ["docker", "compose", "--project-directory", str(ROOT)]
    if target == "app":
        args.extend(("--env-file", str(env_file), "-f", "docker-compose.yml", "-f", "deploy/compose.production.yml"))
        if content_tls is not None:
            args.extend(("-f", "deploy/compose.content-tls.yml"))
        if catalog_kafka_tls is not None:
            args.extend(("-f", "deploy/compose.catalog-kafka-tls.yml"))
        if feed_kafka == "tls":
            args.extend(("-f", "deploy/compose.feed-kafka-tls.yml"))
        if feed_kafka == "rollback":
            args.extend(("-f", "deploy/compose.feed-kafka-rollback.yml"))
        if sandbox_kafka_dir is not None:
            args.extend(("-f", "deploy/compose.sandbox-kafka-tls.yml"))
        if sandbox_kafka_rollback:
            args.extend(("-f", "deploy/compose.sandbox-kafka-rollback.yml"))
        if content_kafka:
            args.extend(("-f", f"deploy/compose.content-kafka-{content_kafka}.yml"))
    else:
        args.extend(("-f", "deploy/compose.runner.yml"))
    return args


def compose_env(images, migration=None, runner_tls=None, content_tls=None, catalog_kafka_tls=None,
                sandbox_kafka_dir=None):
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
    if content_tls is not None:
        environment["RELEASE_CONTENT_TLS_DIR"] = str(content_tls)
    if catalog_kafka_tls is not None:
        environment["RELEASE_CATALOG_KAFKA_TLS_DIR"] = str(catalog_kafka_tls)
    if sandbox_kafka_dir is not None:
        environment["RELEASE_SANDBOX_KAFKA_DIR"] = str(sandbox_kafka_dir)
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
    parser.add_argument("--content-tls-dir", type=Path)
    parser.add_argument("--catalog-kafka-tls-dir", type=Path)
    feed_mode = parser.add_mutually_exclusive_group()
    feed_mode.add_argument("--feed-kafka-tls", dest="feed_kafka", action="store_const", const="tls")
    feed_mode.add_argument("--feed-kafka-rollback", dest="feed_kafka", action="store_const", const="rollback")
    sandbox_mode = parser.add_mutually_exclusive_group()
    sandbox_mode.add_argument("--sandbox-kafka-credentials-dir", type=Path)
    sandbox_mode.add_argument("--sandbox-kafka-rollback", action="store_true")
    content_mode = parser.add_mutually_exclusive_group()
    content_mode.add_argument("--content-kafka-tls", dest="content_kafka", action="store_const", const="tls")
    content_mode.add_argument("--content-kafka-rollback", dest="content_kafka", action="store_const", const="rollback")
    args = parser.parse_args()
    app_files = (args.env_file, args.sandbox_migration, args.runner_tls_dir)
    if args.target == "app" and not all(app_files):
        parser.error("app requires --env-file, --sandbox-migration and --runner-tls-dir")
    if args.target == "runner" and (any(app_files) or args.content_tls_dir is not None or args.catalog_kafka_tls_dir is not None):
        parser.error("runner does not accept application files")
    if args.feed_kafka and (args.target != "app" or args.catalog_kafka_tls_dir is None):
        parser.error("Feed Kafka flags require app and --catalog-kafka-tls-dir")
    if args.content_kafka and (args.target != "app" or args.catalog_kafka_tls_dir is None):
        parser.error("Content Kafka flags require app and --catalog-kafka-tls-dir")
    if ((args.sandbox_kafka_credentials_dir is not None or args.sandbox_kafka_rollback)
            and (args.target != "app" or args.catalog_kafka_tls_dir is None)):
        parser.error("Sandbox Kafka flags require app and --catalog-kafka-tls-dir")

    try:
        with args.manifest.open(encoding="utf-8") as file:
            manifest = json.load(file)
        images = image_env(manifest, args.target)
        migration = None
        runner_tls = None
        content_tls = None
        catalog_kafka_tls = None
        sandbox_kafka_dir = None
        env_file = None
        if args.target == "app":
            env_file = (check_private_env(args.env_file) if args.action == "up"
                        else args.env_file.resolve(strict=True))
            migration = args.sandbox_migration.resolve(strict=True)
            check_migration(manifest, migration)
            runner_tls = args.runner_tls_dir.resolve(strict=True)
            check_runner_tls_files(runner_tls, production=args.action == "up")
            if args.content_tls_dir is not None:
                if args.content_tls_dir.is_symlink():
                    raise ValueError("Content TLS directory must not be a symlink")
                content_tls = args.content_tls_dir.resolve(strict=True)
                check_content_tls_files(content_tls, production=args.action == "up")
            if args.catalog_kafka_tls_dir is not None:
                if args.catalog_kafka_tls_dir.is_symlink():
                    raise ValueError("Kafka TLS directory must not be a symlink")
                catalog_kafka_tls = args.catalog_kafka_tls_dir.resolve(strict=True)
                check_catalog_kafka_files(catalog_kafka_tls, production=args.action == "up")
            if args.sandbox_kafka_credentials_dir is not None:
                if args.sandbox_kafka_credentials_dir.is_symlink():
                    raise ValueError("Sandbox Kafka directory must not be a symlink")
                sandbox_kafka_dir = args.sandbox_kafka_credentials_dir.resolve(strict=True)
                check_sandbox_kafka_files(sandbox_kafka_dir, production=args.action == "up")
        environment = compose_env(images, migration, runner_tls, content_tls, catalog_kafka_tls, sandbox_kafka_dir)
        compose = compose_args(args.target, env_file, content_tls, catalog_kafka_tls, args.feed_kafka,
                               sandbox_kafka_dir, args.sandbox_kafka_rollback, args.content_kafka)
        check_model(check_compose(compose, environment), args.target, images, migration, runner_tls, content_tls,
                    catalog_kafka_tls, args.feed_kafka, sandbox_kafka_dir, args.sandbox_kafka_rollback,
                    args.content_kafka)
        if args.action == "up":
            check_host(args.target, environment)
            subprocess.run([*compose, "up", "-d", "--no-build"], check=True, cwd=ROOT, env=environment)
        print(f"{args.target}: production configuration accepted")
    except (OSError, ValueError, KeyError, TypeError, AttributeError, subprocess.SubprocessError) as error:
        parser.exit(1, f"production compose: {error}\n")


if __name__ == "__main__":
    main()
