#!/usr/bin/env python3
"""Asynchronous OpenAI image generation jobs for the async-imagegen plugin."""

from __future__ import annotations

import argparse
import base64
import binascii
import ctypes
import gzip
import http.client
import json
import os
import secrets
import socket
import ssl
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
import zlib
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator


MODEL = os.environ.get("ASYNC_IMAGEGEN_MODEL", "grok-imagine-image-2.0")
DEFAULT_BASE_URL = os.environ.get("ASYNC_IMAGEGEN_BASE_URL", "https://cpa-ohio.turbo2c.xyz/v1")
PINNED_HOSTS = {
    "cpa-ohio.turbo2c.xyz": os.environ.get("ASYNC_IMAGEGEN_FORCE_IP", "18.117.229.92"),
}
DEFAULT_CONCURRENCY = 2
DEFAULT_TIMEOUT = 300
DEFAULT_SIZE = "2048x1152"
DEFAULT_RESOLUTION = os.environ.get("ASYNC_IMAGEGEN_RESOLUTION", "2k")
DEFAULT_QUALITY = "high"
POLL_SECONDS = 0.5
MAX_BACKOFF_SECONDS = 30
TERMINAL_STATUSES = {"completed", "failed"}


def normalize_windows_path(value: str) -> str:
    if os.name == "nt" and len(value) >= 3 and value[1] == ":":
        return value[:2] + value[2:].replace("\\\\", "\\")
    return value


class ImageGenError(RuntimeError):
    def __init__(self, message: str, *, transient: bool = False) -> None:
        super().__init__(message)
        self.transient = transient


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def task_root() -> Path:
    configured = os.environ.get("ASYNC_IMAGEGEN_HOME")
    if configured:
        root = Path(normalize_windows_path(configured)).expanduser()
    elif os.name == "nt" and os.environ.get("LOCALAPPDATA"):
        root = Path(normalize_windows_path(os.environ["LOCALAPPDATA"])) / "async-imagegen"
    elif os.environ.get("XDG_STATE_HOME"):
        root = Path(os.environ["XDG_STATE_HOME"]) / "async-imagegen"
    else:
        root = Path.home() / ".local" / "state" / "async-imagegen"
    root.mkdir(parents=True, exist_ok=True)
    (root / "jobs").mkdir(exist_ok=True)
    (root / "slots").mkdir(exist_ok=True)
    return root


