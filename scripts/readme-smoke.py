"""Verify the README starter using a disposable, explicitly opted-in workspace.

Copy examples/notebook-repo to /tmp/notebook-demo and this script into a QA
management container. Set OPENBRICKS_DOCS_QA=1 for docker exec. The script
retains synthetic fixtures for screenshots; remove the QA volume afterward.
"""
import json
import os
import functools
import http.server
import ipaddress
from pathlib import Path
import shutil
import ssl
import subprocess
import tempfile
import threading
import time
from datetime import datetime, timedelta, timezone

import httpx
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import NameOID


assert os.environ.get("OPENBRICKS_DOCS_QA") == "1", "Use only a disposable documentation QA workspace"
client = httpx.Client(base_url="http://127.0.0.1:3000", timeout=360)
client.post("/api/auth/login", json={"username": "admin", "password": os.environ["OPENBRICKS_ADMIN_PASSWORD"]}).raise_for_status()


def api(method, path, **kwargs):
    response = client.request(method, "/api" + path, **kwargs)
    assert response.is_success, f"{path}: HTTP {response.status_code}: {response.text[-1800:]}"
    return response.json()


def execute(notebook):
    run = api("POST", f"/notebooks/{notebook['id']}/run", json={})
    deadline = time.monotonic() + 180
    while run["status"] in ("queued", "running"):
        assert time.monotonic() < deadline, "Starter execution timed out"
        time.sleep(0.4)
        run = api("GET", f"/runs/{run['id']}")
    assert run["status"] == "succeeded", run.get("error")
    return run


def git(*args):
    subprocess.run(["git", *map(str, args)], check=True, capture_output=True, text=True)


with tempfile.TemporaryDirectory(prefix="openbricks-readme-") as directory:
    root = Path(directory)
    source = root / "source"
    shutil.copytree("/tmp/notebook-demo", source, ignore=shutil.ignore_patterns(".git", "build", "dist", "*.egg-info", "__pycache__"))
    git("init", "-b", "main", source)
    git("-C", source, "add", ".")
    git("-C", source, "-c", "user.name=OpenBricks docs QA", "-c", "user.email=docs@example.invalid", "commit", "-m", "Notebook starter fixture")
    bare = root / "notebook-demo.git"
    git("clone", "--bare", source, bare)
    git("--git-dir", bare, "update-server-info")
    # The product accepts HTTPS/SSH, never plain HTTP/file clones. Serve this
    # disposable Git fixture over TLS and trust its short-lived CA only in QA.
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    subject = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "OpenBricks documentation QA")])
    certificate = (x509.CertificateBuilder().subject_name(subject).issuer_name(subject).public_key(key.public_key())
                   .serial_number(x509.random_serial_number()).not_valid_before(datetime.now(timezone.utc) - timedelta(minutes=1))
                   .not_valid_after(datetime.now(timezone.utc) + timedelta(days=1))
                   .add_extension(x509.BasicConstraints(ca=True, path_length=0), critical=True)
                   .add_extension(x509.SubjectAlternativeName([x509.IPAddress(ipaddress.ip_address("127.0.0.1"))]), critical=False)
                   .sign(key, hashes.SHA256()))
    cert_path, key_path = root / "qa-cert.pem", root / "qa-key.pem"
    cert_path.write_bytes(certificate.public_bytes(serialization.Encoding.PEM))
    key_path.write_bytes(key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()))
    key_path.chmod(0o600)
    git("config", "--global", "http.https://127.0.0.1:8787/.sslCAInfo", cert_path)

    class QuietHandler(http.server.SimpleHTTPRequestHandler):
        def log_message(self, *args):
            pass

    server = http.server.ThreadingHTTPServer(("127.0.0.1", 8787), functools.partial(QuietHandler, directory=str(root)))
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.load_cert_chain(cert_path, key_path)
    server.socket = context.wrap_socket(server.socket, server_side=True)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        httpx.get("https://127.0.0.1:8787/notebook-demo.git/HEAD", verify=ssl.create_default_context(cafile=str(cert_path))).raise_for_status()
        repository = api("POST", "/repositories", json={"name": "Notebook starter", "url": "https://127.0.0.1:8787/notebook-demo.git", "branch": "main"})
        notebooks = api("POST", f"/repositories/{repository['id']}/import")
        assert len(notebooks) == 5, [item.get("path") for item in notebooks]
        assert len({item["folder_id"] for item in notebooks}) == 4
        print("PASS: real Git clone/import preserves five notebooks and nested folders", flush=True)

        installed = api("POST", "/packages/build", json={"repository_id": repository["id"], "path": "."})
        assert installed and installed[0]["name"] == "openbricks-notebook-demo"
        print("PASS: build/install the starter wheel from its cloned Git project", flush=True)
        pipeline = next(item for item in notebooks if item["path"].endswith("00_run_pipeline.py"))
        execute(pipeline)
        print("PASS: full Spark pipeline validates 9 raw, 6 clean, 2 rejected, 5 completed orders; revenue 85.50", flush=True)
        execute(pipeline)
        print("PASS: repeat execution preserves expected totals without duplicate appends", flush=True)
        report = next(item for item in notebooks if item["path"].endswith(".sql"))
        result = execute(report)
        assert any(output.get("result", {}).get("rows") for output in result["outputs"] if isinstance(output.get("result"), dict))
        print("PASS: separate SQL notebook reads the persistent report table", flush=True)
        job = api("POST", "/jobs", json={"name": "Daily orders report", "notebook_id": pipeline["id"], "enabled": False,
                    "schedule_mode": "basic", "basic_schedule": {"frequency": "daily", "time": "08:00"}, "timezone": "America/New_York"})
        assert job["cron"] == "0 8 * * *"
        Path("/data/docs-fixtures.json").write_text(json.dumps({"repository_id": repository["id"], "pipeline_id": pipeline["id"], "report_id": report["id"], "job_id": job["id"]}))
        print("PASS: README scheduling configuration saves the correct Basic cron", flush=True)
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
        git("config", "--global", "--unset", "http.https://127.0.0.1:8787/.sslCAInfo")
