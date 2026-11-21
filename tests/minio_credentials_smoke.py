#!/usr/bin/env python3
import argparse
import hashlib
import os
import secrets
import statistics
import subprocess
import time
import xml.etree.ElementTree as xml
from pathlib import Path
from urllib.parse import urlencode


ROOT = Path(__file__).resolve().parents[1]
IMAGE = "cgr.dev/chainguard/minio@sha256:e9cf6b99c226e9539ed0f14b6f00e37b07da153c7ba1f4f8709c807574b471ec"


def require(condition, message):
    if not condition:
        raise RuntimeError(message)


def run(command, **kwargs):
    return subprocess.run(command, capture_output=True, text=True, encoding="utf-8", **kwargs)


def docker(*args, **kwargs):
    return run(["docker", *args], **kwargs)


def object_request(base, method, path, identity=None, body=None, headers=()):
    command = ["curl", "-q", "--silent", "--show-error", "--noproxy", "*", "--max-time", "5",
               "--write-out", "\n%{http_code}"]
    command.extend(("--head",) if method == "HEAD" else ("--request", method))
    config = ""
    if identity:
        command.extend(("--aws-sigv4", "aws:amz:us-east-1:s3", "--config", "-"))
        config = f'user = "{identity[0]}:{identity[1]}"\n'
    if body is not None:
        command.extend(("--data-binary", body))
    for header in headers:
        command.extend(("--header", header))
    result = run([*command, base + path], input=config)
    require(result.returncode == 0, f"{method} request failed")
    response, _, status = result.stdout.rpartition("\n")
    return int(status), response


def expect(base, status, method, path, identity=None, **kwargs):
    actual, payload = object_request(base, method, path, identity, **kwargs)
    require(actual == status, f"{method} {path.split('?')[0]}: expected {status}, got {actual}")
    return payload


def bootstrap(server, image, environment):
    return docker(
        "run", "--rm", "--network", f"container:{server}", "--read-only",
        "--tmpfs", "/tmp:rw,mode=1777,size=16m",
        "--mount", f"type=bind,source={ROOT / 'deploy/minio'},target=/app/minio,readonly",
        "--mount", f"type=bind,source={ROOT / 'config/media/public-policy.json'},target=/etc/media/public-policy.json,readonly",
        "-e", "OVM_MINIO_ENDPOINT=http://127.0.0.1:9000",
        *(value for key in environment for value in ("-e", key)),
        "--entrypoint", "/bin/sh", image, "/app/minio/init.sh",
        env={**os.environ, **environment},
    )


def admin(server, *args):
    result = docker(
        "exec", server, "/bin/sh", "-ec",
        'export MC_CONFIG_DIR; MC_CONFIG_DIR=$(mktemp -d /tmp/ovm-admin.XXXXXX); '
        'mc alias set local http://127.0.0.1:9000 "$MINIO_ROOT_USER" "$MINIO_ROOT_PASSWORD" >/dev/null; '
        'exec mc "$@"',
        "mc-fixture", *args,
    )
    require(result.returncode == 0, "temporary MinIO administration failed")


def benchmark(base, identities, path):
    for label, identity in identities.items():
        command = ["curl", "-q", "--silent", "--show-error", "--noproxy", "*", "--max-time", "5",
                   "--aws-sigv4", "aws:amz:us-east-1:s3", "--config", "-", "--write-out",
                   "%{http_code} %{time_total}\n"]
        for _ in range(30):
            command.extend(("--output", os.devnull, "--url", base + path))
        result = run(command, input=f'user = "{identity[0]}:{identity[1]}"\n')
        require(result.returncode == 0, "could not benchmark object reads")
        samples = [line.split() for line in result.stdout.splitlines()]
        require(len(samples) == 30 and all(code == "200" for code, _ in samples), "benchmark request failed")
        print(f"{label}: median {1000 * statistics.median(float(time) for _, time in samples[1:]):.3f} ms "
              "(29 reused-connection reads)")


