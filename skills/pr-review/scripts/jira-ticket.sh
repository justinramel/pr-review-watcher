#!/usr/bin/env bash
# Fetch a Jira ticket as structure-preserving Markdown for the Spec axis.
#
#   jira-ticket.sh <ISSUE-KEY>        e.g. jira-ticket.sh FGP-1392
#
# Uses JIRA_BASE_URL, JIRA_EMAIL, and JIRA_API_TOKEN only when all three are
# set. Otherwise it loads all three values from
# ${XDG_CONFIG_HOME:-~/.config}/pr-review/jira.json.
# Requires: curl, jq.
set -euo pipefail
# Keep credentials in this shell only; child processes receive them through
# jira_curl's stdin config.
export -n JIRA_BASE_URL JIRA_EMAIL JIRA_API_TOKEN 2>/dev/null || true

key="${1:-}"
if [ -z "$key" ]; then
  echo "usage: jira-ticket.sh <ISSUE-KEY>" >&2
  exit 2
fi
if [[ ! "$key" =~ ^[A-Z][A-Z0-9_]*-[0-9]+$ ]]; then
  echo "Invalid Jira issue key: '$key'." >&2
  exit 2
fi

CONFIG_DIR="${XDG_CONFIG_HOME:-$HOME/.config}/pr-review"
CONFIG_FILE="$CONFIG_DIR/jira.json"
LEGACY_CONFIG_FILE="$CONFIG_DIR/jira.env"
SCRIPT_DIR="$(CDPATH= cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
RENDERER="$SCRIPT_DIR/jira-ticket.jq"

# shellcheck source=jira-common.sh
. "$SCRIPT_DIR/jira-common.sh"

command -v curl >/dev/null 2>&1 || { echo "curl is required." >&2; exit 1; }
command -v jq   >/dev/null 2>&1 || { echo "jq is required." >&2; exit 1; }
[ -r "$RENDERER" ] || { echo "Missing Jira renderer: $RENDERER" >&2; exit 1; }

read_config_value() {
  local name="$1"
  jq -er --arg name "$name" '.[$name] | select(type == "string" and length > 0)' "$CONFIG_FILE"
}

if [ -z "${JIRA_BASE_URL:-}" ] || [ -z "${JIRA_EMAIL:-}" ] || [ -z "${JIRA_API_TOKEN:-}" ]; then
  if [ ! -f "$CONFIG_FILE" ]; then
    if [ -f "$LEGACY_CONFIG_FILE" ]; then
      echo "Legacy Jira credentials found. Re-run pr-review/scripts/setup-jira.sh to migrate them." >&2
    else
      echo "No Jira credentials. Run pr-review/scripts/setup-jira.sh first." >&2
    fi
    exit 1
  fi

  JIRA_BASE_URL="$(read_config_value base_url)" || { echo "Invalid Jira base URL in $CONFIG_FILE." >&2; exit 1; }
  JIRA_EMAIL="$(read_config_value email)" || { echo "Invalid Jira email in $CONFIG_FILE." >&2; exit 1; }
  JIRA_API_TOKEN="$(read_config_value api_token)" || { echo "Invalid Jira API token in $CONFIG_FILE." >&2; exit 1; }
fi

case "$JIRA_BASE_URL" in
  https://*) JIRA_BASE_URL="${JIRA_BASE_URL%/}" ;;
  *) echo "JIRA_BASE_URL must start with https://." >&2; exit 1;;
esac

umask 077
field_catalog="$(mktemp "${TMPDIR:-/tmp}/pr-review-jira-fields.XXXXXX")"

issue_body="$(mktemp "${TMPDIR:-/tmp}/pr-review-jira-issue.XXXXXX")"
cleanup() {
  rm -f "$field_catalog" "$issue_body"
}
trap cleanup EXIT
trap 'exit 130' HUP INT TERM

if ! field_code="$(jira_curl --silent --show-error \
  --output "$field_catalog" \
  --write-out '%{http_code}' \
  --header 'Accept: application/json' \
  "$JIRA_BASE_URL/rest/api/3/field")"; then
  echo "Could not connect to Jira while resolving fields." >&2
  exit 1
fi
case "$field_code" in
  200) ;;
  401|403) echo "Not authorised to read Jira fields (HTTP $field_code). Re-run setup-jira.sh." >&2; exit 1;;
  *) echo "Jira field request failed (HTTP $field_code)." >&2; exit 1;;
esac

if ! acceptance_ids="$(jq -er '
  map(select((.name // "" | ascii_downcase | contains("acceptance criteria"))))
  | map(.id)
  | join(",")
' "$field_catalog")"; then
  echo "Jira returned an invalid field catalog." >&2
  exit 1
fi

issue_fields="summary,description,status,issuetype,labels,priority"
if [ -n "$acceptance_ids" ]; then
  issue_fields="$issue_fields,$acceptance_ids"
fi

if ! code="$(jira_curl --silent --show-error \
  --output "$issue_body" \
  --write-out '%{http_code}' \
  --header 'Accept: application/json' \
  --get \
  --data-urlencode "fields=$issue_fields" \
  "$JIRA_BASE_URL/rest/api/3/issue/$key")"; then
  echo "Could not connect to Jira while fetching $key." >&2
  exit 1
fi

case "$code" in
  200) ;;
  401|403) echo "Not authorised for $key (HTTP $code). Re-run setup-jira.sh." >&2; exit 1;;
  404) echo "Ticket $key not found (HTTP $code)." >&2; exit 1;;
  *) echo "Jira request for $key failed (HTTP $code)." >&2; exit 1;;
esac

jq -r \
  --slurpfile field_catalog "$field_catalog" \
  --arg base "$JIRA_BASE_URL" \
  --arg key "$key" \
  -f "$RENDERER" \
  "$issue_body"
