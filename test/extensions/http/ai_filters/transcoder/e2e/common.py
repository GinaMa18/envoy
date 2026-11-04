"""What the end-to-end test's three clients share: arguments, results, raw HTTP and Envoy checks.

run.sh starts Envoy for one client and one backend, then runs `<client>_client.py`, which imports
this. Each client script sends a unary and a streaming request over plain HTTP, the way any client
in its API would, and checks what comes back. It then checks what Envoy sent upstream (the tap
filter's capture), Envoy's access log and stats (below) and, if the client's official Python SDK
is installed, repeats the test with the SDK.
"""
import argparse
import json
import pathlib
import sys
import time
import urllib.error
import urllib.request

# The prompt every raw request sends.
PROMPT = "In one short sentence, what is Envoy proxy?"

GEMINI_BACKENDS = ("gemini", "gemini-api")
ANTHROPIC_BACKENDS = ("anthropic", "vertex-anthropic")
# Backends whose binding moves `model` out of the body and into the path.
MODEL_IN_PATH_BACKENDS = GEMINI_BACKENDS + ("vertex-anthropic",)
ALL_BACKENDS = ("openai",) + ANTHROPIC_BACKENDS + GEMINI_BACKENDS
VERTEX_CLIENTS = ("vertex-gemini", "vertex-anthropic")
# The API version Vertex AI takes in the body, and the one the Anthropic API takes in its header.
VERTEX_ANTHROPIC_VERSION = "vertex-2023-10-16"
ANTHROPIC_VERSION = "2023-06-01"

# Envoy is on localhost, so a proxy from the environment must not get in the way.
_opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))


def parse_args():
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", required=True)
    parser.add_argument("--admin-port", required=True)
    parser.add_argument("--client", required=True)
    parser.add_argument("--backend", required=True, choices=ALL_BACKENDS)
    parser.add_argument("--model", required=True)
    # The model Envoy sends upstream instead, via `envoy.ai.model.resolved`; empty for none.
    parser.add_argument("--resolved-model", default="")
    parser.add_argument("--run-dir", required=True, type=pathlib.Path)
    parser.add_argument("--backend-host", required=True)
    # Where the backend-edge transcoder should send each request upstream.
    parser.add_argument("--unary-path", required=True)
    parser.add_argument("--stream-path", required=True)
    # The project and location a Vertex AI client puts in its path.
    parser.add_argument("--project", default="acme")
    parser.add_argument("--location", default="us-central1")
    args = parser.parse_args()
    args.upstream_model = args.resolved_model or args.model
    return args


def sampling(backend):
    """Sampling parameters for the multi-turn request.

    Anthropic's current models reject `temperature` and `top_p` in the same request.
    """
    if backend in ANTHROPIC_BACKENDS:
        return {"temperature": 0.2}
    return {"temperature": 0.2, "top_p": 0.9}


class Results:
    """Prints each check as it runs, remembers the failures, and appends each to results.jsonl."""

    def __init__(self, args):
        self.failures = []
        self.args = args
        self.log = args.run_dir / "results.jsonl"
        self.section = ""

    def _record(self, status, what, detail=""):
        with self.log.open("a") as f:
            f.write(json.dumps({
                "client": self.args.client, "backend": self.args.backend,
                "model": self.args.model, "resolved_model": self.args.resolved_model,
                "section": self.section, "check": what, "status": status, "detail": detail,
            }) + "\n")

    def start(self, section):
        print(section)
        self.section = section

    def check(self, ok, what, detail=""):
        print(("  ok    " if ok else "  FAIL  ") + what + (f": {detail}" if detail and not ok else ""))
        self._record("ok" if ok else "FAIL", what, detail)
        if not ok:
            self.failures.append(what)

    def info(self, what, detail):
        """A measurement that is reported but not judged."""
        print(f"  info  {what}: {detail}")
        self._record("info", what, detail)

    def known(self, ok, what, why):
        """A check that is a known gap: `gap` when it fails, `fixed` when it passes, never FAIL."""
        if ok:
            print(f"  fixed {what}: it works now, so make it a check")
            self._record("fixed", what, why)
        else:
            print(f"  gap   {what}: {why}")
            self._record("gap", what, why)

    def run(self, what, fn):
        """Runs `fn`, which returns a detail line or raises: a check that counts toward the result."""
        try:
            detail = fn()
        except Exception as error:  # noqa: BLE001 - report whatever the SDK raised.
            print(f"  FAIL  {what}: {describe(error)}")
            self._record("FAIL", what, describe(error))
            self.failures.append(what)
            return
        print(f"  ok    {what}")
        if detail:
            print(f"        {detail}")
        self._record("ok", what, detail or "")

    def gap(self, what, fn):
        """Runs `fn` for a known gap: its failure is expected, and does not fail the test."""
        try:
            detail = fn()
        except Exception as error:  # noqa: BLE001 - the failure is the expected outcome.
            print(f"  gap   {what}: {describe(error)}")
            self._record("gap", what, describe(error))
            return
        print(f"  fixed {what}: it works now, so make it a check")
        self._record("fixed", what, detail or "")

    def expect(self, expected, backend, what, fn):
        """Runs `fn` as a check or a known gap, as `expected[backend]` = (kind, why) says."""
        kind, why = expected[backend]
        (self.run if kind == "check" else self.gap)(f"{what} ({why})", fn)

    def exit_code(self):
        return 1 if self.failures else 0


