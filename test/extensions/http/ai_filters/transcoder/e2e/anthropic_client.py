"""The end-to-end test's Anthropic Messages client.

Sends Anthropic Messages requests through Envoy to the backend and checks that what comes back is
Anthropic's format: first over plain HTTP, then, if the `anthropic` package is installed, with the
official SDK. See common.py and README.md.
"""
import sys

import common

args = common.parse_args()
results = common.Results()
PATH = "/v1/messages"
# Sent the way an Anthropic client would; the route strips them before the request reaches a
# backend that is not Anthropic, and credential_injector overwrites the key for one that is.
HEADERS = {"x-api-key": "not-a-real-anthropic-key", "anthropic-version": "2023-06-01"}


def text_of(doc):
    return "".join(b.get("text") or "" for b in doc.get("content") or [] if b.get("type") == "text")


def raw_checks():
    body = {"model": args.model, "max_tokens": 1024,
            "messages": [{"role": "user", "content": common.PROMPT}]}
    unary_status, _, unary_text = common.post(args, "unary", PATH, body, HEADERS)
    stream_status, stream_headers, stream_text = common.post(
        args, "stream", PATH, {**body, "stream": True}, HEADERS)

    print(f"Unary: the {args.backend} response, as an Anthropic Message")
    results.check(unary_status == 200, f"HTTP status is 200 (got {unary_status})")
    unary = common.raw_json(results, unary_text)
    usage = unary.get("usage", {})
    results.check(unary.get("type") == "message" and unary.get("role") == "assistant",
                  "type is message, role is assistant")
    results.check(bool(text_of(unary)), "content has a text block")
    results.check(bool(unary.get("stop_reason")), "stop_reason is set")
    results.check(usage.get("input_tokens", 0) > 0 and usage.get("output_tokens", 0) > 0,
                  "usage has input and output tokens")
    print(f"        reply: {text_of(unary).strip()!r}")

    print(f"Streaming: the {args.backend} stream, as Anthropic Messages events")
    results.check(stream_status == 200, f"HTTP status is 200 (got {stream_status})")
    results.check("text/event-stream" in stream_headers.get("content-type", ""),
                  "content-type is text/event-stream")
    events = common.sse_events(stream_text)
    results.check(not any(data == "[DONE]" for _, data in events),
                  "no data: [DONE] (Anthropic streams end with message_stop)")
    parsed = common.json_events(results, [e for e in events if e[1] != "[DONE]"])
    types = [doc.get("type") for _, doc in parsed]
    results.check(bool(parsed) and all(name == doc.get("type") for name, doc in parsed),
                  f"every event's name matches its data's type (got {len(parsed)} events)")
    results.check(types[:1] == ["message_start"] and types[-1:] == ["message_stop"],
                  f"the stream runs from message_start to message_stop (got {types[:1]}..{types[-1:]})")
    text = "".join(doc.get("delta", {}).get("text") or "" for _, doc in parsed
                   if doc.get("type") == "content_block_delta")
    results.check(bool(text.strip()), "content_block_delta events carry text")
    deltas = [doc for _, doc in parsed if doc.get("type") == "message_delta"]
    results.check(any(d.get("delta", {}).get("stop_reason") for d in deltas),
                  "a message_delta has the stop_reason")
    results.check(any(d.get("usage", {}).get("output_tokens") for d in deltas),
                  "a message_delta carries output token usage")
    print(f"        text: {text.strip()!r}")


raw_checks()
common.envoy_checks(results, args)

try:
    import anthropic
except ImportError:
    print("\nAnthropic Python SDK: skipped. Install it (pip install anthropic) to also test with it.")
    sys.exit(results.exit_code())

common.sdk_header("Anthropic Python SDK", anthropic.__version__, args)
client = anthropic.Anthropic(api_key="not-a-real-anthropic-key",
                             base_url=f"http://127.0.0.1:{args.port}", max_retries=0, timeout=120)
model = args.model


def sdk_text(message):
    return "".join(b.text for b in message.content if b.type == "text")


