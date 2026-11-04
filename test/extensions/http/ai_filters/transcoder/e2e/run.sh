#!/usr/bin/env bash
#
# End-to-end test for the AI Protocol Manager transcoder: a client in one LLM API talks to a real
# backend in another (or the same) through Envoy. CLIENT, BACKEND and MODEL each take one value, a
# comma-separated list, or `all`. Builds Envoy once, then for each (client, backend, model) starts
# Envoy with envoy.yaml and runs <client>_client.py, which sends a unary and a streaming request in
# the client's API, checks the translated responses, what Envoy sent upstream (tap), the access log
# and the stats, and repeats the test with the client's official Python SDK if it is installed.
# Writes RUN_DIR/report.md at the end. See README.md.

set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ENVOY_REPO="${ENVOY_REPO:-$(git -C "${HERE}" rev-parse --show-toplevel)}"
CLIENT="${CLIENT:-openai}"
BACKEND="${BACKEND:-gemini}"
MODEL="${MODEL:-}"
BAZEL_BUILD_FLAGS="${BAZEL_BUILD_FLAGS:---copt=-Wno-nullability-completeness}"
ALL_CLIENTS=(openai anthropic gemini)
ALL_BACKENDS=(openai anthropic gemini)

if [[ -z "${ENVOY_BIN:-}" ]]; then
  echo "==> Building //source/exe:envoy-static (set ENVOY_BIN to use a prebuilt binary)"
  # shellcheck disable=SC2086 # BAZEL_BUILD_FLAGS is a list of flags.
  (cd "${ENVOY_REPO}" && bazel build ${BAZEL_BUILD_FLAGS} //source/exe:envoy-static)
  ENVOY_BIN="${ENVOY_REPO}/bazel-bin/source/exe/envoy-static"
fi
export ENVOY_BIN

# The top-level run owns RUN_DIR, so every pair's results land under one directory for the report.
if [[ -z "${E2E_NESTED:-}" ]]; then
  RUN_DIR="${RUN_DIR:-$(mktemp -d "${TMPDIR:-/tmp}/ai-transcoder-e2e.XXXXXX")}"
  mkdir -p "${RUN_DIR}"
  export RUN_DIR
fi

# `all` and comma lists run the test once per (client, backend, model), each in its own
# subdirectory of RUN_DIR.
expand() {
  local value="$1"
  shift
  if [[ "${value}" == "all" ]]; then
    printf '%s\n' "$@"
  else
    tr ',' '\n' <<< "${value}"
  fi
}
mapfile -t clients < <(expand "${CLIENT}" "${ALL_CLIENTS[@]}")
mapfile -t backends < <(expand "${BACKEND}" "${ALL_BACKENDS[@]}")
mapfile -t models < <(tr ',' '\n' <<< "${MODEL}")
if (( ${#clients[@]} * ${#backends[@]} * ${#models[@]} > 1 )); then
  PASSED=()
  FAILED=()
  for client in "${clients[@]}"; do
    for backend in "${backends[@]}"; do
      for model in "${models[@]}"; do
        pair="${client}-to-${backend}"
        run_dir="${RUN_DIR}/${pair}"
        if [[ -n "${model}" ]]; then
          run_dir+="/${model}"
        fi
        echo
        echo "################ CLIENT=${client} BACKEND=${backend} MODEL=${model:-default} ################"
        if E2E_NESTED=1 CLIENT="${client}" BACKEND="${backend}" MODEL="${model}" \
          RUN_DIR="${run_dir}" "${BASH_SOURCE[0]}"; then
          PASSED+=("${pair}${model:+ (${model})}")
        else
          FAILED+=("${pair}${model:+ (${model})}")
        fi
      done
    done
  done
  echo
  python3 "${HERE}/report.py" "${RUN_DIR}"
  echo "PASS: ${PASSED[*]:-none}"
  if [[ ${#FAILED[@]} -ne 0 ]]; then
    echo "FAIL: ${FAILED[*]}"
    exit 1
  fi
  exit 0
fi

# The client's side: its script, the path prefix the route matches, the API it speaks, and the
# credential headers it sends. A Vertex AI client addresses the model under a project and a
# location, which the transcoder reads through the API's built-in Vertex binding.
CLIENT_PROJECT="${CLIENT_PROJECT:-${PROJECT_ID:-acme}}"
CLIENT_LOCATION="${CLIENT_LOCATION:-${LOCATION:-us-central1}}"
case "${CLIENT}" in
  openai)
    CLIENT_SCRIPT=openai_client.py
    CLIENT_PATH_PREFIX="/v1/chat/completions"
    REQUEST_PROTOCOL="OPENAI_CHAT_COMPLETIONS"
    CLIENT_HEADERS=(authorization)
    ;;
  gemini)
    CLIENT_SCRIPT=gemini_client.py
    CLIENT_PATH_PREFIX="/v1beta/models/"
    REQUEST_PROTOCOL="GEMINI_GENERATE_CONTENT"
    CLIENT_HEADERS=(x-goog-api-key)
    ;;
  vertex-gemini)
    CLIENT_SCRIPT=gemini_client.py
    CLIENT_PATH_PREFIX="/v1/projects/"
    REQUEST_PROTOCOL="GEMINI_GENERATE_CONTENT"
    CLIENT_HEADERS=(x-goog-api-key authorization)
    ;;
  anthropic)
    CLIENT_SCRIPT=anthropic_client.py
    CLIENT_PATH_PREFIX="/v1/messages"
    REQUEST_PROTOCOL="ANTHROPIC_MESSAGES"
    CLIENT_HEADERS=(x-api-key)
    ;;
  vertex-anthropic)
    CLIENT_SCRIPT=anthropic_client.py
    CLIENT_PATH_PREFIX="/v1/projects/"
    REQUEST_PROTOCOL="ANTHROPIC_MESSAGES"
    CLIENT_HEADERS=(authorization)
    ;;
  *)
    echo "ERROR: CLIENT must be openai, anthropic, gemini, vertex-gemini, vertex-anthropic or all" \
      "(got ${CLIENT})." >&2
    exit 1
    ;;
esac

# The model to send upstream in place of the one the client asks for, via filter state. The
# backend's path names it.
RESOLVED_MODEL="${RESOLVED_MODEL:-}"

# Vertex AI takes an API key or an OAuth access token.
vertex_credential() {
  if [[ -n "${VERTEX_ACCESS_TOKEN:-}" ]]; then
    CREDENTIAL_HEADER="authorization"
    BACKEND_SECRET="Bearer ${VERTEX_ACCESS_TOKEN}"
  else
    : "${VERTEX_API_KEY:?Set VERTEX_API_KEY (or VERTEX_ACCESS_TOKEN) for a Vertex AI backend.}"
    CREDENTIAL_HEADER="x-goog-api-key"
    BACKEND_SECRET="${VERTEX_API_KEY}"
  fi
  : "${PROJECT_ID:?Set PROJECT_ID to the Google Cloud project the credential belongs to.}"
}

# The backend's side: its host and API, how it carries that API on HTTP (the response binding the
# backend-edge transcoder renders the path from and edits the body and headers with), how it takes
# the key, and the paths the two requests should reach it on. Every backend names a binding, so a
# request leaves in the backend's layout whatever layout it arrived in.
case "${BACKEND}" in
  gemini)
    vertex_credential
    LOCATION="${LOCATION:-us-central1}"
    MODEL="${MODEL:-gemini-2.5-flash}"
    BACKEND_HOST="${VERTEX_HOST:-aiplatform.googleapis.com}"
    RESPONSE_PROTOCOL="GEMINI_GENERATE_CONTENT"
    MODEL_PATH="/v1/projects/${PROJECT_ID}/locations/${LOCATION}/publishers/google/models"
    # Gemini's own body edits (no `model` or `stream` in the body) apply on top of the pattern.
    RESPONSE_BINDING="${MODEL_PATH}/{model}:{generateContent|streamGenerateContent?alt=sse}"
    UNARY_PATH="${MODEL_PATH}/${RESOLVED_MODEL:-${MODEL}}:generateContent"
    STREAM_PATH="${MODEL_PATH}/${RESOLVED_MODEL:-${MODEL}}:streamGenerateContent?alt=sse"
    ;;
  gemini-api)
    : "${GEMINI_API_KEY:?Set GEMINI_API_KEY to a Gemini API key.}"
    MODEL="${MODEL:-gemini-2.5-flash}"
    BACKEND_HOST="generativelanguage.googleapis.com"
    RESPONSE_PROTOCOL="GEMINI_GENERATE_CONTENT"
    RESPONSE_BINDING="/v1beta/models/{model}:{generateContent|streamGenerateContent?alt=sse}"
    CREDENTIAL_HEADER="x-goog-api-key"
    BACKEND_SECRET="${GEMINI_API_KEY}"
    UNARY_PATH="/v1beta/models/${RESOLVED_MODEL:-${MODEL}}:generateContent"
    STREAM_PATH="/v1beta/models/${RESOLVED_MODEL:-${MODEL}}:streamGenerateContent?alt=sse"
    ;;
  anthropic)
    : "${ANTHROPIC_API_KEY:?Set ANTHROPIC_API_KEY to an Anthropic API key.}"
    MODEL="${MODEL:-claude-haiku-4-5}"
    BACKEND_HOST="api.anthropic.com"
    RESPONSE_PROTOCOL="ANTHROPIC_MESSAGES"
    # The API's own layout; its `anthropic-version` header edit applies on top of the pattern.
    RESPONSE_BINDING="/v1/messages"
    CREDENTIAL_HEADER="x-api-key"
    BACKEND_SECRET="${ANTHROPIC_API_KEY}"
    UNARY_PATH="/v1/messages"
    STREAM_PATH="/v1/messages"
    ;;
  vertex-anthropic)
    vertex_credential
    LOCATION="${LOCATION:-us-east5}"
    MODEL="${MODEL:-claude-haiku-4-5@20251001}"
    BACKEND_HOST="${VERTEX_HOST:-${LOCATION}-aiplatform.googleapis.com}"
    RESPONSE_PROTOCOL="ANTHROPIC_MESSAGES"
    MODEL_PATH="/v1/projects/${PROJECT_ID}/locations/${LOCATION}/publishers/anthropic/models"
    # Claude on Vertex AI: the model in the path alone, the API version in the body, and no
    # `anthropic-version` header.
    RESPONSE_BINDING='{"uri_pattern": "'"${MODEL_PATH}"'/{model}:{rawPredict|streamRawPredict}",'
    RESPONSE_BINDING+=' "body_mutations": [{"remove": "model"},'
    RESPONSE_BINDING+=' {"append": {"record": {"key": "anthropic_version", "value": "vertex-2023-10-16"}, "action": "ADD_IF_ABSENT"}}],'
    RESPONSE_BINDING+=' "header_mutations": [{"remove": "anthropic-version"}]}'
    UNARY_PATH="${MODEL_PATH}/${RESOLVED_MODEL:-${MODEL}}:rawPredict"
    STREAM_PATH="${MODEL_PATH}/${RESOLVED_MODEL:-${MODEL}}:streamRawPredict"
    ;;
  openai)
    : "${OPENAI_API_KEY:?Set OPENAI_API_KEY to an OpenAI API key.}"
    MODEL="${MODEL:-gpt-4o-mini}"
    BACKEND_HOST="api.openai.com"
    RESPONSE_PROTOCOL="OPENAI_CHAT_COMPLETIONS"
    # The IR has no built-in binding, so without this a Gemini client's path would go to OpenAI.
    RESPONSE_BINDING="/v1/chat/completions"
    CREDENTIAL_HEADER="authorization"
    BACKEND_SECRET="Bearer ${OPENAI_API_KEY}"
    UNARY_PATH="/v1/chat/completions"
    STREAM_PATH="/v1/chat/completions"
    ;;
  *)
    echo "ERROR: BACKEND must be openai, anthropic, gemini, gemini-api, vertex-anthropic or all" \
      "(got ${BACKEND})." >&2
    exit 1
    ;;
