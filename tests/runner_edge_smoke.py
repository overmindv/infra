#!/usr/bin/env python3
import argparse
import http.client
import secrets
import socket
import ssl
import subprocess
import sys
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from public_edge_smoke import docker, require


ROOT = Path(__file__).resolve().parents[1]
HOST = "runner.localhost"
MARKER = "runner-fixture-secret"


def openssl(*args):
    result = subprocess.run(["openssl", *args], check=False, capture_output=True)
    if result.returncode:
        raise RuntimeError("could not generate temporary certificates with OpenSSL")


def make_ca(directory, name):
    cert, key = directory / f"{name}.pem", directory / f"{name}-key.pem"
    openssl("req", "-x509", "-newkey", "ed25519", "-nodes", "-days", "1",
            "-subj", f"/CN={name}", "-addext", "keyUsage=critical,keyCertSign,cRLSign",
            "-keyout", key, "-out", cert)
    return cert, key


def issue(directory, name, ca, serial, usage, san=None):
    cert, key = directory / f"{name}.pem", directory / f"{name}-key.pem"
    csr, ext = directory.parent / f"{name}.csr", directory.parent / f"{name}.ext"
    options = f"basicConstraints=critical,CA:FALSE\nkeyUsage=digitalSignature\nextendedKeyUsage={usage}\n"
    if san:
        options += f"subjectAltName=DNS:{san}\n"
    ext.write_text(options, encoding="ascii")
    openssl("req", "-new", "-newkey", "ed25519", "-nodes", "-subj", f"/CN={name}",
            "-keyout", key, "-out", csr)
    openssl("x509", "-req", "-in", csr, "-CA", ca[0], "-CAkey", ca[1],
            "-set_serial", str(serial), "-extfile", ext, "-days", "1", "-out", cert)


class Upstream(BaseHTTPRequestHandler):
    def serve(self):
        payload = self.rfile.read(int(self.headers.get("Content-Length", 0)))
        self.send_response(200)
        self.end_headers()
        self.wfile.write(payload or self.path.encode("ascii"))

    do_GET = do_POST = serve

    def log_message(self, *_):
        pass


def request(context, method, path, peer="127.0.0.1", hostname=HOST, body=None):
    connection = http.client.HTTPSConnection(hostname, 8443, timeout=2, context=context)
    try:
        with socket.create_connection(("127.0.0.1", 8443), timeout=2,
                                      source_address=(peer, 0)) as sock:
            connection.sock = context.wrap_socket(sock.dup(), server_hostname=hostname)
        connection.request(method, path, body=body)
        response = connection.getresponse()
        return response.status, response.read()
    finally:
        connection.close()


def client(certificate=None):
    context = ssl.create_default_context(cafile="/tls/runner-ca.pem")
    if certificate:
        context.load_cert_chain(f"/tls/{certificate}.pem", f"/tls/{certificate}-key.pem")
    return context


def probe():
    server = ThreadingHTTPServer(("127.0.0.1", 15050), Upstream)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    worker = client("worker")
    for attempt in range(25):
        try:
            version = request(worker, "GET", "/version")
            break
        except ConnectionRefusedError:
            if attempt == 24:
                raise
            time.sleep(0.2)
    require(version == (200, b"/version"), "authorized health request failed")
    require(request(worker, "POST", "/run", body=b"fixture") == (200, b"fixture"), "job payload changed")
    require(request(worker, "GET", "/version", peer="127.0.0.2")[0] == 403, "unexpected source IP allowed")
    for method, path in (("GET", "/run"), ("POST", "/version"), ("GET", "/file")):
        require(request(worker, method, path)[0] == 404, "unexpected method or route exposed")
    for context, hostname in ((client(), HOST), (client("foreign"), HOST), (worker, "wrong.localhost")):
        try:
            request(context, "GET", "/version", hostname=hostname)
        except ssl.SSLError:
            continue
        raise RuntimeError("unexpected TLS identity accepted")
    try:
        with socket.create_connection(("127.0.0.1", 80), timeout=1):
            raise RuntimeError("runner proxy opened an HTTP listener")
    except ConnectionRefusedError:
        pass
    server.shutdown()
    server.server_close()
    status, _ = request(worker, "POST", f"/run?fixture={MARKER}", body=MARKER.encode("ascii"))
    require(status == 502, "missing upstream failure response")
    print("Runner routes passed; wrong certificates, hostname, source IP and HTTP rejected")


def main():
    parser = argparse.ArgumentParser(description="Exercise the private Caddy proxy with temporary certificates")
    parser.add_argument("--caddy-image", default="caddy:2.11.9-alpine")
    parser.add_argument("--probe-image", default="python:3-alpine")
    args = parser.parse_args()
    name = f"ovm163-runner-{secrets.token_hex(5)}"
    with tempfile.TemporaryDirectory(prefix="runner-local-ca-") as path:
        root = Path(path)
        runner, clients = root / "runner", root / "clients"
        runner.mkdir()
        clients.mkdir()
        runner_ca = make_ca(root, "runner-ca")
        worker_ca = make_ca(root, "worker-ca")
        foreign_ca = make_ca(root, "foreign-ca")
        issue(runner, "server", runner_ca, 2, "serverAuth", HOST)
        issue(clients, "worker", worker_ca, 2, "clientAuth")
        issue(clients, "foreign", foreign_ca, 2, "clientAuth")
        (runner / "worker-ca.pem").write_bytes(worker_ca[0].read_bytes())
        (clients / "runner-ca.pem").write_bytes(runner_ca[0].read_bytes())
        started = docker(
            "run", "--rm", "-d", "--name", name, "--network", "none", "--read-only",
            "--tmpfs", "/data", "--tmpfs", "/config",
            "--mount", f"type=bind,source={ROOT / 'deploy/caddy/runner.Caddyfile'},target=/etc/caddy/Caddyfile,readonly",
            "--mount", f"type=bind,source={runner},target=/etc/overmindv/runner-tls,readonly",
            "-e", f"RUNNER_TLS_HOST={HOST}", "-e", "RUNNER_BIND_IP=127.0.0.1",
            "-e", "RUNNER_APP_IP=127.0.0.1", args.caddy_image,
        )
        if started.returncode:
            parser.exit(1, "could not start the temporary runner proxy\n")
        try:
            result = docker(
                "run", "--rm", "--network", f"container:{name}", "--read-only",
                "--mount", f"type=bind,source={ROOT / 'tests'},target=/tests,readonly",
                "--mount", f"type=bind,source={clients},target=/tls,readonly",
                args.probe_image, "python", "-B", "/tests/runner_edge_smoke.py", "probe",
            )
            logs = docker("logs", name, check=True)
            if result.returncode:
                raise RuntimeError((result.stderr + logs.stderr).decode("utf-8", errors="replace"))
            payload = logs.stdout + logs.stderr
            require(b"http.log.error" in payload, "no upstream error was logged")
            require(MARKER.encode("ascii") not in payload, "runner query or job body reached the log")
            print(result.stdout.decode("utf-8").strip())
        finally:
            docker("stop", "-t", "2", name)


if __name__ == "__main__":
    if sys.argv[1:] == ["probe"]:
        probe()
    else:
        main()
