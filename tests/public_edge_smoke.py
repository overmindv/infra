#!/usr/bin/env python3
import argparse
import http.client
import json
import secrets
import socket
import ssl
import statistics
import subprocess
import sys
import threading
import time
from contextlib import closing
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
WEB_HOST = "app.localhost"
OBJECT_HOST = "storage.localhost"
ORIGIN = f"https://{WEB_HOST}"
OBJECT_ORIGIN = f"https://{OBJECT_HOST}"
SIGNATURE = "fixture-presign-marker"
ACTOR = "fixture-actor-marker"
COOKIE = "fixture-cookie-marker"


def require(condition, message):
    if not condition:
        raise RuntimeError(message)


class Upstream(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    disable_nagle_algorithm = True

    def serve(self):
        size = int(self.headers.get("Content-Length", 0))
        self.rfile.read(size)
        payload = json.dumps({"path": self.path, "headers": dict(self.headers)}).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        if self.server.server_port == 18081:
            self.send_header("Set-Cookie", "session=fixture; Secure; HttpOnly; SameSite=Lax")
        if self.server.server_port == 19000:
            self.send_header("Access-Control-Allow-Origin", "*")
            self.send_header("ETag", '"fixture"')
            self.send_header("Connection", "close")
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(payload)

    do_GET = do_HEAD = do_POST = do_PUT = serve

    def log_message(self, *_):
        pass


def connect(host, context):
    conn = http.client.HTTPSConnection(host, timeout=3, context=context)
    with socket.create_connection(("127.0.0.1", 443), timeout=3) as sock:
        conn.sock = context.wrap_socket(sock.dup(), server_hostname=host)
    return conn


def request(conn, path, method="GET", headers=None, body=None):
    conn.request(method, path, body=body, headers=headers or {})
    response = conn.getresponse()
    return response.status, response.headers, response.read()


def run_probe(ca):
    context = ssl.create_default_context(cadata=ca)
    servers = [ThreadingHTTPServer(("127.0.0.1", port), Upstream) for port in (13000, 18081, 19000)]
    for server in servers:
        threading.Thread(target=server.serve_forever, daemon=True).start()

    conn = http.client.HTTPConnection("127.0.0.1", 80, timeout=3)
    status, headers, _ = request(conn, "/graphql", headers={"Host": WEB_HOST})
    conn.close()
    require(status == 308 and headers["Location"] == f"{ORIGIN}/graphql", "missing HTTPS redirect")
    for attempt in range(40):
        try:
            web = connect(WEB_HOST, context)
            break
        except OSError:
            if attempt == 39:
                raise
            time.sleep(0.2)

    with closing(web), closing(connect(OBJECT_HOST, context)) as objects:
        status, headers, _ = request(web, "/")
        require(status == 200 and OBJECT_ORIGIN in headers["Content-Security-Policy"], "wrong web policy")
        status, headers, body = request(web, "/graphql", "POST", headers={
            "Origin": ORIGIN,
            "Authorization": "Bearer fixture",
            "Cookie": f"session={COOKIE}",
            "X-Forwarded-Proto": "http",
            "X-Forwarded-For": "192.0.2.42",
            "Forwarded": "proto=http",
            "X-User-ID": ACTOR,
            "X-User-Roles": "admin",
            "X-Actor-ID": ACTOR,
            "X-Login-Client-Signature": ACTOR,
            "X-Media-Service-Token": ACTOR,
            "X-Feed-Service-Token": ACTOR,
        }, body=b"{}")
        require(status == 200 and "Secure" in headers["Set-Cookie"], "Secure cookie did not pass")
        forwarded = {name.lower(): value for name, value in json.loads(body)["headers"].items()}
        require(forwarded["x-forwarded-proto"] == "https", "proxy kept the supplied scheme")
        require(forwarded["x-forwarded-for"] != "192.0.2.42", "proxy kept the supplied peer")
        require("forwarded" not in forwarded and not any(
            name.startswith(("x-user-", "x-actor-", "x-login-client-"))
            or name.endswith("-service-token") for name in forwarded
        ), "internal headers reached the gateway")
        require(forwarded["cookie"] == f"session={COOKIE}", "session cookie stripped from GraphQL")
        require(forwarded["authorization"] == "Bearer fixture", "GraphQL authorization was stripped")
        require(request(web, "/internal/session/check")[0] == 404, "internal API was exposed")

        part = f"/media-quarantine/uploads/fixture?partNumber=1&X-Amz-Signature={SIGNATURE}"
        signed = f"/media/originals/fixture?X-Amz-Signature={SIGNATURE}"
        preview = "/media/public/verified/variants/fixture/w128.webp"
        for method, path in (("POST", "/media-quarantine"), ("PUT", part),
                             ("GET", signed), ("HEAD", preview)):
            status, headers, _ = request(objects, path, "OPTIONS", headers={
                "Origin": ORIGIN, "Access-Control-Request-Method": method,
            })
            require(status == 204 and headers["Access-Control-Allow-Origin"] == ORIGIN, "wrong S3 preflight")
            require(method in headers["Access-Control-Allow-Methods"], "missing allowed storage method")
        require(request(objects, part, "OPTIONS", headers={
            "Origin": "https://foreign.localhost", "Access-Control-Request-Method": "PUT",
        })[0] == 403, "foreign browser origin was allowed")
        for method, path in (("GET", "/"), ("GET", "/minio/admin/v3/info"),
                             ("GET", "/sandbox-tests/fixture"), ("DELETE", signed)):
            require(request(objects, path, method, {"Origin": ORIGIN})[0] == 404, "unexpected S3 route")
        require(request(objects, part, "PUT", body=b"fixture")[0] == 404, "upload without Origin allowed")
        status, _, body = request(objects, "/media-quarantine", "POST", {
            "Origin": ORIGIN, "Content-Type": "multipart/form-data; boundary=fixture",
        }, body=b"test form")
        require(status == 200 and json.loads(body)["path"] == "/media-quarantine", "POST upload was rejected")
        status, headers, body = request(objects, part, "PUT", headers={
            "Origin": ORIGIN, "Authorization": "Bearer fixture", "Cookie": f"session={COOKIE}",
        }, body=b"test part")
        require(status == 200 and headers.get_all("Access-Control-Allow-Origin") == [ORIGIN], "upstream CORS leaked")
        result = json.loads(body)
        object_headers = {name.lower(): value for name, value in result["headers"].items()}
        require(result["path"] == part and object_headers["host"] == OBJECT_HOST, "signature inputs changed")
        require(not {"authorization", "cookie"} & object_headers.keys(), "session credentials reached S3")
        require(request(objects, signed, "GET")[0] == 200, "signed download did not reach S3")
        require(request(objects, preview, "GET")[0] == 200, "verified preview route did not reach S3")
        with closing(connect(OBJECT_HOST, context)) as oversized:
            require(request(oversized, part, "PUT", headers={
                "Origin": ORIGIN, "Content-Length": str(102 * 1024 * 1024), "Expect": "100-continue",
            })[0] == 413, "upload body limit was not enforced")

        timings = {}
        direct = http.client.HTTPConnection("127.0.0.1", 18081, timeout=3)
        for name, upstream in (("direct", direct), ("https", web)):
            samples = []
            for _ in range(30):
                start = time.perf_counter()
                require(request(upstream, "/graphql")[0] == 200, "timed request failed")
                samples.append((time.perf_counter() - start) * 1000)
            timings[name] = statistics.median(samples)
        direct.close()
        servers[-1].shutdown()
        servers[-1].server_close()
        require(request(objects, signed, headers={"Cookie": COOKIE, "X-User-ID": ACTOR})[0] == 502,
                "missing upstream failure response")
    print(f"TLS, headers, CORS and S3 routes passed; local median from 30 requests: "
          f"direct {timings['direct']:.2f} ms, via HTTPS {timings['https']:.2f} ms")


def docker(*args, **options):
    return subprocess.run(["docker", *args], capture_output=True, **options)


def main():
    parser = argparse.ArgumentParser(description="Smoke test the public reverse proxy without public DNS or ACME")
    parser.add_argument("--caddy-image", default="caddy:2.11.9-alpine")
    parser.add_argument("--probe-image", default="python:3-alpine")
    args = parser.parse_args()
    name = f"ovm163-edge-{secrets.token_hex(5)}"
    caddyfile = ROOT / "deploy/caddy/public.Caddyfile"
    script = Path(__file__).resolve()
    started = docker("run", "--rm", "-d", "--name", name, "--network", "none",
                     "--read-only", "--tmpfs", "/data", "--tmpfs", "/config",
                     "--mount", f"type=bind,source={caddyfile},target=/etc/caddy/Caddyfile,readonly",
                     "-e", f"CORS_ORIGINS={ORIGIN}", "-e", f"MEDIA_S3_PUBLIC_ENDPOINT={OBJECT_ORIGIN}",
                     args.caddy_image)
    if started.returncode:
        parser.exit(1, "could not start temporary Caddy\n")
    try:
        for attempt in range(40):
            ca = docker("exec", name, "cat", "/data/caddy/pki/authorities/local/root.crt")
            if ca.returncode == 0:
                break
            if attempt == 39:
                raise RuntimeError("temporary Caddy CA did not initialize")
            time.sleep(0.2)
        probe = docker("run", "--rm", "-i", "--network", f"container:{name}",
                       "--read-only", "--workdir", "/tmp",
                       "--mount", f"type=bind,source={script},target=/tmp/probe.py,readonly",
                       args.probe_image, "python", "-B", "/tmp/probe.py", "probe", input=ca.stdout)
        if probe.returncode:
            raise RuntimeError(probe.stderr.decode("utf-8", errors="replace"))
        logs = docker("logs", name, check=True)
        payload = logs.stdout + logs.stderr
        require(b"http.log.error" in payload, "no reverse proxy error in the log")
        for value in (SIGNATURE, COOKIE, ACTOR):
            require(value.encode() not in payload, "sensitive request data reached the proxy log")
        print(probe.stdout.decode("utf-8").strip())
        print("Upstream failure did not log request credentials or the signed query")
    finally:
        docker("stop", "-t", "2", name)


if __name__ == "__main__":
    if sys.argv[1:] == ["probe"]:
        run_probe(sys.stdin.read())
    else:
        main()
