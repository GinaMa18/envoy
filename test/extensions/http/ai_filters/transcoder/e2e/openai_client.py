"""The end-to-end test's OpenAI Chat Completions client.

Sends OpenAI requests through Envoy to the backend and checks that what comes back is OpenAI's
format: first over plain HTTP, then, if the `openai` package is installed, with the official SDK.
See common.py and README.md.
"""
import json
import sys

import common

args = common.parse_args()
results = common.Results()
PATH = "/v1/chat/completions"
# Sent the way an OpenAI client would; the route strips it before the request reaches the backend.
HEADERS = {"authorization": "Bearer sk-not-a-real-openai-key"}


def first_choice(doc):
    return (doc.get("choices") or [{}])[0]


def raw_checks():
    body = {"model": args.model, "messages": [{"role": "user", "content": common.PROMPT}]}
    unary_status, _, unary_text = common.post(args, "unary", PATH, body, HEADERS)
    stream_status, stream_headers, stream_text = common.post(
        args, "stream", PATH,
        {**body, "stream": True, "stream_options": {"include_usage": True}}, HEADERS)

    print(f"Unary: the {args.backend} response, as an OpenAI chat.completion")
    results.check(unary_status == 200, f"HTTP status is 200 (got {unary_status})")
    unary = common.raw_json(results, unary_text)
    message = first_choice(unary).get("message", {})
    usage = unary.get("usage", {})
    results.check(unary.get("object") == "chat.completion", "object is chat.completion")
    results.check(message.get("role") == "assistant" and bool(message.get("content")),
                  "choices[0].message has assistant text")
    results.check(bool(first_choice(unary).get("finish_reason")), "choices[0].finish_reason is set")
    results.check(usage.get("prompt_tokens", 0) > 0 and usage.get("completion_tokens", 0) > 0,
                  "usage has prompt and completion tokens")
    print(f"        reply: {(message.get('content') or '').strip()!r}")

    print(f"Streaming: the {args.backend} stream, as OpenAI chat.completion.chunk events")
    results.check(stream_status == 200, f"HTTP status is 200 (got {stream_status})")
    results.check("text/event-stream" in stream_headers.get("content-type", ""),
                  "content-type is text/event-stream")
    events = common.sse_events(stream_text)
    done = bool(events) and events[-1][1] == "[DONE]"
    terminator = {
        "gemini": "Gemini sends none; the transcoder adds it",
        "anthropic": "the transcoder writes it for Anthropic's message_stop",
        "openai": "OpenAI sends it",
    }[args.backend]
    results.check(done, f"the stream ends with data: [DONE] ({terminator})")
    chunks = [doc for _, doc in common.json_events(results, events[:-1] if done else events)]
    results.check(bool(chunks) and all(c.get("object") == "chat.completion.chunk" for c in chunks),
                  f"every event is a chat.completion.chunk (got {len(chunks)})")
    text = "".join(first_choice(c).get("delta", {}).get("content") or "" for c in chunks)
    results.check(bool(text.strip()), "the deltas carry assistant text")
    results.check(any(c.get("usage") for c in chunks), "a chunk carries usage (include_usage)")
    print(f"        text: {text.strip()!r}")


raw_checks()
common.envoy_checks(results, args)

try:
    import openai
    from openai import OpenAI
except ImportError:
    print("\nOpenAI Python SDK: skipped. Install it (pip install openai) to also test with it.")
    sys.exit(results.exit_code())

common.sdk_header("OpenAI Python SDK", openai.__version__, args)
client = OpenAI(base_url=f"http://127.0.0.1:{args.port}/v1", api_key="sk-not-a-real-openai-key",
                max_retries=0, timeout=120)
model = args.model


def unary():
    r = client.chat.completions.create(
        model=model,
        messages=[
            {"role": "system", "content": "You are terse. Answer in one sentence."},
            {"role": "user", "content": "What does Envoy proxy do?"},
        ],
    )
    choice = r.choices[0]
    assert r.object == "chat.completion" and choice.message.content, "no assistant text"
    assert choice.finish_reason, "no finish_reason"
    assert r.usage and r.usage.prompt_tokens > 0 and r.usage.completion_tokens > 0, "no usage"
    return f"reply: {choice.message.content.strip()!r}"


def streaming():
    stream = client.chat.completions.create(
        model=model,
        stream=True,
        stream_options={"include_usage": True},
        max_tokens=2048,  # The legacy name; the multi-turn check uses max_completion_tokens.
        messages=[{"role": "user", "content": "Write a haiku about proxies."}],
    )
    chunks, text, usage, finish = 0, [], None, None
    for chunk in stream:
        chunks += 1
        if chunk.choices:
            text.append(chunk.choices[0].delta.content or "")
            finish = chunk.choices[0].finish_reason or finish
        usage = chunk.usage or usage
    assert "".join(text).strip(), "no assistant text"
    assert finish, "no finish_reason"
    assert usage and usage.completion_tokens > 0, "no usage"
    return f"{chunks} chunk(s), finish_reason={finish}, text: {''.join(text).strip()!r}"


