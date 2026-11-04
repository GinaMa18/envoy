# Transcoder end-to-end test: every client API against every real backend

This test sends real requests through Envoy and checks what comes back, what Envoy sent upstream,
and what its access log and stats say. The client speaks one LLM API, the backend another (or the
same), and the AI Protocol Manager's transcoder converts between them.

Clients (`CLIENT`):

| Value | API | Path the client calls |
|---|---|---|
| `openai` | OpenAI Chat Completions, the transcoder's IR | `/v1/chat/completions` |
| `anthropic` | Anthropic Messages | `/v1/messages` |
| `gemini` | Gemini API `generateContent` | `/v1beta/models/{model}:generateContent` |
| `vertex-gemini` | Gemini on Vertex AI | `/v1/projects/{p}/locations/{l}/publishers/google/models/{model}:generateContent` |
| `vertex-anthropic` | Claude on Vertex AI (`rawPredict`, model in the path, `anthropic_version` in the body) | `/v1/projects/{p}/locations/{l}/publishers/anthropic/models/{model}:rawPredict` |

Backends (`BACKEND`):

| Value | Host | API | Binding the backend-edge transcoder applies |
|---|---|---|---|
| `openai` | api.openai.com | OpenAI Chat Completions | `/v1/chat/completions` |
| `anthropic` | api.anthropic.com | Anthropic Messages | `/v1/messages`, plus the API's own `anthropic-version` header |
| `gemini` | aiplatform.googleapis.com (Gemini on Vertex AI) | Gemini | `/v1/projects/…/publishers/google/models/{model}:{generateContent\|streamGenerateContent?alt=sse}`; `model` and `stream` leave the body |
| `gemini-api` | generativelanguage.googleapis.com | Gemini | `/v1beta/models/{model}:{…}`, same body edits |
| `vertex-anthropic` | `{location}`-aiplatform.googleapis.com (Claude on Vertex AI) | Anthropic Messages | `/v1/projects/…/publishers/anthropic/models/{model}:{rawPredict\|streamRawPredict}`; `model` leaves the body, `anthropic_version` enters it, the `anthropic-version` header is removed |

`all` is `openai`, `anthropic`, `gemini` for both; any comma-separated list works.

The AI Protocol Manager runs `schema_validation`, `request_info`, and two transcoder instances:

```
request:   client ─client API─▶ schema_validation ─▶ request_info ─▶ transcoder TO_IR ─IR─▶ transcoder FROM_IR ─backend API─▶ tap ─▶ credential_injector ─▶ router ─▶ backend
response:  client ◀─client API─ transcoder FROM_IR ◀────IR──── transcoder TO_IR ◀─backend API─ tap ◀─ (token usage is read here, before any transcoding)
```

## Run

Run it from an Envoy checkout at or after `main` with the LLM binding (`envoy.ai.binding.*`).

```sh
# One client against one backend.
CLIENT=gemini BACKEND=anthropic ANTHROPIC_API_KEY=<key> test/extensions/http/ai_filters/transcoder/e2e/run.sh
# Every pair (9 runs), with every backend's key set. Each backend uses its default model.
CLIENT=all BACKEND=all VERTEX_API_KEY=<key> PROJECT_ID=<project> ANTHROPIC_API_KEY=<key> \
  OPENAI_API_KEY=<key> test/extensions/http/ai_filters/transcoder/e2e/run.sh
# Vertex AI variants, several models, and a resolved model.
CLIENT=vertex-anthropic BACKEND=vertex-anthropic MODEL=claude-haiku-4-5@20251001,claude-sonnet-4-5@20250929 \
  VERTEX_API_KEY=<key> PROJECT_ID=<project> test/extensions/http/ai_filters/transcoder/e2e/run.sh
CLIENT=openai BACKEND=gemini RESOLVED_MODEL=gemini-2.5-pro ... test/extensions/http/ai_filters/transcoder/e2e/run.sh
# Only render the configs, to review them.
DRY_RUN=1 ENVOY_BIN=/bin/true CLIENT=all BACKEND=all ... test/extensions/http/ai_filters/transcoder/e2e/run.sh
```

