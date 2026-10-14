#!/usr/bin/env bash
# Interactive one-time setup for the pr-review skill's Jira integration.
# Stores credentials as inert JSON in
# ${XDG_CONFIG_HOME:-~/.config}/pr-review/jira.json with mode 0600.
#
# Re-run any time to update the values. Existing values are offered as defaults.
# Requires: curl, jq, and optionally a browser opener for the token page.
set -euo pipefail

TOKEN_URL="https://id.atlassian.com/manage-profile/security/api-tokens"
CONFIG_DIR="${XDG_CONFIG_HOME:-$HOME/.config}/pr-review"
CONFIG_FILE="$CONFIG_DIR/jira.json"
LEGACY_CONFIG_FILE="$CONFIG_DIR/jira.env"
# Keep credentials in this shell only; child processes receive them through
# jira_curl's stdin config.
export -n JIRA_BASE_URL JIRA_EMAIL JIRA_API_TOKEN 2>/dev/null || true
SCRIPT_DIR="$(CDPATH= cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"

# shellcheck source=jira-common.sh
. "$SCRIPT_DIR/jira-common.sh"

bold() { printf '\033[1m%s\033[0m\n' "$1"; }
dim()  { printf '\033[2m%s\033[0m\n' "$1"; }

open_url() {
  local url="$1"
  if   command -v xdg-open >/dev/null 2>&1; then xdg-open "$url"  >/dev/null 2>&1 & disown || true
  elif command -v open     >/dev/null 2>&1; then open "$url"      >/dev/null 2>&1 & disown || true
  elif command -v wslview  >/dev/null 2>&1; then wslview "$url"   >/dev/null 2>&1 & disown || true
  else return 1; fi
}

command -v curl >/dev/null 2>&1 || { echo "curl is required." >&2; exit 1; }
command -v jq   >/dev/null 2>&1 || { echo "jq is required." >&2; exit 1; }

saved_base_url=""
saved_email=""
saved_api_token=""
if [ -r "$CONFIG_FILE" ]; then
  saved_base_url="$(jq -er '.base_url | select(type == "string" and length > 0)' "$CONFIG_FILE" 2>/dev/null || true)"
  saved_email="$(jq -er '.email | select(type == "string" and length > 0)' "$CONFIG_FILE" 2>/dev/null || true)"
  saved_api_token="$(jq -er '.api_token | select(type == "string" and length > 0)' "$CONFIG_FILE" 2>/dev/null || true)"
fi

umask 077

bold "pr-review - Jira setup"
echo "This links your reviews to Jira so the Spec axis can read the ticket a PR"
echo "implements. Credentials are stored locally as inert JSON at:"
dim  "  $CONFIG_FILE"
echo

# 1. Base URL ---------------------------------------------------------------
bold "1/3  Your Jira site"
echo "The base URL of your Jira, e.g. https://your-company.atlassian.net"
if [ -n "$saved_base_url" ]; then
  read -r -p "Jira base URL [$saved_base_url]: " JIRA_BASE_URL
  JIRA_BASE_URL="${JIRA_BASE_URL:-$saved_base_url}"
else
  read -r -p "Jira base URL: " JIRA_BASE_URL
fi
JIRA_BASE_URL="${JIRA_BASE_URL%/}"
case "$JIRA_BASE_URL" in
  https://*) ;;
  *) echo "Must start with https://; got '$JIRA_BASE_URL'." >&2; exit 1;;
esac
echo

# 2. Email ------------------------------------------------------------------
bold "2/3  Your Atlassian account email"
if [ -n "$saved_email" ]; then
  read -r -p "Email [$saved_email]: " JIRA_EMAIL
  JIRA_EMAIL="${JIRA_EMAIL:-$saved_email}"
else
  read -r -p "Email: " JIRA_EMAIL
fi
[ -n "$JIRA_EMAIL" ] || { echo "Email cannot be blank." >&2; exit 1; }
echo

# 3. API token --------------------------------------------------------------
bold "3/3  Your API token"
echo "Create or manage Atlassian API tokens here:"
echo "  $TOKEN_URL"
if open_url "$TOKEN_URL"; then
  echo "A browser open was requested. Use the URL above if it did not appear."
else
  echo "Open the URL above in your browser."
fi
echo "Create a token labelled 'pr-review', then copy it; it is shown only once."
echo
# Hidden entry so the token never lands in terminal scrollback or shell history.
if [ -n "$saved_api_token" ]; then
  read -r -s -p "Paste a new API token, or press Enter to keep the saved token: " JIRA_API_TOKEN
  JIRA_API_TOKEN="${JIRA_API_TOKEN:-$saved_api_token}"
else
  read -r -s -p "Paste API token: " JIRA_API_TOKEN
fi
echo
[ -n "$JIRA_API_TOKEN" ] || { echo "Token cannot be blank." >&2; exit 1; }
echo

# Validate before saving ----------------------------------------------------
response_file="$(mktemp "${TMPDIR:-/tmp}/pr-review-jira-myself.XXXXXX")"
config_tmp=""
cleanup() {
  rm -f "$response_file"
  if [ -n "$config_tmp" ]; then
    rm -f "$config_tmp"
  fi
}
trap cleanup EXIT
trap 'exit 130' HUP INT TERM

echo "Checking the credentials against $JIRA_BASE_URL ..."
if ! code="$(jira_curl --silent --show-error \
  --output "$response_file" \
  --write-out '%{http_code}' \
  --header 'Accept: application/json' \
  "$JIRA_BASE_URL/rest/api/3/myself")"; then
  echo "Could not connect to Jira. Check the site URL and network connection." >&2
  exit 1
fi

if [ "$code" != "200" ]; then
  echo "Authentication failed (HTTP $code). Check the site URL, email, and token." >&2
  exit 1
fi

who="$(jq -r 'if (.displayName | type) == "string" then .displayName else empty end' "$response_file")"
echo "Authenticated as ${who:-your account}."

# Save ----------------------------------------------------------------------
mkdir -p "$CONFIG_DIR"
chmod 700 "$CONFIG_DIR"
config_tmp="$(mktemp "$CONFIG_DIR/.jira.json.XXXXXX")"

if ! printf '%s\0%s\0%s\0' "$JIRA_BASE_URL" "$JIRA_EMAIL" "$JIRA_API_TOKEN" | jq -Rs '
  split("\u0000") as $values
  | {
      base_url: $values[0],
      email: $values[1],
      api_token: $values[2]
    }
' > "$config_tmp"; then
  echo "Could not serialize Jira credentials." >&2
  exit 1
fi

chmod 600 "$config_tmp"
mv -f "$config_tmp" "$CONFIG_FILE"
config_tmp=""
rm -f "$LEGACY_CONFIG_FILE"

echo
bold "Done."
echo "Saved to $CONFIG_FILE (permissions 600)."
echo "The pr-review skill will now read Jira tickets referenced by a PR."
