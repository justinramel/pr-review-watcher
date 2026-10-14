---
name: jira-read-ticket
description: Fetch authenticated EA Flood Jira ticket details. Use when asked to read or summarize a ticket, retrieve its requirements, comments, status, or assignee, or when another skill needs ticket requirements.
---

# Jira Read Ticket

Fetch tickets from `eaflood.atlassian.net` with the user's authenticated browser cookies. Keep cookie contents out of chat, command output, summaries, and committed files.

## Authentication

The helper defaults to the Netscape-format cookie export at:

```text
$HOME/.agents/secrets/eaflood.atlassian.net_cookies.txt
```

`JIRA_COOKIES_FILE` or `--cookies` may override that path. The cookie file must have mode `600`. If it is missing, direct the user to export it to the default path and run:

```bash
chmod 600 "$HOME/.agents/secrets/eaflood.atlassian.net_cookies.txt"
```

Ask for a browser cookie export, never cookie text pasted into chat.

## Authentication check and recovery

Run the commands below from this skill directory. The helper verifies `/rest/api/3/myself` before looking up a ticket. Check the saved login independently with:

```bash
python3 scripts/read_jira_ticket.py --check-auth
```

- HTTP 401 means the saved cookies are not authenticating; they may be expired or revoked. Ask the user to sign in to EA Flood Jira and export fresh Netscape-format cookies to the configured cookie file, then set mode `600` and rerun `--check-auth`.
- HTTP 403 on the identity check means access was denied; do not label it an expired login without further evidence.
- If the identity check succeeds but the ticket returns 403/404, check the ticket key and project/issue permissions.
- HTML/login redirects, malformed identity JSON, or a blank Jira page do not establish authentication or a successful ticket read.

Signing in through an interactive browser does not refresh the helper's exported cookie file. Never ask for cookie values in chat or print them. Do not claim access is restored until the authentication check passes.

## Workflow

1. Normalize the input to a Jira key or an `eaflood.atlassian.net/browse/...` URL.
2. Run the helper with the key or URL as its argument:

   ```bash
   python3 scripts/read_jira_ticket.py TICKET-ID
   ```

   Use `--help` for cookie, output-directory, and truncation overrides.
3. Read the extracted ticket text. The helper tries Jira REST first and can fall back to visible browse-page text for other API failures. A successful read must contain identifiable ticket content; a page shell is a failure.
4. If the requested information is absent or extraction looks incomplete, inspect the saved REST JSON or HTML artifact printed by the helper.
5. Answer only with ticket content relevant to the request.

The read is complete when every field the user requested is either reported from Jira or explicitly identified as unavailable. State when authentication, permissions, extraction, or truncation prevents a complete answer.

Temporary artifacts default to `/tmp/jira-read-ticket`; they can contain private ticket data and stay out of repositories.

## Failure handling

- Missing or over-permissive cookies: give the default path and the `chmod 600` command.
- Failed authentication: follow the authentication check and recovery steps above.
- HTTP 404: verify the ticket key and project access.
- Noisy HTML extraction: prefer saved REST JSON; otherwise answer with a caveat.
- Restricted network access: request access to `eaflood.atlassian.net` and rerun.