def describe(error):
    """One line for an exception, including an HTTP error's status and the start of its message."""
    status = getattr(error, "status_code", None) or getattr(error, "code", None)
    text = str(error).strip().partition("\n")[0]
    if isinstance(status, int):
        return f"{type(error).__name__}, HTTP {status}: {text[:160]}"
    return f"{type(error).__name__}: {text[:200]}"


def post(args, name, path, body, headers):
    """Sends one request to Envoy and saves `<name>.body` and `<name>.headers` in the run directory.

    Returns (status, lower-cased headers, body text).
    """
    request = urllib.request.Request(
        f"http://127.0.0.1:{args.port}{path}", data=json.dumps(body).encode(), method="POST",
        headers={"content-type": "application/json", **headers})
    try:
        with _opener.open(request, timeout=120) as response:
            status, response_headers, data = response.status, response.headers, response.read()
    except urllib.error.HTTPError as error:
        status, response_headers, data = error.code, error.headers, error.read()
    text = data.decode(errors="replace")
    (args.run_dir / f"{name}.body").write_text(text)
    (args.run_dir / f"{name}.headers").write_text(
        f"HTTP {status}\n" + "".join(f"{k}: {v}\n" for k, v in response_headers.items()))
    return status, {k.lower(): v for k, v in response_headers.items()}, text


def sse_events(text):
    """Splits an SSE body into (event name or None, data) pairs, one per event."""
    events = []
    for block in text.replace("\r\n", "\n").split("\n\n"):
        name, data = None, []
        for line in block.split("\n"):
            if line.startswith("event:"):
                name = line[len("event:"):].strip()
            elif line.startswith("data:"):
                data.append(line[len("data:"):].strip())
        if data:
            events.append((name, "\n".join(data)))
    return events


def json_events(results, events):
    """Parses each event's data as JSON, failing a check for any that is not."""
    parsed = []
    for name, data in events:
        try:
            parsed.append((name, json.loads(data)))
        except ValueError:
            results.check(False, f"event is JSON: {data[:80]!r}")
    return parsed


def raw_json(results, text):
    try:
        return json.loads(text)
    except ValueError:
        results.check(False, f"body is JSON: {text[:120]!r}")
        return {}


def strict_json(text):
    """Parses a reply that JSON mode should have made raw JSON: no prose, no code fences."""
    return json.loads(text.strip())


def model_echo(results, args, reported, what):
    """The model a translated response names.

    The transcoder reports the model the client asked for, so a resolved model never leaks. An
    OpenAI client of an OpenAI backend is not transcoded, and OpenAI names the exact version it ran,
    so there the backend's name is reported, and a resolved model would show through.
    """
    identity = args.client == "openai" and args.backend == "openai"
    if identity:
        note = " (the IR is not transcoded, so a resolved model's name shows)" if args.resolved_model else ""
        results.info(what, f"{reported!r}{note}")
    else:
        results.check(reported == args.model, f"{what} is the client's ({args.model})",
                      repr(reported))


# ---------------------------------------------------------------------------
# What Envoy did: the tap filter's capture of the upstream request and the raw backend response,
# the access log, and the stats.

ACCESS_LOG_FIELDS = ["status", "path", "authority", "content_length", "accept_encoding",
                     "anthropic_version", "response_content_type", "response_content_encoding",
                     "model_request", "binding_location", "response_flags", "upstream_host"]