def exercise(server, image, base, environment, measure):
    root = (environment["MINIO_ROOT_USER"], environment["MINIO_ROOT_PASSWORD"])
    media = (environment["MEDIA_S3_ACCESS_KEY"], environment["MEDIA_S3_SECRET_KEY"])
    reader = (environment["SANDBOX_TESTS_ACCESS_KEY_ID"], environment["SANDBOX_TESTS_SECRET_ACCESS_KEY"])
    writer = (environment["SANDBOX_ARTIFACTS_ACCESS_KEY_ID"], environment["SANDBOX_ARTIFACTS_SECRET_ACCESS_KEY"])
    for _ in range(2):
        result = bootstrap(server, image, environment)
        require(result.returncode == 0, result.stderr.strip() or "MinIO bootstrap failed")

    suite = "/sandbox-tests/sandbox-tests/prod/test-suites/fixture/v1.json"
    off_prefix = "/sandbox-tests/other/fixture"
    original = "/media/originals/fixture"
    quarantine = "/media-quarantine/uploads/fixture"
    artifact = "/sandbox-artifacts/sandbox-artifacts/prod/runs/fixture/stdout.txt"
    expect(base, 200, "PUT", suite, root, body="fixture")
    expect(base, 200, "PUT", off_prefix, root, body="fixture")
    require(expect(base, 200, "GET", suite, reader) == "fixture", "hidden test read failed")
    expect(base, 403, "GET", off_prefix, reader)
    expect(base, 403, "PUT", suite, reader, body="fixture")
    expect(base, 403, "GET", suite)
    expect(base, 403, "GET", suite, media)
    expect(base, 403, "GET", suite, writer)
    for identity in (media, reader, writer):
        expect(base, 403, "GET", "/minio/admin/v3/info", identity)
    expect(base, 200, "PUT", artifact, writer, body="fixture")
    expect(base, 403, "GET", artifact, writer)
    expect(base, 403, "DELETE", artifact, writer)
    expect(base, 403, "PUT", "/sandbox-artifacts/other/fixture", writer, body="fixture")
    expect(base, 403, "PUT", original, writer, body="fixture")

    expect(base, 200, "HEAD", "/media", media)
    expect(base, 200, "PUT", quarantine, media, body="fixture")
    expect(base, 200, "HEAD", quarantine, media)
    expect(base, 200, "PUT", original, media, headers=(f"x-amz-copy-source: {quarantine}",))
    expect(base, 403, "GET", original)
    expect(base, 403, "PUT", "/media/unmanaged/fixture", media, body="fixture")
    require(expect(base, 200, "GET", original, media) == "fixture", "media copy content differs")

    preview = "/media/public/verified/variants/fixture/w128.webp"
    expect(base, 200, "PUT", preview, media, body="preview")
    require(expect(base, 200, "GET", preview) == "preview", "verified preview is not public")
    expect(base, 403, "PUT", preview, body="fixture")
    expect(base, 403, "PUT", preview.removesuffix(".webp") + ".html", media, body="fixture")

    multi = "/media-quarantine/uploads/multipart-fixture"
    response = expect(base, 200, "POST", multi + "?uploads", media, body="")
    upload_id = xml.fromstring(response).findtext("{*}UploadId")
    require(upload_id, "multipart upload did not return an ID")
    query = urlencode({"uploadId": upload_id})
    part = "fixture-part"
    expect(base, 200, "PUT", f"{multi}?partNumber=1&{query}", media, body=part)
    digest = hashlib.md5(part.encode(), usedforsecurity=False).hexdigest()
    complete = f'<CompleteMultipartUpload><Part><PartNumber>1</PartNumber><ETag>"{digest}"</ETag></Part></CompleteMultipartUpload>'
    expect(base, 200, "POST", f"{multi}?{query}", media, body=complete)
    require(expect(base, 200, "GET", multi, media) == part, "multipart body differs")
    expect(base, 204, "DELETE", multi, media)
    print("Media copy and multipart passed; anonymous, cross-bucket and out-of-prefix requests denied")

    if measure:
        benchmark(base, {"root": root, "scoped reader": reader}, suite)
    replacement = ("ovm-media-" + secrets.token_hex(4), secrets.token_hex(20))
    rotated = dict(environment, MEDIA_S3_ACCESS_KEY=replacement[0], MEDIA_S3_SECRET_KEY=replacement[1])
    result = bootstrap(server, image, rotated)
    require(result.returncode == 0, result.stderr.strip() or "new media identity provisioning failed")
    expect(base, 200, "GET", original, media)
    expect(base, 200, "GET", original, replacement)
    admin(server, "admin", "user", "disable", "local", media[0])
    expect(base, 403, "GET", original, media)
    expect(base, 200, "GET", original, replacement)
    disabled = bootstrap(server, image, environment)
    require(disabled.returncode != 0 and "unexpected IAM bindings or status" in disabled.stderr,
            "disabled access key was silently revived")
    admin(server, "admin", "policy", "attach", "local", "readonly", "--user", replacement[0])
    broad = bootstrap(server, image, rotated)
    require(broad.returncode != 0 and "unexpected IAM bindings or status" in broad.stderr,
            "identity with extra access was accepted")
    logs = docker("logs", server)
    payload = logs.stdout + logs.stderr + disabled.stderr + broad.stderr
    for secret in (root[1], media[1], reader[1], writer[1], replacement[1]):
        require(secret not in payload, "raw credential was logged")
    print("Rotation kept both keys valid during handover; revoked key and excess IAM grants rejected")


