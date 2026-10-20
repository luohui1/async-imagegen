---
name: async-imagegen
description: Submit image generation and image-to-image (multi-reference) jobs to a detached local worker through any OpenAI-compatible Images API, so the current task can continue without waiting. Use for background image creation, job status checks, partial previews, and collecting generated image paths.
---

# Async Imagegen

## Overview

Use the local `scripts/async_imagegen.py` CLI to submit a paid Images API request (OpenAI-compatible) and return
immediately. The worker persists a job id, status, logs, partial previews, and the final image so a
later command can inspect or collect the result.

## Prerequisites

Set `ASYNC_IMAGEGEN_API_KEY` (or `OPENAI_API_KEY`) and `ASYNC_IMAGEGEN_BASE_URL` in the environment
before submitting a job. The key is read by the worker only from those variables and is not accepted as
a command-line argument or written to task state. The
plugin uses API quota and billing; it does not reuse Codex's built-in image generation allowance.

## Workflow

Submit a job and keep the returned `JOB` id:

```text
python scripts/async_imagegen.py --spawn --prompt "A precise technical illustration of a cable" --json
```

Check progress without waiting:

```text
python scripts/async_imagegen.py --status JOB --json
```

Collect the final result when needed:

```text
python scripts/async_imagegen.py --wait JOB --json
```

The JSON result includes `status`, `attempt`, `output_paths`, `partial_paths`, `error`, and the job
directory. Terminal statuses are `completed` and `failed`; nonterminal statuses are `queued` and
`running`.

## Configuration

The CLI supports `--output-dir`, `--size`, `--resolution`, `--quality`, `--output-format`, `--partial-images`,
`--max-retries`, `--timeout-seconds`, `--concurrency-limit`, `--base-url`, `--reference-image`, and `--json`.
The default model is `gpt-image-2.5-flare` (override with `--model` or `ASYNC_IMAGEGEN_MODEL`), the
default size is **4K** landscape `3840x2160`, and the default quality is `high`. Pass a smaller `--size`
(for example `1536x1024`) for faster drafts. `--resolution` (1k/2k) only applies to Grok models.
Set the endpoint with `ASYNC_IMAGEGEN_BASE_URL` (an OpenAI-compatible `/v1` base URL) or `--base-url`.

Task state lives in `%LOCALAPPDATA%\async-imagegen` on Windows, `$XDG_STATE_HOME/async-imagegen`
when configured, or `~/.local/state/async-imagegen` elsewhere. Set `ASYNC_IMAGEGEN_HOME` to choose a
different task root.

## Streaming and cost

`--partial-images 0` uses the normal final-image response. Values 1-3 enable the Images API streaming
events `image_generation.partial_image` and `image_generation.completed`. Each partial image adds
image output tokens, so enable it only when earlier previews are useful.

## Resources

The implementation and Windows wrapper are in `scripts/`. Offline tests are in `tests/` and do not
call OpenAI.

## Reference images (image-to-image, multiple allowed)

Pass one or more local images with `--reference-image PATH`; repeat the flag for several (CLI), or give
`reference_images: ["C:/a.png", "C:/b.png"]` to the MCP `async_imagegen_spawn` tool. A job with
references is sent to `/images/edits` as multipart `image[]` parts, in the order given; without
references it uses `/images/generations`. Partial-image streaming is disabled for edits. Keep reference
images modest (about 1024 px on the long side): very large files can make the upstream return 502.