def _wait_for(predicate, seconds=10):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        value = predicate()
        if value:
            return value
        time.sleep(0.2)
    return predicate()


def access_log(args, at_least):
    """The access log's lines as dicts, once at least `at_least` requests are logged.

    Envoy flushes the log every 200 ms (run.sh's --file-flush-interval-msec).
    """
    path = args.run_dir / "access.log"

    def lines():
        if not path.exists():
            return []
        rows = [line.split("\t") for line in path.read_text().splitlines()]
        rows = [dict(zip(ACCESS_LOG_FIELDS, row)) for row in rows if len(row) == len(ACCESS_LOG_FIELDS)]
        return rows if len(rows) >= at_least else []

    return _wait_for(lines)


class Capture:
    """One request as the tap filter saw it: after the AI Protocol Manager, before the router."""

    def __init__(self, trace):
        request = trace.get("request", {})
        response = trace.get("response", {})
        self.request_headers = _headers(request)
        self.request_body = _body(request)
        self.response_headers = _headers(response)
        self.response_body = _body(response)

    @property
    def path(self):
        return self.request_headers.get(":path", "")

    def request_json(self):
        return json.loads(self.request_body)


def _headers(message):
    return {h["key"].lower(): h.get("value", "") for h in message.get("headers", [])}


# The tap copies the request headers when the stream ends, after credential_injector has set the
# key, so these are redacted the moment a file is read, and again by run.sh on exit.
CREDENTIAL_HEADERS = ("authorization", "x-goog-api-key", "x-api-key")
REDACTED = "<redacted>"


def scrub_tap_file(path):
    """Replaces credential header values in one tap file; returns the trace, or None if partial."""
    try:
        doc = json.loads(path.read_text())
        trace = doc["http_buffered_trace"]
    except (ValueError, KeyError):
        return None
    dirty = False
    for header in trace.get("request", {}).get("headers", []):
        if header.get("key", "").lower() in CREDENTIAL_HEADERS and header.get("value") != REDACTED:
            header["value"] = REDACTED
            dirty = True
    if dirty:
        path.write_text(json.dumps(doc))
    return trace


def scrub_tap_dir(tap_dir):
    for path in pathlib.Path(tap_dir).glob("req_*.json"):
        scrub_tap_file(path)


def _body(message):
    body = message.get("body", {})
    if "as_string" in body:
        return body["as_string"]
    if "as_bytes" in body:
        import base64
        return base64.b64decode(body["as_bytes"]).decode(errors="replace")
    return ""


def captures(args, at_least):
    """The tap's captures in completion order, once at least `at_least` have been written.

    A file is written when its stream ends, which can be a moment after the client has read the
    response; the trace ids in the names are random, so the files are ordered by time.
    """
    tap_dir = args.run_dir / "tap"

    def traces():
        files = sorted(tap_dir.glob("req_*.json"), key=lambda p: p.stat().st_mtime_ns)
        parsed = []
        for file in files:
            trace = scrub_tap_file(file)
            if trace is None:
                return []  # Still being written.
            parsed.append(Capture(trace))
        return parsed if len(parsed) >= at_least else []

    return _wait_for(traces)