For each (client, backend, model), the script:
1. builds `//source/exe:envoy-static`, once;
2. fills in `envoy.yaml` for the client and the backend, and starts Envoy on `127.0.0.1:10000`;
3. runs `<CLIENT>_client.py`, which:
   - sends one unary and one streaming request in the client's API, over plain HTTP;
   - checks the responses, then the upstream requests the tap recorded, the access log and the
     stats;
   - repeats the test with the client's official Python SDK, if it is installed;
4. prints each check, then `PASS` (exit 0) or `FAIL` (exit 1).

With more than one run, it prints the pairs that passed and failed at the end, and always writes
`RUN_DIR/report.md`: one row per run with the counts and the failed checks and known gaps.

| Variable | Default | |
|---|---|---|
| `CLIENT` | `openai` | a client, a comma-separated list, or `all` |
| `BACKEND` | `gemini` | a backend, a comma-separated list, or `all` |
| `MODEL` | `gemini-2.5-flash`, `claude-haiku-4-5`, `claude-haiku-4-5@20251001`, `gpt-4o-mini` | The backend's model, which the client asks for; a comma-separated list runs once per model |
| `RESOLVED_MODEL` | unset | The model Envoy sends upstream instead, via the `envoy.ai.model.resolved` filter state; the response still names `MODEL` |
| `VERTEX_API_KEY` or `VERTEX_ACCESS_TOKEN`, `PROJECT_ID` | required for `gemini` and `vertex-anthropic` | Vertex AI API key or OAuth token, and the Google Cloud project |
| `LOCATION` | `us-central1` (`gemini`), `us-east5` (`vertex-anthropic`) | Vertex AI location |
| `VERTEX_HOST` | `aiplatform.googleapis.com`, `{LOCATION}-aiplatform.googleapis.com` | Vertex AI host |
| `GEMINI_API_KEY` | required for `gemini-api` | Gemini API key |
| `ANTHROPIC_API_KEY` | required for `anthropic` | Anthropic API key |
| `OPENAI_API_KEY` | required for `openai` | OpenAI API key |
| `CLIENT_PROJECT`, `CLIENT_LOCATION` | `PROJECT_ID` / `acme`, `LOCATION` / `us-central1` | What a Vertex AI client puts in its path |
| `SCHEMA_FAIL_OPEN` | `false` | Forward (`true`) or reject (`false`) a client payload that fails its API's schema |
| `PORT`, `ADMIN_PORT` | `10000`, `9901` | Envoy's listener and admin ports |
| `ENVOY_BIN` | unset | Use this Envoy binary instead of building one |
| `BAZEL_BUILD_FLAGS` | `--copt=-Wno-nullability-completeness` | Flags for the build |
| `RUN_DIR` | a new temp directory | Where the run's files go; one subdirectory per pair (and per model), such as `gemini-to-openai/gpt-4o-mini` |
| `CA_FILE` | the system CA bundle | CA bundle for the TLS connection to the backend |
| `LOG_LEVEL` | `info` | Envoy's log level |
| `DRY_RUN` | unset | Render `envoy.yaml` and stop |

## Files

| File | |
|---|---|
| [`run.sh`](run.sh) | Picks the client's and the backend's settings, fills in `envoy.yaml`, starts Envoy and runs the client script; loops over lists. |
| [`envoy.yaml`](envoy.yaml) | The Envoy config, a template with `__PLACEHOLDERS__` for the client's and the backend's settings. |
| [`common.py`](common.py) | What the client scripts share: the results, plain HTTP requests, SSE parsing, and the tap, access log and stats checks. |
| [`openai_client.py`](openai_client.py), [`gemini_client.py`](gemini_client.py), [`anthropic_client.py`](anthropic_client.py) | One per client API: its requests, its response checks, its SDK checks, and which of them are known gaps for each backend (`EXPECTED`). The Gemini and Anthropic ones also speak their Vertex AI layouts. |
| [`report.py`](report.py) | Renders `report.md` from the runs' `results.jsonl`. |

