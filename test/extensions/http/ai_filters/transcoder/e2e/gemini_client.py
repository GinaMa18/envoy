"""The end-to-end test's Gemini client.

Sends Gemini API (generateContent) requests through Envoy to the backend and checks that what comes
back is Gemini's format: first over plain HTTP, then, if the `google-genai` package is installed,
with the official Google Gen AI SDK. See common.py and README.md.
"""
import sys

import common

args = common.parse_args()
results = common.Results()
# Sent the way a Gemini API client would; the route strips it before the request reaches the backend.
HEADERS = {"x-goog-api-key": "not-a-real-gemini-key"}


def candidate(doc):
    return (doc.get("candidates") or [{}])[0]


def text_of(doc):
    return "".join(p.get("text") or "" for p in candidate(doc).get("content", {}).get("parts", []))


def raw_checks():
    body = {"contents": [{"role": "user", "parts": [{"text": common.PROMPT}]}]}
    unary_status, _, unary_text = common.post(
        args, "unary", f"/v1beta/models/{args.model}:generateContent", body, HEADERS)
    stream_status, stream_headers, stream_text = common.post(
        args, "stream", f"/v1beta/models/{args.model}:streamGenerateContent?alt=sse", body, HEADERS)

    print(f"Unary: the {args.backend} response, as a Gemini GenerateContentResponse")
    results.check(unary_status == 200, f"HTTP status is 200 (got {unary_status})")
    unary = common.raw_json(results, unary_text)
    usage = unary.get("usageMetadata", {})
    results.check(candidate(unary).get("content", {}).get("role") == "model"
                  and bool(text_of(unary)), "candidates[0].content has the model's text")
    results.check(bool(candidate(unary).get("finishReason")), "candidates[0].finishReason is set")
    results.check(usage.get("promptTokenCount", 0) > 0 and usage.get("candidatesTokenCount", 0) > 0,
                  "usageMetadata has prompt and candidates token counts")
    print(f"        reply: {text_of(unary).strip()!r}")

    print(f"Streaming: the {args.backend} stream, as Gemini GenerateContentResponse events")
    results.check(stream_status == 200, f"HTTP status is 200 (got {stream_status})")
    results.check("text/event-stream" in stream_headers.get("content-type", ""),
                  "content-type is text/event-stream")
    events = common.sse_events(stream_text)
    results.check(not any(data == "[DONE]" for _, data in events),
                  "no data: [DONE] (Gemini streams have no terminator, and its SDK cannot parse one)")
    chunks = [doc for _, doc in common.json_events(
        results, [e for e in events if e[1] != "[DONE]"])]
    results.check(bool(chunks) and all("candidates" in c or "usageMetadata" in c for c in chunks),
                  f"every event is a GenerateContentResponse (got {len(chunks)})")
    text = "".join(text_of(c) for c in chunks)
    results.check(bool(text.strip()), "the events carry the model's text")
    results.check(any(candidate(c).get("finishReason") for c in chunks), "an event has finishReason")
    results.check(any(c.get("usageMetadata", {}).get("candidatesTokenCount") for c in chunks),
                  "an event carries usageMetadata")
    print(f"        text: {text.strip()!r}")


raw_checks()
common.envoy_checks(results, args)

try:
    from google import genai
    from google.genai import errors, types
except ImportError:
    print("\nGoogle Gen AI SDK: skipped. Install it (pip install google-genai) to also test with it.")
    sys.exit(results.exit_code())

common.sdk_header("Google Gen AI SDK", genai.__version__, args)
client = genai.Client(
    api_key="not-a-real-gemini-key",
    http_options=types.HttpOptions(base_url=f"http://127.0.0.1:{args.port}", api_version="v1beta",
                                   timeout=120_000))
model = args.model


def unary():
    r = client.models.generate_content(
        model=model, contents="What does Envoy proxy do?",
        config=types.GenerateContentConfig(system_instruction="You are terse. Answer in one sentence."))
    assert r.text, "no text"
    assert r.candidates[0].finish_reason, "no finish_reason"
    u = r.usage_metadata
    assert u and u.prompt_token_count and u.candidates_token_count, "no usage_metadata"
    return f"reply: {r.text.strip()!r}"


