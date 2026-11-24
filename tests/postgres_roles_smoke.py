#!/usr/bin/env python3
import argparse
import os
import re
import secrets
import statistics
import subprocess
import time
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
DB = "ovm_fixture"
APP = "overmind_app"
MIGRATOR = "overmind_migrator"
PASSWORD_KEYS = ("POSTGRES_PASSWORD", "OVM_DB_MIGRATOR_PASSWORD", "OVM_DB_APP_PASSWORD")


def docker(*args, **options):
    return subprocess.run(["docker", *args], capture_output=True, encoding="utf-8", **options)


def require(condition, message):
    if not condition:
        raise RuntimeError(message)


def psql(container, role, password, sql, database=DB):
    return docker(
        "exec", "-i", "-e", "PGPASSWORD", container, "psql", "-XwqAt", "-v", "ON_ERROR_STOP=1",
        "-h", "127.0.0.1", "-U", role, "-d", database,
        input=sql, env={**os.environ, "PGPASSWORD": password},
    )


def succeed(result, step):
    require(result.returncode == 0, f"{step} failed")
    return result.stdout.strip()


def denied(result, reason):
    require(result.returncode != 0 and reason in result.stderr, f"expected PostgreSQL to report {reason}")


def benchmark(container, admin, app):
    samples = {"postgres": [], APP: []}
    for _ in range(3):
        for role, password in (("postgres", admin), (APP, app)):
            result = docker(
                "exec", "-i", "-e", "PGPASSWORD", container, "pgbench", "-n", "-c", "2", "-j", "2",
                "-t", "1000", "-f", "/dev/stdin", "-h", "127.0.0.1", "-U", role, DB,
                input="SELECT value FROM demo WHERE id = 1000;\n",
                env={**os.environ, "PGPASSWORD": password},
            )
            output = succeed(result, "benchmark indexed read")
            latency = re.search(r"latency average = ([0-9.]+) ms", output)
            require(latency is not None, "missing pgbench latency")
            samples[role].append(float(latency[1]))
    print(f"Median of 3 pgbench runs (2 clients, 2000 indexed reads each): "
          f"admin {statistics.median(samples['postgres']):.3f} ms, "
          f"runtime {statistics.median(samples[APP]):.3f} ms")


def exercise(container, passwords, measure):
    app, migrator = passwords["OVM_DB_APP_PASSWORD"], passwords["OVM_DB_MIGRATOR_PASSWORD"]
    step = psql(container, MIGRATOR, migrator, """
        CREATE EXTENSION citext;
        CREATE EXTENSION pg_trgm;
        CREATE EXTENSION pgcrypto;
        CREATE TABLE demo (id bigserial PRIMARY KEY, value citext NOT NULL);
        CREATE TABLE user_security_audit (id bigserial PRIMARY KEY, event text NOT NULL);
        CREATE TABLE goose_db_version (id bigint GENERATED ALWAYS AS IDENTITY, version_id bigint NOT NULL);
        CREATE VIEW demo_values AS SELECT value FROM demo;
        INSERT INTO goose_db_version (version_id) VALUES (1);
        INSERT INTO demo (value) SELECT i::text FROM generate_series(1, 10000) AS i;
    """)
    succeed(step, "migrate as limited role")
    denied(psql(container, APP, app, "SELECT id FROM demo LIMIT 1;"), "permission denied")

    grants = (ROOT / "deploy/postgres/runtime-grants.sql").read_text(encoding="utf-8")
    for _ in range(2):
        succeed(psql(container, MIGRATOR, migrator, grants), "publish runtime privileges")
    actual = succeed(psql(container, APP, app, """
        INSERT INTO demo (value) VALUES ('after grants');
        UPDATE demo SET value = 'updated' WHERE value = 'after grants';
        DELETE FROM demo WHERE value = 'updated';
        INSERT INTO user_security_audit (event) VALUES ('test');
        SELECT count(*) FROM demo_values;
    """), "CRUD, sequence and view as runtime role")
    require(actual == "10000", "unexpected fixture row count")

    for sql, reason in (
        ("CREATE TABLE unexpected (id int);", "permission denied for schema"),
        ("ALTER TABLE demo ADD COLUMN unrelated text;", "must be owner of table"),
        ("CREATE SCHEMA unexpected;", "permission denied for database"),
        ("SELECT version_id FROM goose_db_version;", "permission denied for table"),
        ("SELECT nextval('goose_db_version_id_seq');", "permission denied for sequence"),
        ("UPDATE user_security_audit SET event = 'changed';", "permission denied for table"),
        ("DELETE FROM user_security_audit;", "permission denied for table"),
    ):
        denied(psql(container, APP, app, sql), reason)
    denied(psql(container, APP, app, "SELECT 1;", database="postgres"), "permission denied")
    require(succeed(psql(container, MIGRATOR, migrator, """
        SELECT count(*) FROM pg_roles
        WHERE rolname IN ('overmind_app', 'overmind_migrator')
          AND NOT (rolsuper OR rolcreatedb OR rolcreaterole OR rolreplication OR rolbypassrls);
    """), "checking role attributes") == "2", "service role has cluster privileges")
    print("Runtime CRUD worked; DDL, maintenance database, migration history and audit updates denied")
    if measure:
        benchmark(container, passwords["POSTGRES_PASSWORD"], app)

    new_password = secrets.token_hex(32)
    rotated = docker(
        "exec", "-i", "--user", "postgres", container, "psql", "-Xwq",
        "-v", "ON_ERROR_STOP=1", "-U", "postgres", "-d", DB, "-c", r"\password overmind_app",
        input=f"{new_password}\n{new_password}\n",
    )
    succeed(rotated, "rotate runtime password")
    denied(psql(container, APP, app, "SELECT 1;"), "password authentication failed")
    require(succeed(psql(container, APP, new_password, "SELECT count(*) FROM demo;"),
                    "connect with new password") == "10000", "rotation lost data")
    log = docker("logs", container)
    logs = log.stdout + log.stderr
    require(all(value not in logs for value in (*passwords.values(), new_password)), "password found in PostgreSQL log")
    print("Runtime password rotated; old login refused, active data preserved, no raw password in logs")
    return new_password