def envoy_checks(results, args):
    """Checks what Envoy sent upstream, the access log, and the AI filters' stats.

    Call this after the two raw requests (unary, then streaming), before the SDK sends any.
    """
    stats_text = _opener.open(
        f"http://127.0.0.1:{args.admin_port}/stats?filter=ai_protocol_manager", timeout=10).read()
    (args.run_dir / "stats.txt").write_bytes(stats_text)
    log = access_log(args, at_least=2)
    taps = captures(args, at_least=2)

    results.start("Upstream request (tap, after the transcoders and before the router)")
    results.check(len(taps) >= 2, f"the tap captured both requests (got {len(taps)})")
    if len(taps) < 2:
        return
    for name, capture, expected_path, streaming in [("unary", taps[0], args.unary_path, False),
                                                    ("stream", taps[1], args.stream_path, True)]:
        (args.run_dir / f"upstream_{name}_request.json").write_text(
            json.dumps({"headers": capture.request_headers, "body": capture.request_body}, indent=1))
        (args.run_dir / f"upstream_{name}_response{'.sse' if streaming else '.json'}").write_text(
            capture.response_body)
        results.check(capture.path == expected_path,
                      f"{name}: the transcoder set :path to {expected_path}", capture.path)
        try:
            body = capture.request_json()
        except ValueError:
            results.check(False, f"{name}: the upstream body is JSON", capture.request_body[:120])
            continue
        _binding_checks(results, args, name, capture, body, streaming)
        _header_checks(results, args, name, capture)
        _raw_response_checks(results, args, name, capture, streaming)

    results.start("Access log (as the router sent the request)")
    for row in log:
        print(f"        {row['status']} {row['path']} host={row['authority']} "
              f"accept-encoding={row['accept_encoding']} anthropic-version={row['anthropic_version']} "
              f"model={row['model_request']} location={row['binding_location']} "
              f"flags={row['response_flags']}")
    results.check(len(log) >= 2, f"both requests were logged (got {len(log)})")
    for row in log[:2]:
        results.check(row["authority"] == args.backend_host,
                      f"host is {args.backend_host}", row["authority"])
        results.check(row["accept_encoding"] == "-", "accept-encoding was removed",
                      row["accept_encoding"])
        results.check(row["response_content_encoding"] in ("-", "identity"),
                      "the backend response was not compressed", row["response_content_encoding"])
        if args.client == "vertex-anthropic":
            # request_info reads Anthropic's model from the body; only the transcoder reads the
            # Vertex AI path.
            results.known(row["model_request"] == args.model,
                          f"request_info read the client's model {args.model} (got {row['model_request']})",
                          "request_info does not read the model from a Claude on Vertex AI path")
        else:
            results.check(row["model_request"] == args.model,
                          f"request_info read the client's model {args.model}", row["model_request"])
        if args.client in VERTEX_CLIENTS:
            results.check(row["binding_location"] == args.location,
                          f"binding.read kept the client's location {args.location}",
                          row["binding_location"])

    results.start("Stats")
    stats = {}
    for line in stats_text.decode().splitlines():
        name, _, value = line.partition(": ")
        if value.strip().isdigit():
            stats[name] = int(value)

    def stat(suffix):
        return sum(value for name, value in stats.items() if name.endswith(suffix))

    transcoded = stat("ai_protocol_manager.transcoder.transcoded")
    published = stat("ai_protocol_manager.request_info.published")
    usage_found = stat("ai_protocol_manager.token_usage_found")
    valid = stat("ai_protocol_manager.schema_validation.valid")
    schema_detail = (f"valid={valid}, invalid={stat('schema_validation.invalid')}, "
                     f"skipped={stat('schema_validation.skipped')}")
    if args.client == "vertex-anthropic":
        # The Anthropic schema requires `model` in the body and knows no `anthropic_version`, and
        # schema_validation runs before the transcoder reads the Vertex AI binding. run.sh sets
        # fail_open for this client so the request still goes through.
        results.known(valid == 2, f"schema_validation accepted both requests ({schema_detail})",
                      "schema_validation does not apply the Claude on Vertex AI binding")
    else:
        results.check(valid == 2, f"schema_validation accepted both requests ({schema_detail})")
    results.check(stat("ai_protocol_manager.request_parse_error") == 0, "request_parse_error is 0")
    results.check(stat("ai_protocol_manager.transcoder.failed") == 0, "transcoder.failed is 0")
    results.check(stat("ai_protocol_manager.transcoder.unresolved") == 0,
                  "transcoder.unresolved is 0")
    results.check(stat("ai_protocol_manager.transcoder.backend_defaulted") == 0,
                  "transcoder.backend_defaulted is 0 (the route named the backend)")
    results.check(stat("ai_protocol_manager.route_binding_unused") == 0,
                  "route_binding_unused is 0 (the route's binding was applied)")
    # Each request is transcoded twice, and each response at least twice (once per edge).
    results.check(transcoded >= 8, f"transcoder.transcoded is at least 8 (got {transcoded})")
    results.check(published == 2, f"request_info published one record per request (got {published})")
    results.check(usage_found == 2,
                  f"token usage was found in both raw {args.backend} responses (got {usage_found})")


