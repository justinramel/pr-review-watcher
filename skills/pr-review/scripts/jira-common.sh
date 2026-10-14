#!/usr/bin/env bash
# Shared Jira helpers. Source this file from the bundled Jira scripts.

curl_config_escape() {
  local value="${1-}"
  value="${value//\\/\\\\}"
  value="${value//\"/\\\"}"
  value="${value//$'\t'/\\t}"
  value="${value//$'\r'/\\r}"
  value="${value//$'\n'/\\n}"
  value="${value//$'\v'/\\v}"
  printf '%s' "$value"
}

jira_curl() {
  if [ -z "${JIRA_EMAIL:-}" ] || [ -z "${JIRA_API_TOKEN:-}" ]; then
    echo "Jira credentials are not loaded." >&2
    return 2
  fi

  local credentials escaped
  credentials="${JIRA_EMAIL}:${JIRA_API_TOKEN}"
  escaped="$(curl_config_escape "$credentials")"

  # Disable user curl configuration before loading authentication from stdin, so
  # verbose or trace options cannot expose the token.
  printf 'user = "%s"\n' "$escaped" | command curl --disable --config - "$@"
}