def wait_for_postgres(container):
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline:
        if docker("exec", container, "pg_isready", "-q", "-h", "127.0.0.1", "-U", "postgres", "-d", DB).returncode == 0:
            return
        time.sleep(0.25)
    raise RuntimeError("temporary PostgreSQL did not start")


def check_server_user(container):
    uid = succeed(docker("exec", container, "id", "-u", "postgres"), "lookup OS postgres user")
    status = succeed(docker("exec", container, "cat", "/proc/1/status"), "inspect PostgreSQL process")
    actual = re.search(r"^Uid:\s+([^\n]+)", status, re.MULTILINE)
    require(uid.isdecimal() and uid != "0" and actual and actual[1].split() == [uid] * 4,
            "PostgreSQL PID 1 did not drop root")


def main():
    parser = argparse.ArgumentParser(description="Test production roles on a temporary PostgreSQL")
    parser.add_argument("--benchmark", action="store_true")
    parser.add_argument("--image", default="postgres:17-alpine", help="PostgreSQL image or local image ID")
    args = parser.parse_args()
    if args.image.startswith("-"):
        parser.error("expected an image reference")
    name = f"ovm163-db-{secrets.token_hex(5)}"
    passwords = {key: secrets.token_hex(32) for key in PASSWORD_KEYS}
    # Keep PGDATA in the fixture container layer so restart preserves it without a Docker volume.
    start = time.monotonic()
    started = docker(
        "run", "-d", "--name", name, "--platform", "linux/amd64", "--network", "none",
        "--cpus", "2", "--memory", "384m", "--security-opt", "no-new-privileges:true",
        "--tmpfs", "/var/lib/postgresql/data:rw,size=1m",
        "--mount", f"type=bind,source={ROOT / 'deploy/postgres/init-roles.sql'},"
                   "target=/docker-entrypoint-initdb.d/20-roles.sql,readonly",
        "-e", f"POSTGRES_DB={DB}", "-e", "POSTGRES_USER=postgres",
        "-e", "PGDATA=/var/lib/postgresql/ovm-fixture",
        "-e", "POSTGRES_INITDB_ARGS=--auth-local=peer --auth-host=scram-sha-256",
        *(argument for key in passwords for argument in ("-e", key)),
        args.image, env={**os.environ, **passwords},
    )
    container = succeed(started, "start temporary PostgreSQL")
    require(re.fullmatch(r"[0-9a-f]{64}", container), "unexpected fixture container ID")
    try:
        wait_for_postgres(container)
        print(f"Fresh database ready in {time.monotonic() - start:.3f} s")
        check_server_user(container)
        password = exercise(container, passwords, args.benchmark)
        succeed(docker("restart", "-t", "5", container), "restart temporary PostgreSQL")
        wait_for_postgres(container)
        check_server_user(container)
        actual = succeed(psql(container, APP, password, """
            SELECT count(*) FROM demo;
            SELECT (xpath('/root/value/text()', '<root><value>ok</value></root>'::xml))[1]::text;
        """), "read preserved data and XML after restart")
        require(actual == "10000\nok", "restart lost fixture data or XML support")
        print("PostgreSQL runs without root; XML and row checks passed after restart")
    finally:
        docker("stop", "-t", "5", container)
        docker("rm", container)


if __name__ == "__main__":
    main()
