"""What the end-to-end test's three clients share: arguments, results, raw HTTP and Envoy checks.

run.sh starts Envoy for one client and one backend, then runs `<client>_client.py`, which imports
this. Each client script sends a unary and a streaming request over plain HTTP, the way any client
in its API would, and checks what comes back. It then checks Envoy's access log and stats (below)
and, if the client's official Python SDK is installed, repeats the test with the SDK.
"""
import argparse
import json
import pathlib
import time
import urllib.error
import urllib.request

# The prompt every raw request sends.
PROMPT = "In one short sentence, what is Envoy proxy?"


def parse_args():
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", required=True)
    parser.add_argument("--admin-port", required=True)
    parser.add_argument("--model", required=True)
    parser.add_argument("--backend", required=True, choices=["gemini", "anthropic", "openai"])
    parser.add_argument("--run-dir", required=True, type=pathlib.Path)
    # Where the backend-edge transcoder and the route should send each request upstream.
    parser.add_argument("--unary-path", required=True)
    parser.add_argument("--stream-path", required=True)
    return parser.parse_args()


def sampling(backend):
    """Sampling parameters for the multi-turn request.

    Anthropic's current models reject `temperature` and `top_p` in the same request.
    """
    return {"temperature": 0.2} if backend == "anthropic" else {"temperature": 0.2, "top_p": 0.9}


class Results:
    """Prints each check as it runs, and remembers the failures."""

    def __init__(self):
        self.failures = []

    def check(self, ok, what):
        print(("  ok    " if ok else "  FAIL  ") + what)
        if not ok:
            self.failures.append(what)

    def run(self, what, fn):
        """Runs `fn`, which returns a detail line or raises: a check that counts toward the result."""
        try:
            detail = fn()
        except Exception as error:  # noqa: BLE001 - report whatever the SDK raised.
            print(f"  FAIL  {what}: {describe(error)}")
            self.failures.append(what)
            return
        print(f"  ok    {what}")
        if detail:
            print(f"        {detail}")

    def gap(self, what, fn):
        """Runs `fn` for a known gap: its failure is expected, and does not fail the test."""
        try:
            fn()
        except Exception as error:  # noqa: BLE001 - the failure is the expected outcome.
            print(f"  gap   {what}: {describe(error)}")
            return
        print(f"  fixed {what}: it works now, so make it a check")

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
        with urllib.request.urlopen(request, timeout=120) as response:
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


def envoy_checks(results, args):
    """Checks the upstream paths in Envoy's access log and the AI filters' stats.

    Call this after the two raw requests, before the SDK sends any.
    """
    stats_text = urllib.request.urlopen(
        f"http://127.0.0.1:{args.admin_port}/stats?filter=ai_protocol_manager", timeout=10).read()
    (args.run_dir / "stats.txt").write_bytes(stats_text)
    # Envoy flushes the access log every 200 ms (run.sh's --file-flush-interval-msec).
    access_log = args.run_dir / "access.log"
    for _ in range(50):
        if access_log.exists() and len(access_log.read_text().splitlines()) >= 2:
            break
        time.sleep(0.2)
    access = access_log.read_text().splitlines() if access_log.exists() else []

    print("Upstream paths (access.log): the backend-edge transcoder's, then the route's rewrite")
    for line in access:
        print(f"        {line}")

    def went_to(expected):
        # A path with no query of its own may keep the client's (such as Gemini's ?alt=sse).
        paths = [line.split(" ")[1] for line in access if len(line.split(" ")) > 1]
        return any(p == expected or ("?" not in expected and p.split("?")[0] == expected)
                   for p in paths)

    results.check(went_to(args.unary_path), f"the unary request went to {args.unary_path}")
    results.check(went_to(args.stream_path), f"the streaming request went to {args.stream_path}")

    print("Stats")
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
    results.check(stat("ai_protocol_manager.transcoder.failed") == 0, "transcoder.failed is 0")
    results.check(stat("ai_protocol_manager.transcoder.unresolved") == 0,
                  "transcoder.unresolved is 0")
    # Each request is transcoded twice, and each response at least twice (once per edge).
    results.check(transcoded >= 8, f"transcoder.transcoded is at least 8 (got {transcoded})")
    results.check(published == 2, f"request_info published one record per request (got {published})")
    results.check(usage_found == 2,
                  f"token usage was found in both raw {args.backend} responses (got {usage_found})")


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