def multi_turn():
    r = client.chat.completions.create(
        model=model,
        **common.sampling(args.backend),
        max_completion_tokens=2048,
        stop=[common.STOP],
        messages=[{"role": "system", "content": common.MULTI_TURN_SYSTEM}]
        + [{"role": role, "content": text} for role, text in common.MULTI_TURN],
    )
    return common.cut_at_stop(r.choices[0].message.content)


def tool_calling():
    r = client.chat.completions.create(
        model=model,
        messages=[{"role": "user", "content": common.TOOL_PROMPT}],
        tools=[{"type": "function", "function": common.WEATHER_TOOL}],
    )
    calls = r.choices[0].message.tool_calls
    assert calls, "no tool call came back"
    arguments = json.loads(calls[0].function.arguments)
    assert calls[0].function.name == "get_weather" and "city" in arguments, f"bad call: {calls[0]}"
    return f"call: {calls[0].function.name}({calls[0].function.arguments})"


def seed():
    r = client.chat.completions.create(
        model=model, seed=42, messages=[{"role": "user", "content": "Say hi."}])
    assert r.choices[0].message.content, "no assistant text"
    return f"reply: {r.choices[0].message.content.strip()!r}"


def json_mode():
    r = client.chat.completions.create(
        model=model,
        response_format={"type": "json_object"},
        messages=[{"role": "user", "content": common.JSON_PROMPT}],
    )
    content = r.choices[0].message.content or ""
    assert common.strict_json(content).get("ok") is True, f"unexpected JSON: {content!r}"
    return f"reply: {content.strip()!r}"


def unknown_model():
    try:
        client.chat.completions.create(
            model=common.UNKNOWN_MODEL, messages=[{"role": "user", "content": "hi"}])
    except openai.NotFoundError as error:
        # OpenAI's error body is {"error": {"message", "type", "param", "code"}} and nothing else.
        # Anthropic's error object also has a message and type, so check the whole body: any other
        # shape is the backend's own, because errors are not transcoded.
        try:
            raw = error.response.json()
        except ValueError:
            raw = None
        inner = raw.get("error") if isinstance(raw, dict) else None
        assert (set(raw or {}) == {"error"} and isinstance(inner, dict)
                and {"message", "type", "param", "code"} <= set(inner)), \
            f"not an OpenAI error body: {error.response.text}"
        return f"HTTP 404: {str(inner['message'])[:80]!r}"
    raise AssertionError("no error for a model that does not exist")


# Whether each request must work (`check`) or is a known gap (`gap`) for each backend, and why.
EXPECTED = {
    "tool_calling": {
        "gemini": ("gap", "tools are not mapped for Gemini yet"),
        "anthropic": ("gap", "tools reach Anthropic, but its tool_use blocks are not mapped back"),
        "openai": ("check", "the IR is OpenAI's own API"),
    },
    "seed": {
        "gemini": ("check", "mapped to generationConfig.seed"),
        "anthropic": ("gap", "Anthropic has no seed, and rejects the unknown field"),
        "openai": ("check", "the IR is OpenAI's own API"),
    },
    "json_mode": {
        "gemini": ("check", "mapped to generationConfig.responseMimeType"),
        "anthropic": ("gap", "Anthropic has no JSON mode, and rejects the unknown field"),
        "openai": ("check", "the IR is OpenAI's own API"),
    },
    "unknown_model": {
        "gemini": ("gap", "errors are not transcoded: Gemini's format comes back"),
        "anthropic": ("gap", "errors are not transcoded: Anthropic's format comes back"),
        "openai": ("check", "OpenAI's error is already in OpenAI's format"),
    },
}

results.run("unary: parsed as a ChatCompletion with assistant text and usage", unary)
results.run("streaming: parsed as ChatCompletionChunks, with usage", streaming)
results.run(f"multi-turn with {', '.join(common.sampling(args.backend))}, "
            "max_completion_tokens and stop", multi_turn)
for name, what, fn in [
    ("tool_calling", "tool calling", tool_calling),
    ("seed", "seed", seed),
    ("json_mode", "response_format json_object", json_mode),
    ("unknown_model", "an unknown model's error, in OpenAI's format", unknown_model),
]:
    results.expect(EXPECTED[name], args.backend, what, fn)
sys.exit(results.exit_code())
