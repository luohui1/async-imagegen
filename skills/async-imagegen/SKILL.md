---
name: async-imagegen
description: Submit OpenAI gpt-image-2 image generation jobs to a detached local worker when the current Codex task should continue without waiting for image generation. Use for background image creation, job status checks, partial previews, and collecting generated image paths.
---

# Async Imagegen

## Overview

Use the local `scripts/async_imagegen.py` CLI to submit a paid OpenAI Images API request and return
immediately. The worker persists a job id, status, logs, partial previews, and the final image so a
later command can inspect or collect the result.

## Prerequisites

Set `OPENAI_API_KEY` in the environment before submitting a job. The key is read by the worker only
from that variable and is not accepted as a command-line argument or written to task state. The
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
`--max-retries`, `--timeout-seconds`, `--concurrency-limit`, `--base-url`, and `--json`. The default
model is `grok-imagine-image-2.0`. Grok output defaults to **2K** (`--resolution 2k`, size `2048x1152`).
Pass `--resolution 1k` only for faster drafts. Default quality is `high` (sent as Grok `medium`).
`ASYNC_IMAGEGEN_BASE_URL` defaults to `https://cpa-ohio.turbo2c.xyz/v1`.

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