## What it checks

Every run makes these checks, which count toward `PASS`/`FAIL`.

- **Unary:** HTTP 200 and a response in the client's format, translated from the backend's. It has
  the model's text, a finish reason, token usage, and names the model the client asked for (an
  OpenAI client of an OpenAI backend is not transcoded, so there OpenAI's own name is reported).
  - OpenAI: a `chat.completion`.
  - Gemini: a `GenerateContentResponse` with `candidates` and `usageMetadata`.
  - Anthropic: a `message` with `content` blocks, a `stop_reason` and `usage`.
- **Streaming:** HTTP 200, `text/event-stream`, and events in the client's format that carry text,
  a finish reason and usage.
  - OpenAI: `chat.completion.chunk` events that end with `data: [DONE]`.
  - Gemini: `GenerateContentResponse` events and no `[DONE]`. Gemini's SDK cannot parse one.
  - Anthropic: named events from `message_start` to `message_stop`, with the text in
    `content_block_delta` and the stop reason and usage in `message_delta`.
- **Upstream request (from the tap):** what left the AI Protocol Manager for each request.
  - `:path` is the backend's, rendered by its binding: Vertex AI's project, location and model,
    `generateContent` or `streamGenerateContent?alt=sse`, `rawPredict` or `streamRawPredict`.
  - The body is laid out as the backend takes it: no `model` or `stream` for Gemini, no `model` and
    a Vertex `anthropic_version` for Claude on Vertex AI, `model` (the resolved one, if set) and
    `stream` for the others.
  - `anthropic-version` is set only for the Anthropic API; `content-length` matches the
    re-serialized body.
  - The raw backend response is 200, in the backend's own format, and uncompressed.
- **Access log:** the router sent each request to the backend's host, `accept-encoding` was
  removed, `request_info` read the client's model, and a Vertex AI client's location was kept in
  `envoy.ai.binding.read`.
- **Stats:**
  - `schema_validation.valid` is 2: both client payloads passed their API's schema.
  - `transcoder.failed`, `transcoder.unresolved`, `transcoder.backend_defaulted`,
    `route_binding_unused` and `request_parse_error` are 0, and both requests were transcoded.
  - `request_info` published one record per request.
  - Token usage was found in both raw backend responses.
- **SDK, if installed:** a unary request with a system prompt, a streaming request, and a multi-turn
  request with sampling parameters, a token limit and a stop sequence. Anthropic's current models
  reject `temperature` and `top_p` together, so for an Anthropic backend it sends only
  `temperature`.

Each run leaves these files in its `RUN_DIR`: the rendered `envoy.yaml`, `envoy.log`,
`access.log`, `stats.txt`, `results.jsonl`, the `unary` and `stream` response bodies and headers,
`upstream_{unary,stream}_request.json` (the transcoded request's headers and body) and
`upstream_{unary,stream}_response.{json,sse}` (the raw backend response), and the tap's own files
under `tap/`.

### Known gaps

Some requests are checks for some pairs and known gaps for others. A gap is reported as `gap`
without failing the test; when one is fixed, the script prints `fixed`: make it a check in that
client's `EXPECTED`. Claude on Vertex AI behaves as the Anthropic API, and the Gemini API as Gemini
on Vertex AI.

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

A Claude on Vertex AI client (`CLIENT=vertex-anthropic`) reports two more gaps on every run: only
the transcoder reads the Vertex binding, so `schema_validation` rejects its payload (no `model`,
an unknown `anthropic_version`; `run.sh` sets `fail_open` for this client so the request still
goes through) and `request_info` reads no model from its path.

## Optional: the SDKs

The plain HTTP checks need nothing beyond `python3`. To also test with the SDK real applications
use, install it for the `python3` that runs the test:

```sh
python3 -m venv /tmp/e2e-venv && . /tmp/e2e-venv/bin/activate && pip install openai google-genai anthropic
```

An SDK parses every response into its own types and reads the stream itself, so it catches format
problems that hand-written checks could miss. Without it, that part is skipped and the test can
still pass. The Vertex AI clients use the SDKs' Vertex modes (`genai.Client(vertexai=True, ...)`,
`anthropic.AnthropicVertex`) with placeholder credentials, pointed at Envoy.