def _binding_checks(results, args, name, capture, body, streaming):
    """The backend's binding put the model, the streaming mode and the API version where its
    provider takes them."""
    backend = args.backend
    if backend in MODEL_IN_PATH_BACKENDS:
        results.check("model" not in body, f"{name}: no `model` in the body (it is in the path)",
                      repr(body.get("model")))
        results.check(f"/{args.upstream_model}:" in capture.path,
                      f"{name}: the path names the model {args.upstream_model}", capture.path)
    else:
        results.check(body.get("model") == args.upstream_model,
                      f"{name}: the body's model is {args.upstream_model}", repr(body.get("model")))
    if backend in GEMINI_BACKENDS:
        results.check("stream" not in body, f"{name}: no `stream` in the body (it is in the path)")
    else:
        results.check(bool(body.get("stream")) == streaming,
                      f"{name}: the body's stream is {streaming}", repr(body.get("stream")))
    if backend == "vertex-anthropic":
        results.check(body.get("anthropic_version") == VERTEX_ANTHROPIC_VERSION,
                      f"{name}: the body carries anthropic_version {VERTEX_ANTHROPIC_VERSION}",
                      repr(body.get("anthropic_version")))
    else:
        results.check("anthropic_version" not in body,
                      f"{name}: no Vertex `anthropic_version` in the body")


def _header_checks(results, args, name, capture):
    """The binding's header edits, and the framing of the re-serialized body."""
    headers = capture.request_headers
    version = headers.get("anthropic-version")
    if args.backend == "anthropic":
        results.check(version == ANTHROPIC_VERSION,
                      f"{name}: the binding set anthropic-version {ANTHROPIC_VERSION}", repr(version))
    else:
        results.check(version is None,
                      f"{name}: no anthropic-version header for a {args.backend} backend",
                      repr(version))
    body_bytes = len(capture.request_body.encode())
    if "content-length" in headers:
        results.check(int(headers["content-length"]) == body_bytes,
                      f"{name}: content-length matches the transcoded body ({body_bytes} bytes)",
                      headers["content-length"])
    else:
        results.info(f"{name}: no content-length upstream",
                     f"transfer-encoding={headers.get('transfer-encoding')!r}, {body_bytes} bytes")
    results.check(headers.get("content-type", "").startswith("application/json"),
                  f"{name}: content-type is application/json", headers.get("content-type"))


def _raw_response_checks(results, args, name, capture, streaming):
    """The backend answered in its own API, which token usage extraction read before transcoding."""
    headers = capture.response_headers
    status = headers.get(":status")
    results.check(status == "200", f"{name}: the backend returned 200", status)
    content_type = headers.get("content-type", "")
    if streaming:
        results.check("text/event-stream" in content_type,
                      f"{name}: the raw response is text/event-stream", content_type)
        events = sse_events(capture.response_body)
        results.check(bool(events), f"{name}: the raw stream has events (got {len(events)})")
        return
    results.check("application/json" in content_type,
                  f"{name}: the raw response is application/json", content_type)
    try:
        raw = json.loads(capture.response_body)
    except ValueError:
        results.check(False, f"{name}: the raw response is JSON", capture.response_body[:120])
        return
    marker = {"openai": "choices", "anthropic": "content", "vertex-anthropic": "content",
              "gemini": "candidates", "gemini-api": "candidates"}[args.backend]
    results.check(marker in raw, f"{name}: the raw response is in {args.backend}'s format (`{marker}`)",
                  ", ".join(sorted(raw)))


def sdk_header(name, version, args):
    print()
    print(f"{name} {version}, backend {args.backend}")


# Shared request shapes, so every client asks every backend the same things.
MULTI_TURN_SYSTEM = "Follow the user's formatting exactly."
MULTI_TURN = [
    ("user", "Say 'ready'."),
    ("assistant", "ready"),
    ("user", "Now count from 1 to 10, comma-separated, nothing else."),
]
STOP = "7"
WEATHER_TOOL = {
    "name": "get_weather",
    "description": "Current weather for a city",
    "parameters": {"type": "object", "properties": {"city": {"type": "string"}},
                   "required": ["city"]},
}
TOOL_PROMPT = "What's the weather in Paris? Use the tool."
JSON_PROMPT = "Return a JSON object whose key 'ok' is true."
UNKNOWN_MODEL = "model-does-not-exist"


def cut_at_stop(content):
    assert content and STOP not in content, f"the stop sequence was ignored: {content!r}"
    return f"reply, cut at the stop sequence: {content!r}"


if __name__ == "__main__":
    # `common.py --scrub-tap DIR`: redact the key from the tap files, for run.sh's exit handler.
    if len(sys.argv) == 3 and sys.argv[1] == "--scrub-tap":
        scrub_tap_dir(sys.argv[2])
    else:
        sys.exit("usage: common.py --scrub-tap TAP_DIR")
