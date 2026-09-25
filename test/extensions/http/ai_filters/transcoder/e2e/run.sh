#!/usr/bin/env bash
#
# End-to-end test for the AI Protocol Manager transcoder: a client in one LLM API talks to a real
# backend in another (or the same) through Envoy. CLIENT and BACKEND each pick gemini, anthropic or
# openai, or all three in turn. Builds Envoy, starts it with envoy.yaml, then runs
# <CLIENT>_client.py, which sends a unary and a streaming request in the client's API, checks the
# translated responses, the upstream paths and the filters' stats, and repeats the test with the
# client's official Python SDK if it is installed. See README.md.

set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ENVOY_REPO="${ENVOY_REPO:-$(git -C "${HERE}" rev-parse --show-toplevel)}"
CLIENT="${CLIENT:-openai}"
BACKEND="${BACKEND:-gemini}"
BAZEL_BUILD_FLAGS="${BAZEL_BUILD_FLAGS:---copt=-Wno-nullability-completeness}"
ALL=(gemini anthropic openai)

if [[ -z "${ENVOY_BIN:-}" ]]; then
  echo "==> Building //source/exe:envoy-static (set ENVOY_BIN to use a prebuilt binary)"
  # shellcheck disable=SC2086 # BAZEL_BUILD_FLAGS is a list of flags.
  (cd "${ENVOY_REPO}" && bazel build ${BAZEL_BUILD_FLAGS} //source/exe:envoy-static)
  ENVOY_BIN="${ENVOY_REPO}/bazel-bin/source/exe/envoy-static"
fi
export ENVOY_BIN

# CLIENT=all or BACKEND=all runs the test once per pair, each in its own run directory.
if [[ "${CLIENT}" == "all" || "${BACKEND}" == "all" ]]; then
  clients=("${CLIENT}")
  backends=("${BACKEND}")
  [[ "${CLIENT}" == "all" ]] && clients=("${ALL[@]}")
  [[ "${BACKEND}" == "all" ]] && backends=("${ALL[@]}")
  PASSED=()
  FAILED=()
  for client in "${clients[@]}"; do
    for backend in "${backends[@]}"; do
      pair="${client}-to-${backend}"
      echo
      echo "################ CLIENT=${client} BACKEND=${backend} ################"
      run_dir=""
      if [[ -n "${RUN_DIR:-}" ]]; then
        run_dir="${RUN_DIR}/${pair}"
      fi
      if CLIENT="${client}" BACKEND="${backend}" RUN_DIR="${run_dir}" MODEL="" \
        "${BASH_SOURCE[0]}"; then
        PASSED+=("${pair}")
      else
        FAILED+=("${pair}")
      fi
    done
  done
  echo
  echo "PASS: ${PASSED[*]:-none}"
  if [[ ${#FAILED[@]} -ne 0 ]]; then
    echo "FAIL: ${FAILED[*]}"
    exit 1
  fi
  exit 0
fi

# The client's side: the API it calls, and the credentials it sends with it.
case "${CLIENT}" in
  openai)
    CLIENT_PATH_PREFIX="/v1/chat/completions"
    REQUEST_PROTOCOL="OPENAI_CHAT_COMPLETIONS"
    CLIENT_HEADERS=(authorization)
    ;;
  gemini)
    CLIENT_PATH_PREFIX="/v1beta/models/"
    REQUEST_PROTOCOL="GEMINI_GENERATE_CONTENT"
    CLIENT_HEADERS=(x-goog-api-key)
    ;;
  anthropic)
    CLIENT_PATH_PREFIX="/v1/messages"
    REQUEST_PROTOCOL="ANTHROPIC_MESSAGES"
    CLIENT_HEADERS=(x-api-key anthropic-version)
    ;;
  *)
    echo "ERROR: CLIENT must be gemini, anthropic, openai or all (got ${CLIENT})." >&2
    exit 1
    ;;
esac

# The backend's side: its host and API, where the backend-edge transcoder's path goes on it, and
# how it takes the key. The client names the backend's model.
EXTRA_HEADER_KEY="x-ai-transcoder-e2e"
EXTRA_HEADER_VALUE="${CLIENT}-to-${BACKEND}"
case "${BACKEND}" in
  gemini)
    : "${VERTEX_API_KEY:?Set VERTEX_API_KEY to a Vertex AI API key.}"
    : "${PROJECT_ID:?Set PROJECT_ID to the Google Cloud project the key belongs to.}"
    LOCATION="${LOCATION:-us-central1}"
    MODEL="${MODEL:-gemini-2.5-flash}"
    BACKEND_HOST="aiplatform.googleapis.com"
    RESPONSE_PROTOCOL="GEMINI_GENERATE_CONTENT"
    REWRITE_PATTERN='^/v1beta/models/(.*)$'
    # \1 is the regex's capture group; sed turns the \\ into one backslash.
    REWRITE_SUBSTITUTION="/v1/projects/${PROJECT_ID}/locations/${LOCATION}/publishers/google/models/"'\\1'
    CREDENTIAL_HEADER="x-goog-api-key"
    BACKEND_SECRET="${VERTEX_API_KEY}"
    UNARY_PATH="/v1/projects/${PROJECT_ID}/locations/${LOCATION}/publishers/google/models/${MODEL}:generateContent"
    STREAM_PATH="/v1/projects/${PROJECT_ID}/locations/${LOCATION}/publishers/google/models/${MODEL}:streamGenerateContent?alt=sse"
    ;;
  anthropic)
    : "${ANTHROPIC_API_KEY:?Set ANTHROPIC_API_KEY to an Anthropic API key.}"
    MODEL="${MODEL:-claude-haiku-4-5}"
    BACKEND_HOST="api.anthropic.com"
    RESPONSE_PROTOCOL="ANTHROPIC_MESSAGES"
    REWRITE_PATTERN='^.*$'
    REWRITE_SUBSTITUTION="/v1/messages"
    CREDENTIAL_HEADER="x-api-key"
    BACKEND_SECRET="${ANTHROPIC_API_KEY}"
    EXTRA_HEADER_KEY="anthropic-version"
    EXTRA_HEADER_VALUE="2023-06-01"
    UNARY_PATH="/v1/messages"
    STREAM_PATH="/v1/messages"
    ;;
  openai)
    : "${OPENAI_API_KEY:?Set OPENAI_API_KEY to an OpenAI API key.}"
    MODEL="${MODEL:-gpt-4o-mini}"
    BACKEND_HOST="api.openai.com"
    RESPONSE_PROTOCOL="OPENAI_CHAT_COMPLETIONS"
    REWRITE_PATTERN='^.*$'
    REWRITE_SUBSTITUTION="/v1/chat/completions"
    CREDENTIAL_HEADER="authorization"
    BACKEND_SECRET="Bearer ${OPENAI_API_KEY}"
    UNARY_PATH="/v1/chat/completions"
    STREAM_PATH="/v1/chat/completions"
    ;;
  *)
    echo "ERROR: BACKEND must be gemini, anthropic, openai or all (got ${BACKEND})." >&2
    exit 1
    ;;
