# async-imagegen

`async-imagegen` submits image generation and image-to-image requests (OpenAI-compatible Images API,
multiple reference images supported) to a detached local Python worker. The
submit command returns a job id immediately; the worker writes status, logs, partial previews, and
the final image under a local task directory.

## Requirements

- Python 3.9 or newer
- `ASYNC_IMAGEGEN_API_KEY` (or `OPENAI_API_KEY`) and `ASYNC_IMAGEGEN_BASE_URL` set in the environment
- An account on an OpenAI-compatible Images API that offers your model (default `gpt-image-2.5-flare`)

The plugin uses an OpenAI-compatible Images API and consumes API quota and billing. It does not use Codex's
built-in image generation allowance.

## Commands

From this directory on Windows:

```text
scripts\async-imagegen.cmd --spawn --prompt "A clean technical illustration of a cable cross-section" --json
scripts\async-imagegen.cmd --status JOB --json
scripts\async-imagegen.cmd --wait JOB --json
```

The same commands work with `python scripts/async_imagegen.py` on every platform. Use
`--partial-images 1` or `--partial-images 2` to enable streamed partial previews. Partial images
are written as `partials/partial-N.*`; each partial image adds image output tokens and cost.

## Claude Code

Claude Code can use the same worker through the bundled stdio MCP adapter. On this Windows machine
it is registered at user scope as `async-imagegen`, exposing `async_imagegen_spawn`,
`async_imagegen_status`, and `async_imagegen_wait`. The adapter reuses this plugin's worker and reads
`ASYNC_IMAGEGEN_API_KEY`, `ASYNC_IMAGEGEN_BASE_URL` and `ASYNC_IMAGEGEN_MODEL` from the MCP server's `env`
block (or the process environment). Keep the key out of the repository.

Useful options include `--output-dir`, `--size`, `--quality`, `--output-format`, `--max-retries`,
`--timeout-seconds`, `--concurrency-limit`, `--base-url`, `--reference-image`, and `--json`. The default
is 4K landscape (`3840x2160`) at `high` quality with model `gpt-image-2.5-flare`, using JPEG
compression 80. Use a smaller `--size` such as `1536x1024` for faster drafts.

## Local state

State is stored in `%LOCALAPPDATA%\async-imagegen` on Windows, `$XDG_STATE_HOME/async-imagegen`
when configured, or `~/.local/state/async-imagegen` elsewhere. Set `ASYNC_IMAGEGEN_HOME` to use a
different location. Each job contains `request.json`, `state.json`, `worker.log`, and generated
images. The API key is read only from `ASYNC_IMAGEGEN_API_KEY` or `OPENAI_API_KEY`; it is never accepted as an option or written
to job files and logs.

## Development

The implementation intentionally uses only the Python standard library. Run the offline fake API
tests with:

```text
python -m unittest discover
```

## Reference images

`--reference-image PATH` (repeatable) or the MCP `reference_images` array attaches one or more local
images and switches the job to the Images API edit endpoint (`/images/edits`, multipart `image[]`).
Multiple references are supported and sent in order. Keep each around 1024 px on the long side to
avoid upstream 502s on very large uploads.

