# Transcoder end-to-end test: every client API against every real backend

This test sends real requests through Envoy and checks what comes back. The client speaks one LLM
API, the backend another (or the same), and the AI Protocol Manager's transcoder converts between
them. Each can be:

- `gemini`: the Gemini API (`generateContent`). As a backend, this is Gemini on Vertex AI.
- `anthropic`: the Anthropic Messages API.
- `openai`: the OpenAI Chat Completions API. This is the transcoder's IR, so an OpenAI client talking
  to an OpenAI backend checks that the round trip changes nothing.

The AI Protocol Manager runs `request_info` and then two transcoder instances:

```
request:   client ─client API─▶ request_info ─▶ transcoder TO_IR ─IR─▶ transcoder FROM_IR ─backend API─▶ credential_injector ─▶ router ─▶ backend
response:  client ◀─client API─ transcoder FROM_IR ◀────IR──── transcoder TO_IR ◀─backend API─ (token usage is read here, before any transcoding)
```

## Run

Run it from an Envoy checkout that has the transcoder: this branch, or `main` once the transcoder PR
has merged.

```sh
# One client against one backend.
CLIENT=gemini BACKEND=anthropic ANTHROPIC_API_KEY=<key> test/extensions/http/ai_filters/transcoder/e2e/run.sh
# Every pair (9 runs), with every backend's key set. Each backend uses its default model.
CLIENT=all BACKEND=all VERTEX_API_KEY=<key> PROJECT_ID=<project> ANTHROPIC_API_KEY=<key> \
  OPENAI_API_KEY=<key> test/extensions/http/ai_filters/transcoder/e2e/run.sh
```

For each pair, the script:
1. builds `//source/exe:envoy-static`, once;
2. fills in `envoy.yaml` for the client and the backend, and starts Envoy on `127.0.0.1:10000`;
3. runs `<CLIENT>_client.py`, which:
   - sends one unary and one streaming request in the client's API, over plain HTTP;
   - checks the responses, then the upstream paths and the stats;
   - repeats the test with the client's official Python SDK, if it is installed;
4. prints each check, then `PASS` (exit 0) or `FAIL` (exit 1).

With `all`, it prints the pairs that passed and failed at the end.

| Variable | Default | |
|---|---|---|
| `CLIENT` | `openai` | `gemini`, `anthropic`, `openai` or `all` |
| `BACKEND` | `gemini` | `gemini`, `anthropic`, `openai` or `all` |
| `VERTEX_API_KEY`, `PROJECT_ID` | required for the `gemini` backend | Vertex AI API key, and the Google Cloud project it belongs to |
| `LOCATION` | `us-central1` | Vertex AI location |
| `ANTHROPIC_API_KEY` | required for the `anthropic` backend | Anthropic API key |
| `OPENAI_API_KEY` | required for the `openai` backend | OpenAI API key |
| `MODEL` | `gemini-2.5-flash`, `claude-haiku-4-5`, `gpt-4o-mini` | The backend's model, which the client asks for. Ignored with `all` |
| `PORT`, `ADMIN_PORT` | `10000`, `9901` | Envoy's listener and admin ports |
| `ENVOY_BIN` | unset | Use this Envoy binary instead of building one |
| `BAZEL_BUILD_FLAGS` | `--copt=-Wno-nullability-completeness` | Flags for the build |
| `RUN_DIR` | a new temp directory | Where the run's files go. With `all`, one subdirectory per pair, such as `gemini-to-openai` |
| `CA_FILE` | the system CA bundle | CA bundle for the TLS connection to the backend |
| `LOG_LEVEL` | `info` | Envoy's log level |

## Files

| File | |
|---|---|
| [`run.sh`](run.sh) | Picks the client's and the backend's settings, fills in `envoy.yaml`, starts Envoy and runs the client script. |
| [`envoy.yaml`](envoy.yaml) | The Envoy config, a template with `__PLACEHOLDERS__` for the client's and the backend's settings. |
| [`common.py`](common.py) | What the client scripts share: the results, plain HTTP requests, SSE parsing, and the access log and stats checks. |
| [`openai_client.py`](openai_client.py), [`gemini_client.py`](gemini_client.py), [`anthropic_client.py`](anthropic_client.py) | One per client API: its requests, its response checks, its SDK checks, and which of them are known gaps for each backend (`EXPECTED`). |

## What it checks

Every pair runs these checks, which count toward `PASS`/`FAIL`.

- **Unary:** HTTP 200 and a response in the client's format, translated from the backend's. It has
  the model's text, a finish reason, and token usage.
  - OpenAI: a `chat.completion`.
  - Gemini: a `GenerateContentResponse` with `candidates` and `usageMetadata`.
  - Anthropic: a `message` with `content` blocks, a `stop_reason` and `usage`.
- **Streaming:** HTTP 200, `text/event-stream`, and events in the client's format that carry text,
  a finish reason and usage.
  - OpenAI: `chat.completion.chunk` events that end with `data: [DONE]`.
  - Gemini: `GenerateContentResponse` events and no `[DONE]`. Gemini's SDK cannot parse one.
  - Anthropic: named events from `message_start` to `message_stop`, with the text in
    `content_block_delta` and the stop reason and usage in `message_delta`.