esac
export BACKEND_SECRET

# The route removes accept-encoding and the client's credentials, except the backend's own key and
# extra header: the router removes headers after credential_injector (which overwrites the key) has
# run, and the route's request_headers_to_add overwrites the extra header.
REMOVE_HEADERS="accept-encoding"
for header in "${CLIENT_HEADERS[@]}"; do
  if [[ "${header}" != "${CREDENTIAL_HEADER}" && "${header}" != "${EXTRA_HEADER_KEY}" ]]; then
    REMOVE_HEADERS+=", ${header}"
  fi
done

PORT="${PORT:-10000}"
ADMIN_PORT="${ADMIN_PORT:-9901}"
LOG_LEVEL="${LOG_LEVEL:-info}"
RUN_DIR="${RUN_DIR:-$(mktemp -d "${TMPDIR:-/tmp}/ai-transcoder-e2e-${CLIENT}-to-${BACKEND}.XXXXXX")}"
mkdir -p "${RUN_DIR}"
rm -f "${RUN_DIR}/access.log"

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

sed -e "s|__RUN_DIR__|${RUN_DIR}|g" \
  -e "s|__PORT__|${PORT}|g" \
  -e "s|__ADMIN_PORT__|${ADMIN_PORT}|g" \
  -e "s|__CA_FILE__|${CA_FILE}|g" \
  -e "s|__CLIENT_PATH_PREFIX__|${CLIENT_PATH_PREFIX}|g" \
  -e "s|__REQUEST_PROTOCOL__|${REQUEST_PROTOCOL}|g" \
  -e "s|__BACKEND_HOST__|${BACKEND_HOST}|g" \
  -e "s|__RESPONSE_PROTOCOL__|${RESPONSE_PROTOCOL}|g" \
  -e "s|__REWRITE_PATTERN__|${REWRITE_PATTERN}|g" \
  -e "s|__REWRITE_SUBSTITUTION__|${REWRITE_SUBSTITUTION}|g" \
  -e "s|__CREDENTIAL_HEADER__|${CREDENTIAL_HEADER}|g" \
  -e "s|__REMOVE_HEADERS__|${REMOVE_HEADERS}|g" \
  -e "s|__EXTRA_HEADER_KEY__|${EXTRA_HEADER_KEY}|g" \
  -e "s|__EXTRA_HEADER_VALUE__|${EXTRA_HEADER_VALUE}|g" \
  "${HERE}/envoy.yaml" > "${RUN_DIR}/envoy.yaml"

echo "==> CLIENT=${CLIENT} BACKEND=${BACKEND} (${BACKEND_HOST}, model ${MODEL})"
echo "==> Starting Envoy on 127.0.0.1:${PORT} (admin on ${ADMIN_PORT}); files in ${RUN_DIR}"
"${ENVOY_BIN}" -c "${RUN_DIR}/envoy.yaml" -l "${LOG_LEVEL}" --base-id "$((RANDOM + 1000))" \
  --file-flush-interval-msec 200 > "${RUN_DIR}/envoy.log" 2>&1 &
ENVOY_PID=$!
cleanup() {
  kill "${ENVOY_PID}" 2> /dev/null || true
  wait "${ENVOY_PID}" 2> /dev/null || true
  rm -rf "${RUN_DIR}/secrets"
}
trap cleanup EXIT

for _ in $(seq 1 150); do
  if curl -fsS "http://127.0.0.1:${ADMIN_PORT}/ready" > /dev/null 2>&1; then
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
if ! curl -fsS "http://127.0.0.1:${ADMIN_PORT}/ready" > /dev/null 2>&1; then
  echo "ERROR: Envoy did not become ready; see ${RUN_DIR}/envoy.log" >&2
  exit 1
fi

echo
CHECKS_FAILED=0
python3 -u "${HERE}/${CLIENT}_client.py" --port "${PORT}" --admin-port "${ADMIN_PORT}" \
  --model "${MODEL}" --backend "${BACKEND}" --run-dir "${RUN_DIR}" \
  --unary-path "${UNARY_PATH}" --stream-path "${STREAM_PATH}" || CHECKS_FAILED=1

echo
if [[ "${CHECKS_FAILED}" -ne 0 ]]; then
  echo "FAIL (${CLIENT} to ${BACKEND}). Files: ${RUN_DIR}"
  exit 1
fi
echo "PASS (${CLIENT} to ${BACKEND}). Files: ${RUN_DIR}"
