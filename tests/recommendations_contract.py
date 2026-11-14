#!/usr/bin/env python3
"""Контрактный smoke: настоящие gateway/recommender, synthetic tasks/entities HTTP.

Не использует .env, Docker, БД, production данные или внешние AI API.
Это не full-stack e2e: владельцы каталога и истории заменены контролируемым fixture.
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
TOPIC = "00000000-0000-4000-8000-000000000001"
USER = "00000000-0000-4000-8000-000000000002"
OTHER = "00000000-0000-4000-8000-000000000003"
TASKS = [
    {"id": f"10000000-0000-4000-8000-{i:012}",
     "task_version_id": f"20000000-0000-4000-8000-{i:012}",
     "topic_id": TOPIC, "status": "published", "version_number": 1,
     "title": f"Synthetic practice {i}", "difficulty": "easy",
     "task_type": "single_choice"} for i in range(1, 4)
]
JWT_SECRET = "synthetic-contract-jwt-secret-only"
SERVICE_TOKEN = "synthetic-contract-service-token-only-00000000"
QUERY = """query Practice($topics:[ID!]!, $minutes:Int!) {
 practicePlan(topicIds:$topics, minutes:$minutes) {
  policyVersion totalMinutes warnings
  items {taskId taskVersionId topicId title difficulty taskType estimatedMinutes reasonCode}
 }
}"""


class Fixtures(BaseHTTPRequestHandler):
    accepted = False
    wrong = False
    failing = False
    actors = []

    def log_message(self, *_args):
        pass

    def do_GET(self):
        path = urllib.parse.urlparse(self.path).path
        status = 200
        if path == "/v1/topics":
            body = [{"id": TOPIC, "title": "Synthetic topic", "status": "active"}]
        elif path == f"/v1/topics/{TOPIC}":
            body = {"id": TOPIC, "title": "Synthetic topic", "status": "active"}
        elif path.endswith("/prerequisites"):
            body = []
        elif path == "/v1/tasks":
            body = {"items": TASKS, "limit": 100, "offset": 0}
        elif path in ("/v1/me/submissions", "/v1/me/code-submissions"):
            actor = self.headers.get("X-User-ID")
            self.actors.append(actor)
            items = []
            if self.failing:
                status, body = 500, {"code": "INTERNAL", "message": "PRIVATE_UPSTREAM_CANARY"}
            else:
                if (self.accepted or self.wrong) and actor == USER and path == "/v1/me/submissions":
                    items = [{"id": "30000000-0000-4000-8000-000000000001",
                              "user_id": USER, "task_id": TASKS[0]["id"],
                              "task_version_id": TASKS[0]["task_version_id"],
                              "correct": self.accepted, "verdict": "accepted" if self.accepted else "wrong_answer",
                              "correct_option_ids": ["PRIVATE_ANSWER_CANARY"],
                              "created_at": "2026-11-01T12:00:00Z"}]
                body = {"items": items, "limit": 100, "offset": 0}
        elif path in ("/health", "/ready", "/healthz"):
            body = {"status": "ok"}
        else:
            status, body = 404, {"message": "fixture path not found"}
        raw = json.dumps(body).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)


def free_port():
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


def call(url, user=USER, minutes=30, query=QUERY):
    headers = {"Content-Type": "application/json", "X-User-ID": OTHER,
               "X-User-Roles": "admin"}
    if user:
        headers["Authorization"] = "Bearer " + jwt(user)
    request = urllib.request.Request(url, headers=headers, data=json.dumps({
        "query": query, "variables": {"topics": [TOPIC], "minutes": minutes}}).encode())
    try:
        with urllib.request.urlopen(request, timeout=15) as response:
            return json.load(response)
    except urllib.error.HTTPError as error:
        return json.load(error)


def ready(url, process):
    deadline = time.monotonic() + 15
    while time.monotonic() < deadline:
        if process.poll() is not None:
            raise RuntimeError("Test service exited before readiness")
        try:
            with urllib.request.urlopen(url, timeout=0.5) as response:
                if response.status == 200:
                    return
        except (OSError, urllib.error.URLError):
            time.sleep(0.05)
    raise RuntimeError("Test service readiness timeout")


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
    parser.add_argument("--gateway-root", type=Path, default=ROOT / "api-gateway")
    parser.add_argument("--recommendations-root", type=Path, default=ROOT / "recommendations")
    args = parser.parse_args()
    build_env = dict(os.environ)
    build_env["CGO_ENABLED"] = "0"
    build_env["GOPROXY"] = os.environ.get("CONTRACT_GOPROXY", "https://proxy.golang.org,direct")
    with tempfile.TemporaryDirectory(prefix="overmindv-contract-") as directory:
        temp = Path(directory)
        for name, source in (("api-gateway", args.gateway_root), ("recommendations", args.recommendations_root)):
            subprocess.run(["go", "build", "-o", str(temp / name), "./cmd/" + name],
                           cwd=source, env=build_env, check=True, timeout=180)
        server = ThreadingHTTPServer(("127.0.0.1", 0), Fixtures)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        upstream = f"http://127.0.0.1:{server.server_port}"
        reco_port, gateway_port = free_port(), free_port()
        reco_url = f"http://127.0.0.1:{reco_port}"
        gateway_url = f"http://127.0.0.1:{gateway_port}"
        # Отдельная минимальная среда: не наследуем service secrets и config с хоста.
        base = {k: v for k, v in os.environ.items() if k in ("PATH", "HOME", "TMPDIR", "SYSTEMROOT")}
        reco_env = {**base, "HTTP_ADDRESS": f"127.0.0.1:{reco_port}",
                    "RECOMMENDATIONS_SERVICE_TOKEN": SERVICE_TOKEN}
        goals_dsn = os.environ.get("CONTRACT_GOALS_TEST_DSN", "")
        if goals_dsn:
            reco_env["DATABASE_URL"] = goals_dsn
        gateway_env = {**base, "PORT": str(gateway_port), "JWT_SECRET": JWT_SECRET,
                       "JWT_ISSUER": "users", "RECOMMENDATIONS_URL": reco_url,
                       **{name + "_TOKEN": "synthetic-fixture-token" for name in ("TASK_HUNTER", "MEDIA", "FEED")},
                       "RECOMMENDATIONS_SERVICE_TOKEN": SERVICE_TOKEN,
                       "USERS_GRAPHQL_URL": upstream + "/query", "USERS_HEALTH_URL": upstream + "/health",
                       **{name + "_URL": upstream for name in ("TASKS", "ENTITIES", "TASK_HUNTER", "MEDIA", "FEED")}}
        try:
            with (temp / "process.log").open("w") as log:
                with process([str(temp / "recommendations")], reco_env, log) as reco:
                    ready(reco_url + "/ready", reco)
                    with process([str(temp / "api-gateway")], gateway_env, log) as gateway:
                        ready(gateway_url + "/health", gateway)
                        url = gateway_url + "/graphql"
                        assert call(url, user=None).get("errors"), "Unauthenticated plan allowed"
                        assert not Fixtures.actors, "History fetched before authentication"
                        assert call(url, minutes=0).get("errors"), "Invalid budget accepted"
                        alias_query = ('{ a: practicePlan(topicIds:["' + TOPIC + '"], minutes:30) {totalMinutes} '
                                       'b: practicePlan(topicIds:["' + TOPIC + '"], minutes:30) {totalMinutes} }')
                        actors_before = len(Fixtures.actors)
                        assert call(url, query=alias_query).get("errors"), "Alias fanout accepted"
                        assert len(Fixtures.actors) == actors_before, "Alias rejection fetched history"
                        outline_query = '{ learningPlan(goal:"Junior AI",minutes:30,sessionCount:3) {aiStatus topics{id} materials{id} warnings} }'
                        assert call(url, user=None, query=outline_query).get("errors"), "Unauthenticated outline allowed"
                        outline = call(url, query=outline_query)
                        assert not outline.get("errors"), "Outline contract failed"
                        assert outline["data"]["learningPlan"]["aiStatus"] == "UNAVAILABLE", "Disabled AI invented plan"
                        cold = call(url)
                        assert not cold.get("errors"), "Cold-start GraphQL request failed: " + json.dumps(cold)
                        plan = cold["data"]["practicePlan"]
                        assert plan["policyVersion"] == "practice-v3" and plan["items"], "Missing preview"
                        assert sum(x["estimatedMinutes"] for x in plan["items"]) == plan["totalMinutes"] <= 30
                        assert all(actor == USER for actor in Fixtures.actors), "Forged actor forwarded"
                        assert all(x["reasonCode"] == "START_TOPIC" for x in plan["items"])
                        if goals_dsn:
                            assert call(url, user=None, query="{ myPracticeGoal { revision } }").get("errors"), "Unauthenticated goal read allowed"
                            empty_goal = call(url, query="{ myPracticeGoal { revision } }")
                            assert empty_goal.get("data", {}).get("myPracticeGoal") is None, "Invented saved goal"
                            save_goal = call(url, query=('mutation { savePracticeGoal(topicIds:["' + TOPIC + '"], minutes:30) '
                                                         '{ topics { id title status } minutes revision } }'))
                            assert not save_goal.get("errors"), "Could not save goal"
                            saved = save_goal["data"]["savePracticeGoal"]
                            assert saved["topics"][0]["id"] == TOPIC and saved["revision"] == 1
                            assert call(url, user=OTHER, query="{ myPracticeGoal { revision } }")["data"]["myPracticeGoal"] is None, "Goal crossed actors"
                            reload_goal = call(url, query="{ myPracticeGoal { revision minutes } }")
                            assert reload_goal["data"]["myPracticeGoal"]["minutes"] == 30, "Goal not persisted"
                            replaced = call(url, query=('mutation { savePracticeGoal(topicIds:["' + TOPIC + '"], minutes:45) '
                                                        '{ minutes revision } }'))
                            assert replaced["data"]["savePracticeGoal"] == {"minutes": 45, "revision": 2}, "Goal replacement failed"
                            deleted = call(url, query="mutation { deletePracticeGoal }")
                            assert deleted["data"]["deletePracticeGoal"] is True, "Goal not deleted"
                            assert call(url, query="mutation { deletePracticeGoal }")["data"]["deletePracticeGoal"] is True, "Repeated delete failed"
                            assert call(url, query="{ myPracticeGoal { revision } }")["data"]["myPracticeGoal"] is None
                        Fixtures.wrong = True
                        retry = call(url)
                        assert not retry.get("errors"), "Wrong-answer request failed"
                        assert retry["data"]["practicePlan"]["items"][0]["taskId"] == TASKS[1]["id"], "Failed task took priority over fresh practice"
                        assert retry["data"]["practicePlan"]["items"][0]["reasonCode"] == "PRACTICE_GAP"
                        Fixtures.wrong = False
                        Fixtures.accepted = True
                        updated = call(url)
                        assert not updated.get("errors"), "Updated-history request failed"
                        assert TASKS[0]["id"] not in [x["taskId"] for x in updated["data"]["practicePlan"]["items"]]
                        other = call(url, user=OTHER)
                        assert not other.get("errors"), "Other learner request failed"
                        assert TASKS[0]["id"] in [x["taskId"] for x in other["data"]["practicePlan"]["items"]]
                        assert "PRIVATE_ANSWER_CANARY" not in json.dumps(updated), "Answer leak"
                        Fixtures.failing = True
                        failure = call(url)
                        assert failure.get("errors"), "History failure treated as cold start"
                        assert "PRIVATE_UPSTREAM_CANARY" not in json.dumps(failure), "Upstream error leaked"
                        reco.terminate()
                        reco.wait(timeout=5)
                        Fixtures.failing = False
                        assert call(url).get("errors"), "Unavailable recommender silently accepted"
            goal_scope = ", saved-goal lifecycle" if goals_dsn else ""
            print("PASS: real GraphQL → recommender contract; auth, budget, actor isolation, alternative after failure, history recalculation, failure handling" + goal_scope)
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)


if __name__ == "__main__":
    main()
