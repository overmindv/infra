#!/usr/bin/env python3
"""Контракт GraphQL gateway → настоящий HTTP tasks → выделенная PostgreSQL.

Users и entities заменены фиксированными HTTP-ответами. Внешняя Kafka не нужна.
"""
import argparse
import base64
import contextlib
import hashlib
import hmac
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import socket
import subprocess
import tempfile
import threading
import time
import urllib.error
import urllib.parse
import urllib.request


ROOT = Path(__file__).resolve().parents[2]
OWNER = "00000000-0000-4000-8000-000000000002"
PRIVATE = "00000000-0000-4000-8000-000000000003"
DELETED = "00000000-0000-4000-8000-000000000004"
COURSE = "00000000-0000-4000-8000-000000000009"
TOPIC_A = "00000000-0000-4000-8000-000000000011"
TOPIC_B = "00000000-0000-4000-8000-000000000012"
JWT_SECRET = "social-contract-jwt-secret-only"


class Fixtures(BaseHTTPRequestHandler):
    def log_message(self, *_args):
        pass

    def reply(self, status, payload):
        raw = json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def do_POST(self):
        if self.path != "/query":
            self.reply(404, {})
            return
        body = json.loads(self.rfile.read(int(self.headers.get("Content-Length", "0"))))
        user_id = body.get("variables", {}).get("id")
        if user_id == DELETED:
            self.reply(200, {"errors": [{"message": "not found", "extensions": {"code": "NOT_FOUND"}}]})
            return
        profile = None
        if user_id in (OWNER, PRIVATE):
            profile = {"id": user_id, "username": "owner" if user_id == OWNER else "private",
                       "firstName": "Public", "lastName": "Learner", "isAdmin": False,
                       "createdAt": "2026-01-01T00:00:00Z"}
        self.reply(200, {"data": {"publicUser": profile}})

    def do_GET(self):
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path
        if path in ("/health", "/ready", "/healthz"):
            self.reply(200, {"status": "ok"})
        elif path == f"/v1/courses/{COURSE}":
            self.reply(200, {"id": COURSE, "name": "Contract course", "status": "active"})
        elif path == "/v1/topics" and urllib.parse.parse_qs(parsed.query).get("course_id") == [COURSE]:
            self.reply(200, [
                {"id": TOPIC_A, "course_id": COURSE, "title": "Topic A", "status": "active"},
                {"id": TOPIC_B, "course_id": COURSE, "title": "Topic B", "status": "archived"},
            ])
        else:
            self.reply(404, {"code": "NOT_FOUND"})


def port():
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def jwt(user):
    def b64(value):
        return base64.urlsafe_b64encode(value).rstrip(b"=")
    head = b64(json.dumps({"alg": "HS256", "typ": "JWT"}).encode())
    body = b64(json.dumps({"sub": user, "iss": "users", "exp": int(time.time()) + 300,
                          "iat": int(time.time()) - 1, "roles": ["student"]}).encode())
    unsigned = head + b"." + body
    return (unsigned + b"." + b64(hmac.new(JWT_SECRET.encode(), unsigned, hashlib.sha256).digest())).decode()


def call(url, query, user=OWNER, variables=None):
    headers = {"Content-Type": "application/json", "X-User-ID": PRIVATE,
               "X-User-Roles": "admin"}
    if user:
        headers["Authorization"] = "Bearer " + jwt(user)
    request = urllib.request.Request(url, headers=headers,
                                     data=json.dumps({"query": query, "variables": variables or {}}).encode())
    try:
        with urllib.request.urlopen(request, timeout=15) as response:
            return json.load(response)
    except urllib.error.HTTPError as error:
        return json.load(error)


def ready(url, process):
    deadline = time.monotonic() + 15
    while time.monotonic() < deadline:
        if process.poll() is not None:
            raise RuntimeError("Contract process exited before readiness")
        try:
            with urllib.request.urlopen(url, timeout=0.5) as response:
                if response.status == 200:
                    return
        except (OSError, urllib.error.URLError):
            time.sleep(0.05)
    raise RuntimeError("Contract readiness timeout")


