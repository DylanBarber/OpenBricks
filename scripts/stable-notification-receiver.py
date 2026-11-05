"""Private, non-relaying HTTP/SMTP sink for disposable OpenBricks smoke tests.

Run only on an isolated test network. HTTP defaults to 8025, SMTP to 2525.
GET /events returns bounded synthetic receipts; /webhook/fail-once/<id> rejects
the first attempt for each delivery ID to exercise durable retry behavior.
No credentials are persisted and no email is ever forwarded.
"""

from __future__ import annotations

import argparse
from email import policy
from email.parser import BytesParser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import socketserver
import threading
import time


class Receipts:
    def __init__(self):
        self.lock = threading.Lock()
        self.events = []
        self.attempts = {}

    def append(self, channel, payload, *, accepted=True, idempotency=None):
        with self.lock:
            self.events.append(
                {
                    "channel": channel,
                    "accepted": accepted,
                    "received_at": time.time(),
                    "idempotency_key": idempotency,
                    "payload": payload,
                }
            )
            self.events[:] = self.events[-2000:]

    def public(self):
        with self.lock:
            return list(self.events)


receipts = Receipts()


class HTTPHandler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def reply(self, code, value):
        body = json.dumps(value).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.path == "/health":
            self.reply(200, {"status": "ok", "smtp_relay": False})
        elif self.path == "/events":
            self.reply(200, {"items": receipts.public()})
        else:
            self.reply(404, {})

    def do_POST(self):
        if not self.path.startswith("/webhook/"):
            return self.reply(404, {})
        size = int(self.headers.get("Content-Length", "0"))
        if not 0 < size <= 2 * 1024**2:
            return self.reply(413, {})
        try:
            payload = json.loads(self.rfile.read(size))
        except ValueError:
            return self.reply(400, {})
        delivery = str(payload.get("delivery_id", ""))
        key = self.path + ":" + delivery
        with receipts.lock:
            attempts = receipts.attempts.get(key, 0) + 1
            receipts.attempts[key] = attempts
        accepted = not ("/fail-once/" in self.path and attempts == 1)
        receipts.append(
            "webhook",
            payload,
            accepted=accepted,
            idempotency=self.headers.get("Idempotency-Key"),
        )
        self.reply(200 if accepted else 503, {"accepted": accepted})


class SMTPHandler(socketserver.StreamRequestHandler):
    def handle(self):
        self.connection.settimeout(15)
        self.wfile.write(b"220 OpenBricks disposable receiver\r\n")
        while line := self.rfile.readline(65537):
            if len(line) > 65536:
                return
            command = line.split(b" ", 1)[0].strip().upper()
            if command in (b"EHLO", b"HELO"):
                self.wfile.write(b"250-openbricks-test-only\r\n250 SIZE 2097152\r\n")
            elif command in (b"MAIL", b"RCPT", b"RSET", b"NOOP"):
                self.wfile.write(b"250 OK\r\n")
            elif command == b"DATA":
                self.wfile.write(b"354 End with a dot\r\n")
                chunks, size = [], 0
                while part := self.rfile.readline(65537):
                    if part == b".\r\n":
                        break
                    size += len(part)
                    if size > 2 * 1024**2:
                        return
                    chunks.append(part[1:] if part.startswith(b"..") else part)
                message = BytesParser(policy=policy.default).parsebytes(
                    b"".join(chunks)
                )
                try:
                    payload = json.loads(
                        message.get_body(preferencelist=("plain",)).get_content()
                    )
                except (ValueError, AttributeError):
                    payload = {"invalid": True}
                receipts.append("smtp", payload)
                self.wfile.write(b"250 Stored locally; never relayed\r\n")
            elif command == b"QUIT":
                self.wfile.write(b"221 Bye\r\n")
                return
            else:
                self.wfile.write(b"502 Unsupported in the disposable receiver\r\n")


class SMTPServer(socketserver.ThreadingTCPServer):
    allow_reuse_address = True
    daemon_threads = True


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--http-port", type=int, default=8025)
    parser.add_argument("--smtp-port", type=int, default=2525)
    args = parser.parse_args()
    with SMTPServer(("0.0.0.0", args.smtp_port), SMTPHandler) as smtp:
        threading.Thread(target=smtp.serve_forever, daemon=True).start()
        with ThreadingHTTPServer(("0.0.0.0", args.http_port), HTTPHandler) as server:
            server.serve_forever()


if __name__ == "__main__":
    main()