def atomic_write_json(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, raw_tmp = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    tmp = Path(raw_tmp)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
            json.dump(value, handle, indent=2, ensure_ascii=True)
            handle.write("\n")
            handle.flush()
        for attempt in range(10):
            try:
                os.replace(tmp, path)
                break
            except OSError as exc:
                if os.name == "nt" and getattr(exc, "winerror", None) == 17:
                    os.replace(os.path.realpath(tmp), os.path.realpath(path))
                    break
                if os.name == "nt" and getattr(exc, "winerror", None) in (5, 32) and attempt < 9:
                    time.sleep(0.02 * (attempt + 1))
                    continue
                raise
    finally:
        try:
            tmp.unlink()
        except FileNotFoundError:
            pass


def read_json(path: Path) -> dict[str, Any]:
    try:
        with path.open("r", encoding="utf-8") as handle:
            value = json.load(handle)
    except (OSError, json.JSONDecodeError) as exc:
        raise ImageGenError(f"cannot read {path}: {exc}") from exc
    if not isinstance(value, dict):
        raise ImageGenError(f"{path} must contain a JSON object")
    return value


def job_dir(job_id: str) -> Path:
    if not job_id or Path(job_id).name != job_id or any(c not in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-_" for c in job_id):
        raise ImageGenError(f"invalid JOB: {job_id}")
    return task_root() / "jobs" / job_id


def state_path(job_id: str) -> Path:
    return job_dir(job_id) / "state.json"


def request_path(job_id: str) -> Path:
    return job_dir(job_id) / "request.json"


def load_state(job_id: str) -> dict[str, Any]:
    path = state_path(job_id)
    if not path.is_file():
        raise ImageGenError(f"unknown JOB: {job_id}")
    return read_json(path)


def save_state(job_id: str, state: dict[str, Any], **changes: Any) -> dict[str, Any]:
    state.update(changes)
    state["updated_at"] = utc_now()
    atomic_write_json(state_path(job_id), state)
    return state


def log_line(job_id: str, message: str) -> None:
    key = api_key()
    safe = message.replace(key, "[REDACTED]") if key else message
    path = job_dir(job_id) / "worker.log"
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(f"{utc_now()} {safe}\n")


def log_phase(job_id: str, phase: str, started: float, detail: str = "") -> None:
    suffix = f" {detail}" if detail else ""
    log_line(job_id, f"{phase} after {time.monotonic() - started:.3f}s{suffix}")


def pid_alive(pid: Any) -> bool:
    try:
        value = int(pid)
    except (TypeError, ValueError):
        return False
    if value <= 0:
        return False
    if value == os.getpid():
        return True
    if os.name == "nt":
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        handle = kernel32.OpenProcess(0x1000, False, value)
        if not handle:
            return False
        exit_code = ctypes.c_ulong()
        try:
            if not kernel32.GetExitCodeProcess(handle, ctypes.byref(exit_code)):
                return False
            return exit_code.value == 259
        finally:
            kernel32.CloseHandle(handle)
    try:
        os.kill(value, 0)
    except (OSError, ProcessLookupError, SystemError):
        return False
    return True


def acquire_exclusive(path: Path, payload: dict[str, Any]) -> bool:
    try:
        fd = os.open(str(path), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
    except FileExistsError:
        return False
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=True)
    except Exception:
        try:
            path.unlink()
        except FileNotFoundError:
            pass
        raise
    return True


def release_lock(path: Path) -> None:
    try:
        path.unlink()
    except FileNotFoundError:
        pass


def reap_stale_slots(root: Path) -> None:
    for path in (root / "slots").glob("slot-*.lock"):
        try:
            owner = read_json(path)
        except ImageGenError:
            release_lock(path)
            continue
        if not pid_alive(owner.get("pid")):
            release_lock(path)


def acquire_slot(job_id: str, limit: int) -> Path:
    root = task_root()
    limit = max(1, min(limit, 32))
    while True:
        reap_stale_slots(root)
        for index in range(limit):
            path = root / "slots" / f"slot-{index}.lock"
            if acquire_exclusive(path, {"job_id": job_id, "pid": os.getpid(), "created_at": utc_now()}):
                return path
        time.sleep(POLL_SECONDS)


def job_id() -> str:
    return datetime.now().strftime("%Y%m%d-%H%M%S") + "-" + secrets.token_hex(4)


def extension(output_format: str) -> str:
    return {"png": ".png", "jpeg": ".jpg", "webp": ".webp"}.get(output_format, ".png")


def windows_user_env(name: str) -> str:
    if os.name != "nt" or os.environ.get("ASYNC_IMAGEGEN_NO_USER_ENV") == "1":
        return ""
    try:
        import winreg
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, "Environment") as key:
            value, kind = winreg.QueryValueEx(key, name)
        text = str(value or "")
        if kind == winreg.REG_EXPAND_SZ:
            text = os.path.expandvars(text)
        return text
    except OSError:
        return ""


def api_key() -> str:
    return str(
        os.environ.get("ASYNC_IMAGEGEN_API_KEY")
        or os.environ.get("OPENAI_API_KEY")
        or windows_user_env("ASYNC_IMAGEGEN_API_KEY")
        or windows_user_env("OPENAI_API_KEY")
        or ""
    )


def worker_env() -> dict[str, str]:
    env = os.environ.copy()
    key = api_key()
    if key:
        env["ASYNC_IMAGEGEN_API_KEY"] = key
        env.setdefault("OPENAI_API_KEY", key)
    base = os.environ.get("ASYNC_IMAGEGEN_BASE_URL") or windows_user_env("ASYNC_IMAGEGEN_BASE_URL")
    if base:
        env.setdefault("ASYNC_IMAGEGEN_BASE_URL", base)
    model = os.environ.get("ASYNC_IMAGEGEN_MODEL") or windows_user_env("ASYNC_IMAGEGEN_MODEL")
    if model:
        env.setdefault("ASYNC_IMAGEGEN_MODEL", model)
    force_ip = os.environ.get("ASYNC_IMAGEGEN_FORCE_IP") or windows_user_env("ASYNC_IMAGEGEN_FORCE_IP") or PINNED_HOSTS.get("cpa-ohio.turbo2c.xyz", "")
    if force_ip:
        env.setdefault("ASYNC_IMAGEGEN_FORCE_IP", force_ip)
    env.setdefault("ASYNC_IMAGEGEN_RESOLUTION", grok_resolution({}))
    return env


def request_model(request: dict[str, Any]) -> str:
    return str(request.get("model") or MODEL)


def is_grok_model(model: str) -> bool:
    return "grok-imagine" in model.lower()


def grok_quality(quality: str) -> str:
    if quality == "high":
        return "medium"
    if quality in {"low", "medium", "auto"}:
        return quality
    return "medium"


def resolution_from_size(size: str) -> str:
    width, _, height = size.lower().partition("x")
    try:
        long_edge = max(int(width), int(height))
    except ValueError:
        return DEFAULT_RESOLUTION
    return "1k" if long_edge <= 1024 else "2k"


def grok_resolution(request: dict[str, Any]) -> str:
    explicit = str(request.get("resolution") or "").strip().lower()
    if explicit in {"1k", "2k"}:
        return explicit
    return DEFAULT_RESOLUTION if DEFAULT_RESOLUTION in {"1k", "2k"} else "2k"


def aspect_ratio_from_size(size: str) -> str | None:
    width, _, height = size.lower().partition("x")
    try:
        w, h = int(width), int(height)
    except ValueError:
        return "16:9"
    if w <= 0 or h <= 0:
        return "16:9"
    ratios = {
        (1, 1): "1:1",
        (16, 9): "16:9",
        (9, 16): "9:16",
        (4, 3): "4:3",
        (3, 4): "3:4",
        (3, 2): "3:2",
        (2, 3): "2:3",
        (2, 1): "2:1",
        (1, 2): "1:2",
    }
    for (rw, rh), label in ratios.items():
        if w * rh == h * rw:
            return label
    return "16:9" if w >= h else "9:16"


def output_file(job_id_value: str, request: dict[str, Any]) -> Path:
    target = Path(request["output_dir"]).expanduser()
    target.mkdir(parents=True, exist_ok=True)
    return target / f"{job_id_value}{extension(str(request['output_format']))}"


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run OpenAI image generation jobs in the background.")
    mode = parser.add_mutually_exclusive_group(required=False)
    mode.add_argument("--spawn", action="store_true", help="queue a job and return immediately")
    mode.add_argument("--status", metavar="JOB", help="show a job status")
    mode.add_argument("--wait", metavar="JOB", help="wait for a job to finish")
    parser.add_argument("--worker", metavar="JOB", help=argparse.SUPPRESS)
    parser.add_argument("--prompt", help="image prompt")
    parser.add_argument("--output-dir", help="directory for the final image")
    parser.add_argument("--size", default=DEFAULT_SIZE)
    parser.add_argument("--resolution", choices=("1k", "2k"), default=DEFAULT_RESOLUTION if DEFAULT_RESOLUTION in {"1k", "2k"} else "2k")
    parser.add_argument("--quality", choices=("low", "medium", "high", "auto"), default=DEFAULT_QUALITY)
    parser.add_argument("--model", default=MODEL)
    parser.add_argument("--output-format", choices=("png", "jpeg", "webp"), default="jpeg")
    parser.add_argument("--output-compression", type=int, default=80)
    parser.add_argument("--partial-images", type=int, choices=range(0, 4), default=0)
    parser.add_argument("--max-retries", type=int, default=2)
    parser.add_argument("--timeout-seconds", type=int, default=DEFAULT_TIMEOUT)
    parser.add_argument("--concurrency-limit", type=int, default=DEFAULT_CONCURRENCY)
    parser.add_argument("--base-url", default=os.environ.get("ASYNC_IMAGEGEN_BASE_URL", DEFAULT_BASE_URL))
    parser.add_argument("--timeout", type=float, default=0, help="wait timeout; 0 means no limit")
    parser.add_argument("--json", action="store_true", help="print one JSON object")
    args = parser.parse_args(argv)
    if args.max_retries < 0 or args.timeout_seconds <= 0 or args.concurrency_limit <= 0:
        parser.error("retry, timeout, and concurrency values must be positive")
    if args.output_compression is not None and not 0 <= args.output_compression <= 100:
        parser.error("output compression must be between 0 and 100")
    if not args.worker and not args.status and not args.wait and not args.spawn:
        parser.error("choose --spawn, --status JOB, or --wait JOB")
    if args.spawn and not args.prompt:
        parser.error("--spawn requires --prompt")
    return args


def print_result(value: dict[str, Any], as_json: bool) -> None:
    if as_json:
        print(json.dumps(value, ensure_ascii=True, separators=(",", ":")))
        return
    for key in ("id", "status", "attempt", "pid", "output_paths", "partial_paths", "error", "job_dir"):
        if key in value and value[key] not in (None, [], ""):
            label = "JOB" if key == "id" else key.upper()
            print(f"{label}: {value[key]}")


def create_job(args: argparse.Namespace) -> dict[str, Any]:
    value = job_id()
    directory = job_dir(value)
    directory.mkdir(parents=True, exist_ok=False)
    output_dir = Path(args.output_dir).expanduser().resolve() if args.output_dir else directory / "outputs"
    request: dict[str, Any] = {
        "model": str(getattr(args, "model", None) or MODEL),
        "prompt": args.prompt,
        "size": args.size,
        "resolution": str(getattr(args, "resolution", None) or grok_resolution({"size": args.size})),
        "quality": args.quality,
        "output_format": args.output_format,
        "partial_images": args.partial_images,
        "max_retries": args.max_retries,
        "timeout_seconds": args.timeout_seconds,
        "concurrency_limit": args.concurrency_limit,
        "base_url": args.base_url.rstrip("/"),
        "output_dir": str(output_dir),
        "created_at": utc_now(),
    }
    if args.output_compression is not None:
        request["output_compression"] = args.output_compression
    atomic_write_json(request_path(value), request)
    state = {
        "id": value,
        "status": "queued",
        "attempt": 0,
        "max_retries": args.max_retries,
        "created_at": request["created_at"],
        "queued_at": request["created_at"],
        "started_at": None,
        "completed_at": None,
        "updated_at": request["created_at"],
        "pid": None,
        "output_paths": [],
        "partial_paths": [],
        "error": None,
        "job_dir": str(directory),
    }
    atomic_write_json(state_path(value), state)
    return state


def worker_command(job_id_value: str) -> list[str]:
    return [sys.executable, str(Path(__file__).resolve()), "--worker", job_id_value]


def start_worker(job_id_value: str) -> int:
    directory = job_dir(job_id_value)
    log_path = directory / "worker.log"
    log_path.parent.mkdir(parents=True, exist_ok=True)
    kwargs: dict[str, Any] = {
        "cwd": str(Path(__file__).resolve().parent),
        "stdin": subprocess.DEVNULL,
        "stdout": subprocess.DEVNULL,
        "stderr": subprocess.DEVNULL,
        "close_fds": True,
        "env": worker_env(),
    }
    if os.name == "nt":
        kwargs["creationflags"] = subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP
    else:
        kwargs["start_new_session"] = True
    process = subprocess.Popen(worker_command(job_id_value), **kwargs)
    state = load_state(job_id_value)
    if state.get("status") not in TERMINAL_STATUSES:
        save_state(job_id_value, state, pid=process.pid)
    return process.pid


def recover_stale(job_id_value: str, *, start: bool = True) -> dict[str, Any]:
    state = load_state(job_id_value)
    if state.get("status") in TERMINAL_STATUSES or pid_alive(state.get("pid")):
        return state
    if state.get("status") not in {"queued", "running"}:
        return state
    request = read_json(request_path(job_id_value))
    if int(state.get("attempt", 0)) > int(request.get("max_retries", 0)):
        return save_state(job_id_value, state, status="failed", error="worker exited before completing the job")
    marker = job_dir(job_id_value) / "recovery.lock"
    if acquire_exclusive(marker, {"pid": os.getpid(), "created_at": utc_now()}):
        try:
            state = load_state(job_id_value)
            if state.get("status") not in TERMINAL_STATUSES and not pid_alive(state.get("pid")):
                state = save_state(job_id_value, state, status="queued", error="worker interruption recovered", pid=None)
                if start:
                    start_worker(job_id_value)
        finally:
            release_lock(marker)
    return load_state(job_id_value)


def api_payload(request: dict[str, Any]) -> dict[str, Any]:
    model = request_model(request)
    if is_grok_model(model):
        payload: dict[str, Any] = {
            "model": model,
            "prompt": request["prompt"],
            "n": 1,
            "response_format": "b64_json",
            "resolution": grok_resolution(request),
            "quality": grok_quality(str(request.get("quality") or DEFAULT_QUALITY)),
        }
        aspect = aspect_ratio_from_size(str(request.get("size") or DEFAULT_SIZE))
        if aspect:
            payload["aspect_ratio"] = aspect
        return payload
    payload = {
        "model": model,
        "prompt": request["prompt"],
        "size": request["size"],
        "quality": request["quality"],
        "output_format": request["output_format"],
    }
    if request.get("partial_images", 0):
        payload["stream"] = True
        payload["partial_images"] = request["partial_images"]
    if request.get("output_compression") is not None:
        payload["output_compression"] = request["output_compression"]
    return payload


def api_url(request: dict[str, Any]) -> str:
    return str(request.get("base_url", DEFAULT_BASE_URL)).rstrip("/") + "/images/generations"


def pinned_ip_for_url(url: str) -> str:
    host = urllib.parse.urlparse(url).hostname or ""
    if host not in PINNED_HOSTS and host != "cpa-ohio.turbo2c.xyz":
        return ""
    return (
        os.environ.get("ASYNC_IMAGEGEN_FORCE_IP")
        or windows_user_env("ASYNC_IMAGEGEN_FORCE_IP")
        or PINNED_HOSTS.get(host, "")
    )


class PinnedHTTPSConnection(http.client.HTTPSConnection):
    def __init__(self, host: str, *args: Any, pinned_ip: str, **kwargs: Any) -> None:
        super().__init__(host, *args, **kwargs)
        self.pinned_ip = pinned_ip

    def connect(self) -> None:
        sock = socket.create_connection((self.pinned_ip, self.port), self.timeout)
        context = self._context or ssl.create_default_context()
        self.sock = context.wrap_socket(sock, server_hostname=self.host)


class PinnedHTTPSHandler(urllib.request.HTTPSHandler):
    def __init__(self, pinned_ip: str) -> None:
        super().__init__()
        self.pinned_ip = pinned_ip

    def https_open(self, req: urllib.request.Request):  # type: ignore[override]
        def connection(host: str, **kwargs: Any) -> PinnedHTTPSConnection:
            return PinnedHTTPSConnection(host, pinned_ip=self.pinned_ip, **kwargs)

        return self.do_open(connection, req)


def api_error(body: bytes, code: int) -> ImageGenError:
    try:
        value = json.loads(body.decode("utf-8", errors="replace"))
        message = value.get("error", {}).get("message") or value.get("message") or str(value)
    except (json.JSONDecodeError, AttributeError):
        message = body.decode("utf-8", errors="replace")[:500] or f"HTTP {code}"
    return ImageGenError(f"OpenAI API HTTP {code}: {message}", transient=code == 408 or code == 409 or code == 429 or code >= 500)


class CurlResponse:
    def __init__(self, status: int, data: bytes, headers: dict[str, str]) -> None:
        self.status = status
        self._data = data
        self.headers = headers

    def read(self) -> bytes:
        return self._data

    def close(self) -> None:
        return None

    def __enter__(self) -> "CurlResponse":
        return self

    def __exit__(self, *_args: Any) -> None:
        return None


def open_api_curl(url: str, body: bytes, key: str, pinned_ip: str, timeout: int) -> CurlResponse:
    parsed = urllib.parse.urlparse(url)
    host = parsed.hostname or ""
    port = parsed.port or 443
    header_file = tempfile.NamedTemporaryFile("w+", encoding="utf-8", delete=False, suffix=".hdr")
    body_file = tempfile.NamedTemporaryFile("wb", delete=False, suffix=".bin")
    payload_file = tempfile.NamedTemporaryFile("wb", delete=False, suffix=".json")
    try:
        payload_file.write(body)
        payload_file.close()
        header_file.close()
        body_file.close()
        cmd = [
            "curl.exe",
            "-sS",
            "--http1.1",
            "-D",
            header_file.name,
            "-o",
            body_file.name,
            "-w",
            "%{http_code}",
            "--max-time",
            str(timeout),
            "--resolve",
            f"{host}:{port}:{pinned_ip}",
            "-H",
            f"Authorization: Bearer {key}",
            "-H",
            "Content-Type: application/json",
            "--data-binary",
            f"@{payload_file.name}",
            url,
        ]
        result = subprocess.run(cmd, capture_output=True, text=True)
        data = Path(body_file.name).read_bytes()
        raw_headers = Path(header_file.name).read_text(encoding="utf-8", errors="replace")
        headers: dict[str, str] = {}
        for line in raw_headers.splitlines():
            if ":" in line:
                name, value = line.split(":", 1)
                headers[name.strip().lower()] = value.strip()
        if result.returncode != 0 and not data:
            raise ImageGenError(f"OpenAI API connection failed: {result.stderr.strip() or result.stdout.strip()}", transient=True)
        try:
            status = int((result.stdout or "0").strip()[-3:])
        except ValueError:
            status = 0
        if status >= 400:
            raise api_error(data, status)
        return CurlResponse(status, data, headers)
    finally:
        for path in (header_file.name, body_file.name, payload_file.name):
            try:
                os.unlink(path)
            except OSError:
                pass


def open_api(request: dict[str, Any]) -> Any:
    key = api_key()
    if not key:
        raise ImageGenError("OPENAI_API_KEY is not set")
    body = json.dumps(api_payload(request)).encode("utf-8")
    url = api_url(request)
    http_request = urllib.request.Request(
        url,
        data=body,
        headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json", "Accept-Encoding": "gzip, deflate"},
        method="POST",
    )
    try:
        timeout = int(request["timeout_seconds"])
        pinned_ip = pinned_ip_for_url(url)
        if pinned_ip and os.name == "nt":
            return open_api_curl(url, body, key, pinned_ip, timeout)
        if pinned_ip:
            opener = urllib.request.build_opener(PinnedHTTPSHandler(pinned_ip))
            return opener.open(http_request, timeout=timeout)
        return urllib.request.urlopen(http_request, timeout=timeout)
    except urllib.error.HTTPError as exc:
        raw_error = exc.read()
        enc = exc.headers.get("Content-Encoding", "").lower()
        if enc == "gzip":
            try:
                raw_error = gzip.decompress(raw_error)
            except Exception:
                pass
        elif enc == "deflate":
            try:
                raw_error = zlib.decompress(raw_error)
            except Exception:
                pass
        raise api_error(raw_error, exc.code) from exc
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        raise ImageGenError(f"OpenAI API connection failed: {exc}", transient=True) from exc


def download_image(url: str, timeout_seconds: int) -> bytes:
    try:
        with urllib.request.urlopen(url, timeout=timeout_seconds) as response:
            data = response.read()
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        raise ImageGenError(f"failed to download generated image: {exc}", transient=True) from exc
    if not data:
        raise ImageGenError("OpenAI API returned an empty image URL")
    return data


def decode_image(value: Any) -> bytes:
    if not isinstance(value, str) or not value:
        raise ImageGenError("OpenAI API returned no base64 image")
    try:
        return base64.b64decode(value, validate=True)
    except (ValueError, binascii.Error) as exc:
        raise ImageGenError("OpenAI API returned invalid base64 image data") from exc


def event_stream(response: Any) -> Iterator[dict[str, Any]]:
    data_lines: list[str] = []
    for raw_line in response:
        line = raw_line.decode("utf-8", errors="replace").rstrip("\r\n")
        if line.startswith("data:"):
            data_lines.append(line[5:].lstrip())
        elif not line and data_lines:
            data = "\n".join(data_lines)
            data_lines.clear()
            if data != "[DONE]":
                try:
                    value = json.loads(data)
                except json.JSONDecodeError as exc:
                    raise ImageGenError("OpenAI API returned malformed streaming event", transient=True) from exc
                if isinstance(value, dict):
                    yield value
    if data_lines:
        data = "\n".join(data_lines)
        if data != "[DONE]":
            value = json.loads(data)
            if isinstance(value, dict):
                yield value


def save_stream_image(job_id_value: str, request: dict[str, Any], state: dict[str, Any], index: int, data: Any) -> None:
    path = job_dir(job_id_value) / "partials" / f"partial-{index}{extension(str(request['output_format']))}"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(decode_image(data))
    partials = list(state.get("partial_paths", []))
    if str(path) not in partials:
        partials.append(str(path))
    save_state(job_id_value, state, partial_paths=partials)


def generate(job_id_value: str, request: dict[str, Any], state: dict[str, Any]) -> dict[str, Any]:
    phase_started = time.monotonic()
    log_line(job_id_value, "HTTP request started")
    response = open_api(request)
    encoding = response.headers.get("Content-Encoding", "").lower()
    content_length = response.headers.get("Content-Length", "unknown")
    log_phase(job_id_value, "HTTP response headers received", phase_started, f"content_length={content_length} encoding={encoding or 'none'}")
    try:
        if request.get("partial_images", 0) and not is_grok_model(request_model(request)):
            final_data: Any = None
            for event in event_stream(response):
                event_type = event.get("type")
                if event_type == "image_generation.partial_image":
                    log_line(job_id_value, f"partial image received index={event.get('partial_image_index', 0)}")
                    save_stream_image(job_id_value, request, state, int(event.get("partial_image_index", 0)), event.get("b64_json"))
                elif event_type == "image_generation.completed":
                    final_data = event.get("b64_json")
                    log_phase(job_id_value, "stream completed event received", phase_started)
                elif event_type == "error":
                    raise ImageGenError(str(event.get("message") or event), transient=True)
            image_data = decode_image(final_data)
            log_phase(job_id_value, "image decoded", phase_started, f"bytes={len(image_data)}")
        else:
            try:
                raw_body = response.read()
                log_phase(job_id_value, "HTTP response body read", phase_started, f"bytes={len(raw_body)}")
                if encoding == "gzip":
                    raw_body = gzip.decompress(raw_body)
                    log_phase(job_id_value, "decompressed gzip body", phase_started, f"bytes={len(raw_body)}")
                elif encoding == "deflate":
                    raw_body = zlib.decompress(raw_body)
                    log_phase(job_id_value, "decompressed deflate body", phase_started, f"bytes={len(raw_body)}")
                value = json.loads(raw_body.decode("utf-8"))
            except json.JSONDecodeError as exc:
                raise ImageGenError("OpenAI API returned malformed JSON", transient=True) from exc
            try:
                item = value["data"][0]
                if item.get("b64_json"):
                    image_data = decode_image(item["b64_json"])
                elif item.get("url"):
                    image_data = download_image(str(item["url"]), int(request["timeout_seconds"]))
                else:
                    raise KeyError("b64_json")
            except (KeyError, IndexError, TypeError) as exc:
                raise ImageGenError("OpenAI API returned no image data") from exc
            log_phase(job_id_value, "image decoded", phase_started, f"bytes={len(image_data)}")
    finally:
        response.close()
        log_phase(job_id_value, "HTTP response closed", phase_started)
    final_path = output_file(job_id_value, request)
    final_path.write_bytes(image_data)
    log_phase(job_id_value, "image file written", phase_started, f"bytes={len(image_data)}")
    return save_state(job_id_value, state, output_paths=[str(final_path)], error=None)


def run_worker(job_id_value: str) -> int:
    request = read_json(request_path(job_id_value))
    state = load_state(job_id_value)
    slot: Path | None = None
    try:
        slot = acquire_slot(job_id_value, int(request.get("concurrency_limit", DEFAULT_CONCURRENCY)))
        state = load_state(job_id_value)
        if state.get("status") in TERMINAL_STATUSES:
            return 0
        attempt = int(state.get("attempt", 0)) + 1
        state = save_state(job_id_value, state, status="running", attempt=attempt, pid=os.getpid(), started_at=utc_now(), error=None)
        log_line(job_id_value, f"started attempt {attempt}")
        while True:
            try:
                generate(job_id_value, request, state)
                save_state(job_id_value, load_state(job_id_value), status="completed", completed_at=utc_now(), pid=None, error=None)
                log_line(job_id_value, "completed")
                return 0
            except ImageGenError as exc:
                message = str(exc)
                log_line(job_id_value, message)
                if not exc.transient or attempt > int(request.get("max_retries", 0)):
                    save_state(job_id_value, load_state(job_id_value), status="failed", completed_at=utc_now(), pid=None, error=message)
                    return 1
                delay = min(MAX_BACKOFF_SECONDS, 2 ** max(0, attempt - 1))
                save_state(job_id_value, load_state(job_id_value), status="queued", pid=os.getpid(), error=f"attempt {attempt} failed; retrying in {delay}s: {message}")
                time.sleep(delay)
                attempt += 1
                state = load_state(job_id_value)
                save_state(job_id_value, state, status="running", attempt=attempt, pid=os.getpid(), started_at=utc_now())
    except Exception as exc:
        message = str(exc)
        log_line(job_id_value, message)
        try:
            save_state(job_id_value, load_state(job_id_value), status="failed", completed_at=utc_now(), pid=None, error=message)
        except ImageGenError:
            pass
        return 1
    finally:
        if slot is not None:
            release_lock(slot)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv or sys.argv[1:])
    if args.worker:
        return run_worker(args.worker)
    if args.spawn:
        state = create_job(args)
        pid = start_worker(state["id"])
        state = load_state(state["id"])
        state["pid"] = state.get("pid") or pid
        print_result(state, args.json)
        return 0
    job = args.status or args.wait
    assert job is not None
    state = recover_stale(job)
    if args.status:
        print_result(state, args.json)
        return 0
    deadline = time.monotonic() + args.timeout if args.timeout > 0 else None
    while True:
        state = recover_stale(job)
        if state.get("status") in TERMINAL_STATUSES:
            print_result(state, args.json)
            return 0 if state.get("status") == "completed" else 1
        if deadline is not None and time.monotonic() >= deadline:
            state = dict(state)
            state["wait_timeout"] = True
            print_result(state, args.json)
            return 2
        time.sleep(POLL_SECONDS)


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        raise SystemExit(130)
