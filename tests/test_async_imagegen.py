from __future__ import annotations

import base64
import json
import os
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "async_imagegen.py"
IMAGE = base64.b64encode(b"fake-image-bytes").decode("ascii")
PARTIAL = base64.b64encode(b"fake-partial").decode("ascii")


class FakeImageHandler(BaseHTTPRequestHandler):
    failures_remaining = 0
    delay = 0
    active = 0
    max_active = 0
    last_request = None
    lock = threading.Lock()

    def log_message(self, *_args):
        return

    def do_POST(self):
        length = int(self.headers.get("Content-Length", "0"))
        request = json.loads(self.rfile.read(length))
        type(self).last_request = request
        if self.headers.get("Authorization") != "Bearer test-secret-key":
            self.send_error(401)
            return
        with self.lock:
            type(self).active += 1
            type(self).max_active = max(type(self).max_active, type(self).active)
        try:
            if type(self).failures_remaining:
                type(self).failures_remaining -= 1
                self.send_response(503)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(b'{"error":{"message":"temporary test failure"}}')
                return
            if self.delay:
                time.sleep(self.delay)
            if request.get("stream"):
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.send_header("Cache-Control", "no-cache")
                self.end_headers()
                events = [
                    {"type": "image_generation.partial_image", "partial_image_index": 0, "b64_json": PARTIAL},
                    {"type": "image_generation.partial_image", "partial_image_index": 1, "b64_json": PARTIAL},
                    {"type": "image_generation.completed", "b64_json": IMAGE},
                ]
                for event in events:
                    self.wfile.write(f"data: {json.dumps(event)}\n\n".encode())
                    self.wfile.flush()
                return
            body = json.dumps({"data": [{"b64_json": IMAGE}]}).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        finally:
            with self.lock:
                type(self).active -= 1


class AsyncImageGenTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        FakeImageHandler.failures_remaining = 0
        FakeImageHandler.delay = 0
        FakeImageHandler.active = 0
        FakeImageHandler.max_active = 0
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), FakeImageHandler)
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        cls.base_url = f"http://127.0.0.1:{cls.server.server_port}/v1"

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join(timeout=2)

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.env = os.environ.copy()
        self.env["ASYNC_IMAGEGEN_HOME"] = self.temp.name
        self.env["ASYNC_IMAGEGEN_BASE_URL"] = self.base_url
        self.env["OPENAI_API_KEY"] = "test-secret-key"

    def tearDown(self):
        self.temp.cleanup()

    def run_cli(self, *args, env=None, timeout=20):
        return subprocess.run(
            [sys.executable, str(SCRIPT), *args],
            env=env or self.env,
            text=True,
            capture_output=True,
            timeout=timeout,
        )

    def spawn(self, prompt="test image", **options):
        args = ["--spawn", "--prompt", prompt, "--json", "--max-retries", str(options.pop("max_retries", 0))]
        for key, value in options.items():
            args.extend([f"--{key.replace('_', '-')}", str(value)])
        result = self.run_cli(*args)
        self.assertEqual(result.returncode, 0, result.stderr)
        return json.loads(result.stdout)

    def wait(self, job):
        result = self.run_cli("--wait", job["id"], "--json", "--timeout", "20")
        self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
        return json.loads(result.stdout)

    def test_spawn_returns_without_api_key_and_worker_fails(self):
        env = self.env.copy()
        env.pop("OPENAI_API_KEY")
        started = time.monotonic()
        result = self.run_cli("--spawn", "--prompt", "no key", "--json", "--max-retries", "0", env=env)
        self.assertLess(time.monotonic() - started, 5)
        self.assertEqual(result.returncode, 0, result.stderr)
        job = json.loads(result.stdout)
        failed = self.run_cli("--wait", job["id"], "--json", "--timeout", "10", env=env)
        self.assertEqual(failed.returncode, 1)
        state = json.loads(failed.stdout)
        self.assertEqual(state["status"], "failed")
        self.assertIn("OPENAI_API_KEY", state["error"])

    def test_non_stream_worker_writes_final_image(self):
        job = self.spawn()
        state = self.wait(job)
        self.assertEqual(state["status"], "completed")
        self.assertEqual(FakeImageHandler.last_request["size"], "2048x1152")
        self.assertEqual(FakeImageHandler.last_request["quality"], "low")
        self.assertEqual(FakeImageHandler.last_request["output_format"], "jpeg")
        self.assertEqual(FakeImageHandler.last_request["output_compression"], 80)
        output = Path(state["output_paths"][0])
        self.assertEqual(output.read_bytes(), b"fake-image-bytes")

    def test_stream_worker_writes_partial_images(self):
        job = self.spawn(partial_images=2)
        state = self.wait(job)
        self.assertEqual(state["status"], "completed")
        self.assertEqual(len(state["partial_paths"]), 2)
        self.assertTrue(all(Path(path).read_bytes() == b"fake-partial" for path in state["partial_paths"]))
        self.assertEqual(Path(state["output_paths"][0]).read_bytes(), b"fake-image-bytes")

    def test_transient_error_is_retried(self):
        FakeImageHandler.failures_remaining = 1
        job = self.spawn(max_retries=1)
        state = self.wait(job)
        self.assertEqual(state["status"], "completed")
        self.assertEqual(state["attempt"], 2)

    def test_concurrency_limit_is_enforced(self):
        FakeImageHandler.delay = 0.25
        FakeImageHandler.max_active = 0
        first = self.spawn("one", concurrency_limit=1)
        second = self.spawn("two", concurrency_limit=1)
        self.wait(first)
        self.wait(second)
        self.assertEqual(FakeImageHandler.max_active, 1)

    def test_api_key_is_not_written_to_job_files(self):
        job = self.spawn("ordinary prompt")
        self.wait(job)
        for path in Path(self.temp.name).rglob("*"):
            if path.is_file():
                self.assertNotIn("test-secret-key", path.read_text(encoding="utf-8", errors="replace"))


if __name__ == "__main__":
    unittest.main()