## How the config works

`envoy.yaml` is a template: `run.sh` fills in the client's path prefix and API, and the backend's
host, API, binding and key header.

- **Filters:** `envoy.yaml`'s `http_filters` are the ones described above. Requests run through the
  AI filters top to bottom, and responses bottom to top. `schema_validation` holds the client's
  payload to its own API's schema, so it runs first; `request_info` reads the request as the route's
  request API, before any transcoding.
- **Route:** it matches the client's API: `/v1/chat/completions`, `/v1beta/models/`, `/v1/messages`
  or `/v1/projects/`, and declares the client's API as `request.llm_protocol` and the backend's as
  `response.llm_protocol`.
- **Binding:** `response.binding` says how the backend carries its API on HTTP: a URI pattern, or
  an `LLMBinding` in JSON with body and header mutations (Claude on Vertex AI). The backend-edge
  transcoder renders `:path` from it and applies its edits after the body has been validated
  against the backend's schema. The route rewrites nothing itself: a `regex_rewrite` would fight
  the transcoder for the path. Every backend names a binding, so a request leaves in the backend's
  layout whatever layout it arrived in (a Vertex AI client's path never reaches api.anthropic.com).
- **Filter state:** `set_filter_state` runs ahead of the AI Protocol Manager and sets
  `envoy.ai.model.resolved` from `RESOLVED_MODEL`, skipped when empty. The same mechanism takes
  `envoy.ai.llm_protocol.response` and `envoy.ai.binding.response` to override the route per
  request, as a model router would.
- **Headers:** the route removes headers the client sends:
  - `accept-encoding`, because the AI Protocol Manager cannot read a gzipped response;
  - the client's own credentials (`authorization`, `x-goog-api-key` or `x-api-key`), so they never
    reach the backend.

  The router removes headers after `credential_injector` has run, so the route keeps the one that
  carries the backend's key: `credential_injector` has already overwritten the client's value. API
  headers are the transcoder's: the Anthropic binding adds `anthropic-version` where absent, and
  removes an Anthropic client's own when the request leaves for another API.
- **Tap:** `envoy.filters.http.tap` sits between the AI Protocol Manager and `credential_injector`,
  so it records the transcoded request and the raw backend response. Headers the router edits later
  (`host`, `accept-encoding`, the client's credentials) are checked from the access log instead.
  The tap copies the request headers at the end of the stream, after `credential_injector` has set
  the key, so `common.py` redacts the credential headers in its files as it reads them and `run.sh`
  does so again on exit; if a run is killed before that, scrub `tap/` before sharing it.
- **API key:**
  - `run.sh` writes the key only into `RUN_DIR/secrets/backend_api_key.json`, with mode 0600, and
    deletes that file on exit. The key is never in `envoy.yaml`.
  - `credential_injector` reads the key from that file over SDS, and sets it as `x-goog-api-key`
    (Vertex AI, Gemini API), `x-api-key` (Anthropic) or `authorization: Bearer` (OpenAI, or Vertex
    AI with `VERTEX_ACCESS_TOKEN`). SDS needs the bootstrap's `node` id and cluster.
  - With `LOG_LEVEL=debug`, Envoy logs request headers, including the key. Don't share that
    `envoy.log`.
