# async-imagegen

`async-imagegen` submits OpenAI image generation requests to a detached local Python worker. The
submit command returns a job id immediately; the worker writes status, logs, partial previews, and
the final image under a local task directory.

## Requirements

- Python 3.9 or newer
- `OPENAI_API_KEY` set in the environment
- An OpenAI API account enabled for `gpt-image-2`

The plugin uses the OpenAI Images API and consumes API quota and billing. It does not use Codex's
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
`OPENAI_API_KEY` and `ASYNC_IMAGEGEN_BASE_URL` from the Claude Code process environment; neither value
is stored in the MCP configuration.

Useful options include `--output-dir`, `--size`, `--quality`, `--output-format`, `--max-retries`,
`--timeout-seconds`, `--concurrency-limit`, `--base-url`, and `--json`. The default is 2K landscape
(`2048x1152`) at `low` quality, using JPEG compression 80 to reduce transfer time. Use
`--quality high` only when final quality matters; use `--size 1024x1024` for the fastest drafts.

## Local state

State is stored in `%LOCALAPPDATA%\async-imagegen` on Windows, `$XDG_STATE_HOME/async-imagegen`
when configured, or `~/.local/state/async-imagegen` elsewhere. Set `ASYNC_IMAGEGEN_HOME` to use a
different location. Each job contains `request.json`, `state.json`, `worker.log`, and generated
images. The API key is read only from `OPENAI_API_KEY`; it is never accepted as an option or written
to job files and logs.

## Development

The implementation intentionally uses only the Python standard library. Run the offline fake API
tests with:

```text
python -m unittest discover
```