- **Upstream paths (from `access.log`):** each request went to the backend's API, whatever path the
  client called.
  - Gemini: the backend-edge transcoder chose `:generateContent` or
    `:streamGenerateContent?alt=sse` from the request, and the route mapped that path onto Vertex
    AI's project and location.
  - Anthropic: `/v1/messages`. OpenAI: `/v1/chat/completions`.
- **Stats:**
  - `transcoder.failed` and `transcoder.unresolved` are 0, and both requests were transcoded.
  - `request_info` published one record per request.
  - Token usage was found in both raw backend responses.
- **SDK, if installed:** a unary request with a system prompt, a streaming request, and a multi-turn
  request with sampling parameters, a token limit and a stop sequence. Anthropic's current models
  reject `temperature` and `top_p` together, so for the Anthropic backend it sends only
  `temperature`.

Each run leaves these files in `RUN_DIR`: the rendered `envoy.yaml`, `envoy.log`, `access.log`,
`stats.txt`, and the `unary` and `stream` response bodies and headers.

### Known gaps

Some requests are checks for some pairs and known gaps for others. A gap is reported as `gap`
without failing the test; when one is fixed, the script prints `fixed`: make it a check in that
client's `EXPECTED`.

| Client | Request | Gemini backend | Anthropic backend | OpenAI backend |
|---|---|---|---|---|
| OpenAI | tool calling | gap: `tools` are not mapped for Gemini yet | gap: `tool_use` blocks are not mapped back | check |
| OpenAI | `seed` | check | gap: Anthropic has no seed | check |
| OpenAI | `response_format` (JSON mode) | check | gap: Anthropic has no JSON mode | check |
| OpenAI | an unknown model's error | gap: errors are not transcoded | gap: errors are not transcoded | check |
| Gemini | tool calling | gap: Gemini `tools` are not mapped into the IR yet | same | same |
| Gemini | `seed` | check | gap: Anthropic has no seed | check |
| Gemini | `responseMimeType` (JSON mode) | gap: dropped on the way into the IR | same | same |
| Gemini | an unknown model's error | check | gap: errors are not transcoded | gap: errors are not transcoded |
| Anthropic | tool calling | gap: `tools` are not mapped for Gemini yet | gap: `tool_use` blocks are not mapped through the IR | gap: `tool_calls` are not mapped back to `tool_use` |
| Anthropic | an unknown model's error | gap: errors are not transcoded | check | gap: errors are not transcoded |

Anthropic's API has no seed or JSON mode, so the Anthropic client does not ask for them.

## Optional: the SDKs

The plain HTTP checks need nothing beyond `python3`. To also test with the SDK real applications
use, install it for the `python3` that runs the test:

```sh
python3 -m venv /tmp/e2e-venv && . /tmp/e2e-venv/bin/activate && pip install openai google-genai anthropic
```

An SDK parses every response into its own types and reads the stream itself, so it catches format
problems that hand-written checks could miss. Without it, that part is skipped and the test can
still pass.

## How the config works

`envoy.yaml` is a template: `run.sh` fills in the client's path and API, and the backend's host,
API, path rewrite and key header.

- **Filters:** `envoy.yaml`'s `http_filters` are the ones described above. Requests run through the
  AI filters top to bottom, and responses bottom to top. `request_info` reads the request as the
  route's request API, so it runs first, on the client's own payload and path.
- **Route:** it matches the client's API: `/v1/chat/completions`, `/v1beta/models/` or
  `/v1/messages`.
- **Path:** the route's `regex_rewrite` maps the path the backend-edge transcoder left onto the
  backend's API. For Gemini, the transcoder sets `/v1beta/models/{model}:generateContent`, or
  `:streamGenerateContent?alt=sse` when streaming, and the rewrite maps that onto
  `/v1/projects/{project}/locations/{location}/...`. A `prefix_rewrite` would not work here: the
  transcoder has already replaced the path the route matched. For Anthropic and OpenAI, the rewrite
  sets their fixed path, whatever the client called.
- **Headers:** the route removes headers the client sends:
  - `accept-encoding`, because the AI Protocol Manager cannot read a gzipped response;
  - the client's own credentials (`authorization`, `x-goog-api-key`, or `x-api-key` and
    `anthropic-version`), so they never reach the backend.

  The router removes headers after `credential_injector` has run, so the route keeps the one that
  carries the backend's key: `credential_injector` has already overwritten the client's value. For
  Anthropic, the route also sets `anthropic-version`, replacing an Anthropic client's own.
- **API key:**
  - `run.sh` writes the key only into `RUN_DIR/secrets/backend_api_key.json`, with mode 0600, and
    deletes that file on exit. The key is never in `envoy.yaml`.
  - `credential_injector` reads the key from that file over SDS, and sets it as `x-goog-api-key`
    (Gemini), `x-api-key` (Anthropic) or `authorization: Bearer` (OpenAI). SDS needs the
    bootstrap's `node` id and cluster.
  - With `LOG_LEVEL=debug`, Envoy logs request headers, including the key. Don't share that
    `envoy.log`.

## After the transcoder PR merges

This branch only adds this directory. Rebase it onto `main`, or cherry-pick its commit onto `main`,
and it applies cleanly.
