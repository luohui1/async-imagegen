#!/usr/bin/env python3
"""MCP stdio adapter for the async-imagegen local worker.

The worker remains the source of truth for job state and API calls. This adapter
only exposes spawn/status/wait as MCP tools for clients such as Claude Code.
"""

from __future__ import annotations

import json
import sys
import time
from pathlib import Path
from types import SimpleNamespace
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import async_imagegen as engine  # noqa: E402


PROTOCOL_VERSION = "2024-11-05"
SERVER_VERSION = "0.1.0"
MAX_WAIT_SECONDS = 300


def _int(value: Any, default: int, low: int, high: int) -> int:
    try:
        number = int(value)
    except (TypeError, ValueError):
        return default
    return max(low, min(number, high))


def _spawn(arguments: dict[str, Any]) -> dict[str, Any]:
    prompt = str(arguments.get("prompt") or "").strip()
    if not prompt:
        raise ValueError("prompt is required")

    output_dir = arguments.get("output_dir")
    output_format = str(arguments.get("output_format", "jpeg"))
    if output_format not in {"png", "jpeg", "webp"}:
        raise ValueError("output_format must be png, jpeg, or webp")

    args = SimpleNamespace(
        prompt=prompt,
        output_dir=str(output_dir) if output_dir else None,
        size=str(arguments.get("size", "2048x1152")),
        quality=str(arguments.get("quality", "low")),
        output_format=output_format,
        output_compression=_int(arguments.get("output_compression"), 80, 0, 100),
        partial_images=_int(arguments.get("partial_images"), 0, 0, 3),
        max_retries=_int(arguments.get("max_retries"), 2, 0, 10),
        timeout_seconds=_int(arguments.get("timeout_seconds"), 300, 1, 3600),
        concurrency_limit=_int(arguments.get("concurrency_limit"), 2, 1, 32),
        base_url=str(arguments.get("base_url") or engine.DEFAULT_BASE_URL),
    )
    state = engine.create_job(args)
    pid = engine.start_worker(state["id"])
    state = engine.load_state(state["id"])
    state["pid"] = state.get("pid") or pid
    return state


def _status(arguments: dict[str, Any]) -> dict[str, Any]:
    job_id = str(arguments.get("job_id") or "").strip()
    if not job_id:
        raise ValueError("job_id is required")
    return engine.recover_stale(job_id)


def _wait(arguments: dict[str, Any]) -> dict[str, Any]:
    job_id = str(arguments.get("job_id") or "").strip()
    if not job_id:
        raise ValueError("job_id is required")
    wait_seconds = _int(arguments.get("wait_seconds"), 30, 0, MAX_WAIT_SECONDS)
    deadline = time.monotonic() + wait_seconds
    while True:
        state = engine.recover_stale(job_id)
        if state.get("status") in engine.TERMINAL_STATUSES:
            return state
        if time.monotonic() >= deadline:
            result = dict(state)
            result["wait_timeout"] = True
            return result
        time.sleep(engine.POLL_SECONDS)


TOOLS = [
    {
        "name": "async_imagegen_spawn",
        "title": "Start an asynchronous image job",
        "description": (
            "Submit a paid gpt-image-2 image generation request to the local detached worker "
            "and return a JOB immediately. The API key is read only from OPENAI_API_KEY."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "prompt": {"type": "string", "description": "Image generation prompt."},
                "output_dir": {"type": "string", "description": "Optional final output directory."},
                "size": {"type": "string", "default": "2048x1152", "description": "Image size."},
                "quality": {"type": "string", "enum": ["low", "medium", "high", "auto"], "default": "low"},
                "output_format": {"type": "string", "enum": ["png", "jpeg", "webp"], "default": "jpeg"},
                "output_compression": {"type": "integer", "minimum": 0, "maximum": 100, "default": 80},
                "partial_images": {"type": "integer", "minimum": 0, "maximum": 3, "default": 0},
                "max_retries": {"type": "integer", "minimum": 0, "maximum": 10, "default": 2},
                "timeout_seconds": {"type": "integer", "minimum": 1, "maximum": 3600, "default": 300},
                "concurrency_limit": {"type": "integer", "minimum": 1, "maximum": 32, "default": 2},
                "base_url": {"type": "string", "description": "Optional OpenAI-compatible API base URL."},
            },
            "required": ["prompt"],
        },
    },
    {
        "name": "async_imagegen_status",
        "title": "Check an asynchronous image job",
        "description": "Read the current queued, running, completed, or failed state for a JOB.",
        "inputSchema": {
            "type": "object",
            "properties": {"job_id": {"type": "string", "description": "JOB returned by async_imagegen_spawn."}},
            "required": ["job_id"],
        },
    },
    {
        "name": "async_imagegen_wait",
        "title": "Collect an asynchronous image job",
        "description": "Wait up to wait_seconds for a JOB to finish, then return state and output paths.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "job_id": {"type": "string", "description": "JOB returned by async_imagegen_spawn."},
                "wait_seconds": {"type": "integer", "minimum": 0, "maximum": MAX_WAIT_SECONDS, "default": 30},
            },
            "required": ["job_id"],
        },
    },
]


HANDLERS = {
    "async_imagegen_spawn": _spawn,
    "async_imagegen_status": _status,
    "async_imagegen_wait": _wait,
}


def _handle(request: dict[str, Any]) -> dict[str, Any]:
    method = request.get("method")
    if method == "initialize":
        return {
            "protocolVersion": PROTOCOL_VERSION,
            "capabilities": {"tools": {}},
            "serverInfo": {"name": "async-imagegen", "version": SERVER_VERSION},
        }
    if method == "tools/list":
        return {"tools": TOOLS}
    if method == "tools/call":
        params = request.get("params") or {}
        name = params.get("name")
        handler = HANDLERS.get(name)
        if handler is None:
            raise ValueError(f"unknown tool: {name}")
        try:
            result = handler(params.get("arguments") or {})
        except Exception as exc:  # noqa: BLE001 - return tool errors to the MCP client
            return {"content": [{"type": "text", "text": str(exc)}], "isError": True}
        return {"content": [{"type": "text", "text": json.dumps(result, ensure_ascii=False)}]}
    raise ValueError(f"unknown method: {method}")


def main() -> None:
    sys.stdin.reconfigure(encoding="utf-8", errors="replace")
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            request = json.loads(line)
        except json.JSONDecodeError:
            continue
        if "id" not in request:
            continue
        try:
            response = {"jsonrpc": "2.0", "id": request["id"], "result": _handle(request)}
        except Exception as exc:  # noqa: BLE001 - report protocol-level failures
            response = {"jsonrpc": "2.0", "id": request["id"], "error": {"code": -32603, "message": str(exc)}}
        sys.stdout.write(json.dumps(response, ensure_ascii=False) + "\n")
        sys.stdout.flush()


if __name__ == "__main__":
    main()