def main():
    parser = argparse.ArgumentParser(description="Check production IAM on an isolated MinIO")
    parser.add_argument("--image", default=IMAGE)
    parser.add_argument("--benchmark", action="store_true")
    args = parser.parse_args()
    name = f"ovm163-minio-{secrets.token_hex(5)}"
    environment = {
        "MINIO_ROOT_USER": "fixture-root",
        "MINIO_ROOT_PASSWORD": secrets.token_hex(20),
        "MEDIA_S3_ACCESS_KEY": "ovm-media-00000001",
        "MEDIA_S3_SECRET_KEY": secrets.token_hex(20),
        "SANDBOX_TESTS_ACCESS_KEY_ID": "ovm-tests-00000001",
        "SANDBOX_TESTS_SECRET_ACCESS_KEY": secrets.token_hex(20),
        "SANDBOX_ARTIFACTS_ACCESS_KEY_ID": "ovm-art-00000001",
        "SANDBOX_ARTIFACTS_SECRET_ACCESS_KEY": secrets.token_hex(20),
    }
    started = docker("run", "--rm", "-d", "--name", name, "--publish", "127.0.0.1::9000",
                     "--memory", "768m", "--cpus", "2", "--tmpfs", "/data:rw,uid=65532,gid=65532,size=192m",
                     "--tmpfs", "/tmp:rw,mode=1777,size=16m",
                     "-e", "MINIO_ROOT_USER", "-e", "MINIO_ROOT_PASSWORD", "-e", "MINIO_BROWSER=off",
                     args.image, "server", "/data", env={**os.environ, **environment})
    require(started.returncode == 0, "could not start temporary MinIO")
    try:
        binding = docker("port", name, "9000/tcp").stdout.strip()
        require(binding.startswith("127.0.0.1:"), "temporary MinIO is not bound to loopback")
        base = f"http://{binding}"
        for _ in range(32):
            if docker("exec", name, "mc", "ready", "local").returncode == 0:
                break
            time.sleep(0.4)
        else:
            raise RuntimeError("temporary MinIO did not become ready")
        exercise(name, args.image, base, environment, args.benchmark)
    finally:
        docker("stop", "-t", "2", name)


if __name__ == "__main__":
    main()