esac
export BACKEND_SECRET

# The route's response declaration, as a YAML flow mapping: the backend's API and its binding, a
# URI pattern or an LLMBinding in JSON.
if [[ "${RESPONSE_BINDING}" == /* ]]; then
  RESPONSE_PER_ROUTE="{llm_protocol: ${RESPONSE_PROTOCOL}, binding: {uri_pattern: \"${RESPONSE_BINDING}\"}}"
else
  RESPONSE_PER_ROUTE="{llm_protocol: ${RESPONSE_PROTOCOL}, binding: ${RESPONSE_BINDING}}"
fi

# The route removes accept-encoding and the client's credentials, except the backend's own key
# header: the router removes headers after credential_injector (which overwrites the key) has run.
# API headers such as anthropic-version are the transcoder's: its binding adds or removes them.
REMOVE_HEADERS="accept-encoding"
for header in "${CLIENT_HEADERS[@]}"; do
  if [[ "${header}" != "${CREDENTIAL_HEADER}" ]]; then
    REMOVE_HEADERS+=", ${header}"
  fi
done

PORT="${PORT:-10000}"
ADMIN_PORT="${ADMIN_PORT:-9901}"
LOG_LEVEL="${LOG_LEVEL:-info}"
# Whether a client payload that fails its API's schema is forwarded (true) or rejected (false).
# A Claude on Vertex AI client sends no `model` and an `anthropic_version`, which the Anthropic
# schema rejects: schema_validation does not read the Vertex binding (the transcoder does), so
# that client fails open by default and the gap is reported.
if [[ -z "${SCHEMA_FAIL_OPEN:-}" && "${CLIENT}" == "vertex-anthropic" ]]; then
  echo "==> CLIENT=vertex-anthropic: schema_validation fails open (known gap; set SCHEMA_FAIL_OPEN=false to see it)"
  SCHEMA_FAIL_OPEN=true
fi
SCHEMA_FAIL_OPEN="${SCHEMA_FAIL_OPEN:-false}"
mkdir -p "${RUN_DIR}/tap"
rm -f "${RUN_DIR}/access.log" "${RUN_DIR}/results.jsonl" "${RUN_DIR}"/tap/*

if [[ -z "${CA_FILE:-}" ]]; then
  for candidate in /etc/ssl/certs/ca-certificates.crt /etc/pki/tls/certs/ca-bundle.crt \
    /etc/ssl/cert.pem; do
    if [[ -r "${candidate}" ]]; then
      CA_FILE="${candidate}"
      break
    fi
  done
fi
if [[ ! -r "${CA_FILE:-}" ]]; then
  echo "ERROR: no CA bundle found; set CA_FILE." >&2
  exit 1
fi

# The key only goes into this file, which credential_injector reads over SDS and which is removed
# on exit. It is never written into envoy.yaml.
mkdir -p "${RUN_DIR}/secrets"
(
  umask 077
  python3 - "${RUN_DIR}/secrets/backend_api_key.json" << 'EOF'
import json, os, sys

with open(sys.argv[1], "w") as f:
    json.dump({"resources": [{
        "@type": "type.googleapis.com/envoy.extensions.transport_sockets.tls.v3.Secret",
        "name": "backend_api_key",
        "generic_secret": {"secret": {"inline_string": os.environ["BACKEND_SECRET"]}},
    }]}, f)
EOF
)

# Fills in the template. Done in Python rather than sed: the bindings hold `|`, `&` and `\`.
export RUN_DIR PORT ADMIN_PORT CA_FILE CLIENT_PATH_PREFIX REQUEST_PROTOCOL BACKEND_HOST \
  RESPONSE_PER_ROUTE CREDENTIAL_HEADER REMOVE_HEADERS RESOLVED_MODEL SCHEMA_FAIL_OPEN
python3 - "${HERE}/envoy.yaml" "${RUN_DIR}/envoy.yaml" << 'EOF'
import os, sys

keys = ["RUN_DIR", "PORT", "ADMIN_PORT", "CA_FILE", "CLIENT_PATH_PREFIX", "REQUEST_PROTOCOL",
        "BACKEND_HOST", "RESPONSE_PER_ROUTE", "CREDENTIAL_HEADER", "REMOVE_HEADERS",
        "RESOLVED_MODEL", "SCHEMA_FAIL_OPEN"]
text = open(sys.argv[1]).read()
for key in keys:
    text = text.replace(f"__{key}__", os.environ[key])
open(sys.argv[2], "w").write(text)
EOF

if [[ -n "${DRY_RUN:-}" ]]; then
  echo "==> Dry run: CLIENT=${CLIENT} BACKEND=${BACKEND} MODEL=${MODEL}; rendered ${RUN_DIR}/envoy.yaml"
  rm -rf "${RUN_DIR}/secrets"
  exit 0
fi

echo "==> CLIENT=${CLIENT} BACKEND=${BACKEND} (${BACKEND_HOST}, model ${MODEL}${RESOLVED_MODEL:+, resolved to ${RESOLVED_MODEL}})"
echo "==> Starting Envoy on 127.0.0.1:${PORT} (admin on ${ADMIN_PORT}); files in ${RUN_DIR}"
"${ENVOY_BIN}" -c "${RUN_DIR}/envoy.yaml" -l "${LOG_LEVEL}" --base-id "$((RANDOM + 1000))" \
  --file-flush-interval-msec 200 > "${RUN_DIR}/envoy.log" 2>&1 &
ENVOY_PID=$!
cleanup() {
  kill "${ENVOY_PID}" 2> /dev/null || true
  wait "${ENVOY_PID}" 2> /dev/null || true
  rm -rf "${RUN_DIR}/secrets"
  # The tap copies the request headers at stream end, after credential_injector has run, so its
  # files hold the key until the client script or this scrubs them.
  python3 "${HERE}/common.py" --scrub-tap "${RUN_DIR}/tap" || true
}
trap cleanup EXIT

for _ in $(seq 1 150); do
  if curl -fsS --noproxy "*" "http://127.0.0.1:${ADMIN_PORT}/ready" > /dev/null 2>&1; then
    break
  fi
  if ! kill -0 "${ENVOY_PID}" 2> /dev/null; then
    echo "ERROR: Envoy exited during startup; see ${RUN_DIR}/envoy.log:" >&2
    grep -E '\]\[(critical|error)\]\[' "${RUN_DIR}/envoy.log" >&2 \
      || tail -n 20 "${RUN_DIR}/envoy.log" >&2
    exit 1
  fi
  sleep 0.2
done
if ! curl -fsS --noproxy "*" "http://127.0.0.1:${ADMIN_PORT}/ready" > /dev/null 2>&1; then
  echo "ERROR: Envoy did not become ready; see ${RUN_DIR}/envoy.log" >&2
  exit 1
fi

echo
CHECKS_FAILED=0
python3 -u "${HERE}/${CLIENT_SCRIPT}" --port "${PORT}" --admin-port "${ADMIN_PORT}" \
  --client "${CLIENT}" --backend "${BACKEND}" --model "${MODEL}" \
  --resolved-model "${RESOLVED_MODEL}" --run-dir "${RUN_DIR}" \
  --backend-host "${BACKEND_HOST}" --unary-path "${UNARY_PATH}" --stream-path "${STREAM_PATH}" \
  --project "${CLIENT_PROJECT}" --location "${CLIENT_LOCATION}" || CHECKS_FAILED=1

echo
if [[ -z "${E2E_NESTED:-}" ]]; then
  python3 "${HERE}/report.py" "${RUN_DIR}"
fi
if [[ "${CHECKS_FAILED}" -ne 0 ]]; then
  echo "FAIL (${CLIENT} to ${BACKEND}, ${MODEL}). Files: ${RUN_DIR}"
  exit 1
fi
echo "PASS (${CLIENT} to ${BACKEND}, ${MODEL}). Files: ${RUN_DIR}"