def streaming():
    text, usage, finish, chunks = [], None, None, 0
    for chunk in client.models.generate_content_stream(
            model=model, contents="Write a haiku about proxies.",
            config=types.GenerateContentConfig(max_output_tokens=2048)):
        chunks += 1
        text.append(chunk.text or "")
        if chunk.candidates and chunk.candidates[0].finish_reason:
            finish = chunk.candidates[0].finish_reason
        usage = chunk.usage_metadata or usage
    assert "".join(text).strip(), "no text"
    assert finish, "no finish_reason"
    assert usage and usage.candidates_token_count, "no usage_metadata"
    return f"{chunks} chunk(s), finish_reason={finish}, text: {''.join(text).strip()!r}"


def multi_turn():
    s = common.sampling(args.backend)
    r = client.models.generate_content(
        model=model,
        contents=[types.Content(role="model" if role == "assistant" else "user",
                                parts=[types.Part(text=text)])
                  for role, text in common.MULTI_TURN],
        config=types.GenerateContentConfig(
            system_instruction=common.MULTI_TURN_SYSTEM, temperature=s["temperature"],
            top_p=s.get("top_p"), max_output_tokens=2048, stop_sequences=[common.STOP]))
    return common.cut_at_stop(r.text)


def tool_calling():
    tool = common.WEATHER_TOOL
    r = client.models.generate_content(
        model=model, contents=common.TOOL_PROMPT,
        config=types.GenerateContentConfig(tools=[types.Tool(function_declarations=[
            types.FunctionDeclaration(name=tool["name"], description=tool["description"],
                                      parameters_json_schema=tool["parameters"])])]))
    calls = r.function_calls
    assert calls, "no function call came back"
    assert calls[0].name == "get_weather" and "city" in (calls[0].args or {}), f"bad call: {calls[0]}"
    return f"call: {calls[0].name}({calls[0].args})"


def seed():
    r = client.models.generate_content(
        model=model, contents="Say hi.", config=types.GenerateContentConfig(seed=42))
    assert r.text, "no text"
    return f"reply: {r.text.strip()!r}"


def json_mode():
    r = client.models.generate_content(
        model=model, contents=common.JSON_PROMPT,
        config=types.GenerateContentConfig(response_mime_type="application/json"))
    assert common.strict_json(r.text or "").get("ok") is True, f"unexpected JSON: {r.text!r}"
    return f"reply: {r.text.strip()!r}"


def unknown_model():
    try:
        client.models.generate_content(model=common.UNKNOWN_MODEL, contents="hi")
    except errors.APIError as error:
        assert error.code == 404, f"HTTP {error.code}, not 404"
        # Gemini's error body is {"error": {"code": 404, "message", "status": "NOT_FOUND"}}. Any
        # other shape is the backend's own, because errors are not transcoded.
        raw = error.details if isinstance(error.details, dict) else {}
        inner = raw.get("error")
        assert (set(raw) == {"error"} and isinstance(inner, dict) and inner.get("code") == 404
                and "message" in inner and "status" in inner), f"not a Gemini error body: {raw}"
        return f"HTTP 404: {str(inner['message'])[:80]!r}"
    raise AssertionError("no error for a model that does not exist")


# Whether each request must work (`check`) or is a known gap (`gap`) for each backend, and why.
EXPECTED = {
    "tool_calling": {
        b: ("gap", "Gemini tools are not mapped into the IR yet")
        for b in ("gemini", "anthropic", "openai")
    },
    "seed": {
        "gemini": ("check", "generationConfig.seed goes through the IR"),
        "anthropic": ("gap", "Anthropic has no seed, and rejects the unknown field"),
        "openai": ("check", "generationConfig.seed becomes the IR's seed"),
    },
    "json_mode": {
        b: ("gap", "responseMimeType is dropped on the way into the IR")
        for b in ("gemini", "anthropic", "openai")
    },
    "unknown_model": {
        "gemini": ("check", "the backend's error is already in Gemini's format"),
        "anthropic": ("gap", "errors are not transcoded: Anthropic's format comes back"),
        "openai": ("gap", "errors are not transcoded: OpenAI's format comes back"),
    },
}

results.run("unary with a system instruction: text, finish_reason and usage_metadata", unary)
results.run("streaming: parsed chunk by chunk, with usage_metadata", streaming)
results.run(f"multi-turn with {', '.join(common.sampling(args.backend))}, max_output_tokens and "
            "stop_sequences", multi_turn)
for name, what, fn in [
    ("tool_calling", "tool calling", tool_calling),
    ("seed", "seed", seed),
    ("json_mode", "response_mime_type application/json", json_mode),
    ("unknown_model", "an unknown model's error, in Gemini's format", unknown_model),
]:
    results.expect(EXPECTED[name], args.backend, what, fn)
sys.exit(results.exit_code())