def unary():
    r = client.messages.create(
        model=model, max_tokens=1024, system="You are terse. Answer in one sentence.",
        messages=[{"role": "user", "content": "What does Envoy proxy do?"}])
    assert r.type == "message" and sdk_text(r), "no text"
    assert r.stop_reason, "no stop_reason"
    assert r.usage.input_tokens > 0 and r.usage.output_tokens > 0, "no usage"
    return f"reply: {sdk_text(r).strip()!r}"


def streaming():
    with client.messages.stream(
            model=model, max_tokens=2048,
            messages=[{"role": "user", "content": "Write a haiku about proxies."}]) as stream:
        text = "".join(stream.text_stream)
        final = stream.get_final_message()
    assert text.strip(), "no text"
    assert final.stop_reason, "no stop_reason"
    assert final.usage.output_tokens > 0, "no usage"
    return f"stop_reason={final.stop_reason}, text: {text.strip()!r}"


def multi_turn():
    # Recent SDKs have no temperature or top_p keywords; the fields still go in the request body.
    r = client.messages.create(
        model=model, max_tokens=2048, system=common.MULTI_TURN_SYSTEM,
        stop_sequences=[common.STOP], extra_body=common.sampling(args.backend),
        messages=[{"role": role, "content": text} for role, text in common.MULTI_TURN])
    return common.cut_at_stop(sdk_text(r))


def tool_calling():
    tool = common.WEATHER_TOOL
    r = client.messages.create(
        model=model, max_tokens=1024,
        messages=[{"role": "user", "content": common.TOOL_PROMPT}],
        tools=[{"name": tool["name"], "description": tool["description"],
                "input_schema": tool["parameters"]}])
    calls = [b for b in r.content if b.type == "tool_use"]
    assert calls, "no tool_use block came back"
    assert calls[0].name == "get_weather" and "city" in calls[0].input, f"bad call: {calls[0]}"
    return f"call: {calls[0].name}({calls[0].input})"


def unknown_model():
    try:
        client.messages.create(model=common.UNKNOWN_MODEL, max_tokens=16,
                               messages=[{"role": "user", "content": "hi"}])
    except anthropic.NotFoundError as error:
        # Anthropic's error body is {"type": "error", "error": {"type", "message"}}. Any other shape
        # is the backend's own, because errors are not transcoded.
        try:
            raw = error.response.json()
        except ValueError:
            raw = {}
        inner = raw.get("error") if isinstance(raw, dict) else None
        assert (raw.get("type") == "error" and isinstance(inner, dict)
                and {"type", "message"} <= set(inner)), \
            f"not an Anthropic error body: {error.response.text[:200]}"
        return f"HTTP 404: {str(inner['message'])[:80]!r}"
    raise AssertionError("no error for a model that does not exist")


# Whether each request must work (`check`) or is a known gap (`gap`) for each backend, and why.
# Anthropic's API has no seed or JSON mode, so this client does not ask for them.
EXPECTED = {
    "tool_calling": {
        "gemini": ("gap", "tools are not mapped for Gemini yet"),
        "anthropic": ("gap", "tool_use blocks are not mapped through the IR yet"),
        "openai": ("gap", "the IR's tool_calls are not mapped back to tool_use blocks yet"),
    },
    "unknown_model": {
        "gemini": ("gap", "errors are not transcoded: Gemini's format comes back"),
        "anthropic": ("check", "the backend's error is already in Anthropic's format"),
        "openai": ("gap", "errors are not transcoded: OpenAI's format comes back"),
    },
}

results.run("unary with a system prompt: text, stop_reason and usage", unary)
results.run("streaming: the SDK assembles the final message, with usage", streaming)
results.run(f"multi-turn with {', '.join(common.sampling(args.backend))}, max_tokens and "
            "stop_sequences", multi_turn)
for name, what, fn in [
    ("tool_calling", "tool calling", tool_calling),
    ("unknown_model", "an unknown model's error, in Anthropic's format", unknown_model),
]:
    results.expect(EXPECTED[name], args.backend, what, fn)
sys.exit(results.exit_code())
