#!/usr/bin/env python3
"""Asynchronous OpenAI image generation jobs for the async-imagegen plugin."""

from __future__ import annotations

import argparse
import base64
import binascii
import ctypes
import json
import os
import secrets
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator


MODEL = "gpt-image-2"
DEFAULT_BASE_URL = "https://api.openai.com/v1"
DEFAULT_CONCURRENCY = 2
DEFAULT_TIMEOUT = 300
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
            os.fsync(handle.fileno())
        try:
            os.replace(tmp, path)
        except OSError as exc:
            # Codex's Windows app sandbox can expose LOCALAPPDATA through a
            # redirected drive alias; resolve both sides before retrying.
            if os.name != "nt" or getattr(exc, "winerror", None) != 17:
                raise
            os.replace(os.path.realpath(tmp), os.path.realpath(path))
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
    key = os.environ.get("OPENAI_API_KEY", "")
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
    parser.add_argument("--size", default="2048x1152")
    parser.add_argument("--quality", choices=("low", "medium", "high", "auto"), default="low")
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
        "model": MODEL,
        "prompt": args.prompt,
        "size": args.size,
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
    payload: dict[str, Any] = {
        "model": MODEL,
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


def api_error(body: bytes, code: int) -> ImageGenError:
    try:
        value = json.loads(body.decode("utf-8", errors="replace"))
        message = value.get("error", {}).get("message") or value.get("message") or str(value)
    except (json.JSONDecodeError, AttributeError):
        message = body.decode("utf-8", errors="replace")[:500] or f"HTTP {code}"
    return ImageGenError(f"OpenAI API HTTP {code}: {message}", transient=code == 408 or code == 409 or code == 429 or code >= 500)


def open_api(request: dict[str, Any]) -> Any:
    key = os.environ.get("OPENAI_API_KEY")
    if not key:
        raise ImageGenError("OPENAI_API_KEY is not set")
    body = json.dumps(api_payload(request)).encode("utf-8")
    http_request = urllib.request.Request(
        api_url(request),
        data=body,
        headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
        method="POST",
    )
    try:
        return urllib.request.urlopen(http_request, timeout=int(request["timeout_seconds"]))
    except urllib.error.HTTPError as exc:
        raise api_error(exc.read(), exc.code) from exc
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        raise ImageGenError(f"OpenAI API connection failed: {exc}", transient=True) from exc


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
    content_length = response.headers.get("Content-Length", "unknown")
    log_phase(job_id_value, "HTTP response headers received", phase_started, f"content_length={content_length}")
    try:
        if request.get("partial_images", 0):
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
                value = json.loads(raw_body.decode("utf-8"))
            except json.JSONDecodeError as exc:
                raise ImageGenError("OpenAI API returned malformed JSON", transient=True) from exc
            try:
                image_data = decode_image(value["data"][0]["b64_json"])
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
