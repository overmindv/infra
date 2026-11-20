import json
import os
import sys
from collections import deque
from http.server import BaseHTTPRequestHandler, HTTPServer

deliveries = deque(maxlen=200)
metrics = (
    'parker_ready{service="tasks"} 1\n'
    'parker_http_requests_total{service="tasks",method="GET",route="/v1/tasks",status="200"} 42\n'
    + "".join(
        f'parker_http_requests_total{{service="tasks",method="GET",route="/private-canary/{i}",status="200"}} 1\n'
        for i in range(1400)
    )
).encode()


class Receiver(BaseHTTPRequestHandler):
    def reply(self, status, body=b"", content_type="text/plain; version=0.0.4"):
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.path == "/metrics":
            self.reply(200, metrics)
        elif self.path == "/delivered":
            self.reply(200, json.dumps(list(deliveries)).encode(), "application/json")
        else:
            self.reply(404)

    def do_POST(self):
        if self.headers.get("Authorization") != f"Bearer {os.environ['FIXTURE_WEBHOOK_TOKEN']}":
            self.reply(401)
            return
        length = int(self.headers.get("Content-Length", 0))
        if self.path != "/alerts" or not 0 < length <= 65536:
            self.reply(400)
            return
        payload = json.loads(self.rfile.read(length))
        deliveries.extend(
            (
                alert["status"],
                alert["labels"]["alertname"],
                alert["labels"].get("job"),
            )
            for alert in payload["alerts"]
        )
        self.reply(200)


if __name__ == "__main__":
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 8080
    HTTPServer(("0.0.0.0", port), Receiver).serve_forever()