@contextlib.contextmanager
def process(args, env, log):
    child = subprocess.Popen(args, env=env, stdout=log, stderr=log)
    try:
        yield child
    finally:
        child.terminate()
        try:
            child.wait(timeout=5)
        except subprocess.TimeoutExpired:
            child.kill()
            child.wait()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--tasks-root", type=Path, default=ROOT / "tasks")
    parser.add_argument("--gateway-root", type=Path, default=ROOT / "api-gateway")
    args = parser.parse_args()
    dsn = os.environ.get("PROGRESS_TEST_DSN", "")
    if not dsn or ("component" not in dsn and "test" not in dsn):
        raise SystemExit("PROGRESS_TEST_DSN must name a dedicated test/component database")
    build_env = {**os.environ, "CGO_ENABLED": "0", "GOPROXY": "https://proxy.golang.org,direct"}
    with tempfile.TemporaryDirectory(prefix="overmindv-social-contract-") as directory:
        temp = Path(directory)
        subprocess.run(["go", "build", "-o", str(temp / "tasks-fixture"), "./tests/contract"],
                       cwd=args.tasks_root, env=build_env, check=True, timeout=180)
        subprocess.run(["go", "build", "-o", str(temp / "api-gateway"), "./cmd/api-gateway"],
                       cwd=args.gateway_root, env=build_env, check=True, timeout=180)
        server = ThreadingHTTPServer(("127.0.0.1", 0), Fixtures)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        upstream = f"http://127.0.0.1:{server.server_port}"
        tasks_port, gateway_port = port(), port()
        tasks_url, gateway_url = f"http://127.0.0.1:{tasks_port}", f"http://127.0.0.1:{gateway_port}"
        base = {k: v for k, v in os.environ.items() if k in ("PATH", "HOME", "TMPDIR", "SYSTEMROOT")}
        tasks_env = {**base, "PROGRESS_TEST_DSN": dsn, "PROGRESS_FIXTURE_ADDRESS": f"127.0.0.1:{tasks_port}"}
        gateway_env = {**base, "PORT": str(gateway_port), "JWT_SECRET": JWT_SECRET,
                       "JWT_ISSUER": "users", "TASKS_URL": tasks_url,
                       "USERS_GRAPHQL_URL": upstream + "/query", "USERS_HEALTH_URL": upstream + "/health",
                       **{name + "_TOKEN": "synthetic-fixture-token" for name in ("TASK_HUNTER", "MEDIA", "FEED")},
                       **{name + "_URL": upstream for name in ("ENTITIES", "TASK_HUNTER", "MEDIA", "FEED", "RECOMMENDATIONS")},
                       "RECOMMENDATIONS_SERVICE_TOKEN": "synthetic-fixture-token"}
        try:
            with (temp / "process.log").open("w") as log:
                with process([str(temp / "tasks-fixture")], tasks_env, log) as tasks:
                    ready(tasks_url + "/health", tasks)
                    with process([str(temp / "api-gateway")], gateway_env, log) as gateway:
                        ready(gateway_url + "/health", gateway)
                        url = gateway_url + "/graphql"
                        own_query = "{ myPracticeProgress { userId public attempts solvedTasks score daily { date attempts accepted } } }"
                        assert call(url, own_query, user=None).get("errors"), "Unauthenticated summary allowed"
                        own = call(url, own_query)["data"]["myPracticeProgress"]
                        assert own["userId"] == OWNER and own["score"] == 30 and own["solvedTasks"] == 2
                        assert own["public"] is False and len(own["daily"]) == 30
                        peer_query = f'{{ practiceProgress(userId:"{OWNER}") {{ score public }} }}'
                        assert call(url, peer_query, user=PRIVATE)["data"]["practiceProgress"] is None
                        assert call(url, "{ practiceLeaderboard { userId score } }")["data"]["practiceLeaderboard"] == []
                        mutation = "mutation { setMyPracticeStatsPublic(public:true) }"
                        assert call(url, mutation)["data"]["setMyPracticeStatsPublic"] is True
                        peer = call(url, peer_query, user=PRIVATE)["data"]["practiceProgress"]
                        assert peer == {"score": 30, "public": True}
                        rating_query = "{ practiceLeaderboard { rank userId score solvedTasks } }"
                        rating = call(url, rating_query)["data"]["practiceLeaderboard"]
                        assert rating == [{"rank": 1, "userId": OWNER, "score": 30, "solvedTasks": 2}], rating
                        course_query = f'{{ practiceLeaderboard(courseId:"{COURSE}") {{ rank userId score solvedTasks }} }}'
                        course = call(url, course_query)["data"]["practiceLeaderboard"]
                        assert course == rating, course  # archived topic contributes to course score
                        assert call(url, "mutation { setMyPracticeStatsPublic(public:false) }")["data"]["setMyPracticeStatsPublic"] is False
                        assert call(url, peer_query, user=PRIVATE)["data"]["practiceProgress"] is None
                        assert call(url, rating_query)["data"]["practiceLeaderboard"] == []
        except Exception:
            print((temp / "process.log").read_text())
            raise
        finally:
            server.shutdown()
    print("PASS: auth, privacy/revoke, full summary, active global/course ranking through real tasks + DB")


if __name__ == "__main__":
    main()
