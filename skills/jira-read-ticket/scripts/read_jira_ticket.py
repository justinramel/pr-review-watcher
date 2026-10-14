#!/usr/bin/env python3
"""Fetch an EA Flood Jira issue using a Netscape cookies.txt export."""

from __future__ import annotations

import argparse
import html
import http.cookiejar
import json
import os
import re
import stat
import sys
import textwrap
import urllib.error
import urllib.parse
import urllib.request
import warnings
from dataclasses import dataclass
from html.parser import HTMLParser
from pathlib import Path
from typing import Any

DEFAULT_BASE_URL = "https://eaflood.atlassian.net"
DEFAULT_COOKIE_FILE = Path.home() / ".agents" / "secrets" / "eaflood.atlassian.net_cookies.txt"
DEFAULT_OUTPUT_DIR = Path("/tmp/jira-read-ticket")


@dataclass
class FetchResult:
    status: int
    url: str
    content_type: str
    body: bytes


class VisibleTextParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self._skip_depth = 0
        self.parts: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in {"script", "style", "noscript"}:
            self._skip_depth += 1
        if tag in {"p", "div", "section", "article", "li", "tr", "br", "h1", "h2", "h3"}:
            self.parts.append("\n")

    def handle_endtag(self, tag: str) -> None:
        if tag in {"script", "style", "noscript"} and self._skip_depth:
            self._skip_depth -= 1
        if tag in {"p", "div", "section", "article", "li", "tr", "h1", "h2", "h3"}:
            self.parts.append("\n")

    def handle_data(self, data: str) -> None:
        if not self._skip_depth:
            text = data.strip()
            if text:
                self.parts.append(text)

    def text(self) -> str:
        joined = " ".join(self.parts)
        lines = [re.sub(r"\s+", " ", line).strip() for line in joined.splitlines()]
        return "\n".join(line for line in lines if line)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("ticket", nargs="?", help="Jira ticket key or browse URL")
    parser.add_argument("--check-auth", action="store_true", help="Check saved Jira authentication without reading or changing a ticket")
    parser.add_argument("--base-url", default=DEFAULT_BASE_URL)
    parser.add_argument(
        "--cookies",
        default=os.environ.get("JIRA_COOKIES_FILE", str(DEFAULT_COOKIE_FILE)),
        help="Netscape cookies.txt path. Defaults to JIRA_COOKIES_FILE or ~/.agents/secrets/eaflood.atlassian.net_cookies.txt",
    )
    parser.add_argument("--output-dir", default=str(DEFAULT_OUTPUT_DIR))
    parser.add_argument("--max-chars", type=int, default=50000)
    args = parser.parse_args()
    if not args.check_auth and not args.ticket:
        parser.error("a ticket is required unless using --check-auth")
    return args


def normalize_ticket(value: str) -> str:
    parsed = urllib.parse.urlparse(value)
    if parsed.scheme and parsed.netloc:
        match = re.search(r"/browse/([A-Z][A-Z0-9]+-\d+)", parsed.path, re.IGNORECASE)
        if not match:
            raise SystemExit(f"Could not find a Jira issue key in URL: {value}")
        return match.group(1).upper()

    ticket = value.strip().upper()
    if not re.fullmatch(r"[A-Z][A-Z0-9]+-\d+", ticket):
        raise SystemExit(f"Invalid Jira issue key: {value}")
    return ticket


def make_opener(cookie_path: Path) -> urllib.request.OpenerDirector:
    if not cookie_path.exists():
        raise SystemExit(
            "cookies file not found: "
            f"{cookie_path}\nPut a Netscape-format cookies.txt export there, or pass --cookies /path/to/cookies.txt."
        )

    mode = stat.S_IMODE(cookie_path.stat().st_mode)
    if mode & (stat.S_IRWXG | stat.S_IRWXO):
        raise SystemExit(
            f"cookies file permissions are too broad ({mode:o}): {cookie_path}\n"
            f"Restrict the file with: chmod 600 {cookie_path}"
        )

    jar = http.cookiejar.MozillaCookieJar(str(cookie_path))
    try:
        # cookiejar may include the raw cookie line in both a warning traceback
        # and LoadError. Neither is safe to expose in terminal output or logs.
        with warnings.catch_warnings():
            warnings.simplefilter('ignore')
            jar.load(ignore_discard=True, ignore_expires=False)
    except http.cookiejar.LoadError:
        raise SystemExit(f"Could not load cookies file {cookie_path}: invalid Netscape cookie export. Export it again.") from None

    return urllib.request.build_opener(urllib.request.HTTPCookieProcessor(jar))


def fetch(opener: urllib.request.OpenerDirector, url: str, accept: str) -> FetchResult:
    request = urllib.request.Request(
        url,
        headers={
            "Accept": accept,
            "User-Agent": "Mozilla/5.0 EA Flood Jira ticket reader",
            "X-Atlassian-Token": "no-check",
        },
    )
    try:
        with opener.open(request, timeout=30) as response:
            return FetchResult(
                status=response.status,
                url=response.geturl(),
                content_type=response.headers.get("content-type", ""),
                body=response.read(),
            )
    except urllib.error.HTTPError as error:
        return FetchResult(
            status=error.code,
            url=error.geturl(),
            content_type=error.headers.get("content-type", ""),
            body=error.read(),
        )
    except urllib.error.URLError as error:
        raise SystemExit(f"Network error while fetching {url}: {error}") from error


def check_authentication(opener: urllib.request.OpenerDirector, base_url: str) -> None:
    """Distinguish invalid saved credentials from ticket visibility failures."""
    url = f"{base_url}/rest/api/3/myself"
    response = fetch(opener, url, "application/json")
    if response.status == 401:
        raise SystemExit(
            "Jira authentication failed (HTTP 401). The saved cookies are not authenticating; "
            "they may be expired or revoked. Sign in to Jira and export fresh Netscape-format "
            "cookies to the file passed with --cookies (or JIRA_COOKIES_FILE). "
            "The default is ~/.agents/secrets/eaflood.atlassian.net_cookies.txt. "
            "Keep the file mode 600; never paste cookie values into chat."
        )
    if response.status == 403:
        raise SystemExit(
            "Jira identity check was denied (HTTP 403). Check account/site access; "
            "this response alone does not prove the login expired."
        )
    if response.status != 200 or "json" not in response.content_type:
        raise SystemExit(
            f"Could not verify Jira authentication (HTTP {response.status}). "
            "A login redirect or HTML page is not an authenticated API response. "
            "Check Jira access and refresh the cookie export if needed."
        )
    try:
        identity = json.loads(response.body.decode("utf-8"))
    except (json.JSONDecodeError, UnicodeDecodeError) as error:
        raise SystemExit("Jira identity check returned invalid JSON; authentication is unverified.") from error
    if not isinstance(identity, dict) or not identity.get("accountId"):
        raise SystemExit("Jira identity response has no accountId; authentication is unverified.")


def adf_to_text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        return "\n".join(filter(None, (adf_to_text(item) for item in value)))
    if not isinstance(value, dict):
        return str(value)

    node_type = value.get("type")
    if node_type == "text":
        return value.get("text", "")

    child_text = "\n".join(filter(None, (adf_to_text(item) for item in value.get("content", []))))
    if node_type in {"paragraph", "heading", "listItem"}:
        return child_text
    if node_type in {"bulletList", "orderedList"}:
        return "\n".join(f"- {line}" for line in child_text.splitlines() if line.strip())
    return child_text


def simple_value(value: Any) -> str:
    if value is None or value == [] or value == {}:
        return ""
    if isinstance(value, str):
        return value
    if isinstance(value, (int, float, bool)):
        return str(value)
    if isinstance(value, list):
        rendered = [simple_value(item) for item in value]
        return ", ".join(item for item in rendered if item)
    if isinstance(value, dict):
        for key in ("displayName", "name", "value", "key"):
            if value.get(key):
                return str(value[key])
        if value.get("type") == "doc":
            return adf_to_text(value)
    return ""


def truncate(text: str, max_chars: int) -> str:
    if len(text) <= max_chars:
        return text
    return text[:max_chars] + f"\n\n[truncated at {max_chars} characters]"


def format_issue(data: dict[str, Any], max_chars: int) -> str:
    fields = data.get("fields", {})
    names = data.get("names", {})
    lines = [
        f"Key: {data.get('key', '')}",
        f"Summary: {fields.get('summary', '')}",
        f"Type: {simple_value(fields.get('issuetype'))}",
        f"Status: {simple_value(fields.get('status'))}",
        f"Priority: {simple_value(fields.get('priority'))}",
        f"Assignee: {simple_value(fields.get('assignee'))}",
        f"Reporter: {simple_value(fields.get('reporter'))}",
        f"Created: {fields.get('created', '')}",
        f"Updated: {fields.get('updated', '')}",
    ]

    description = adf_to_text(fields.get("description")) or simple_value(fields.get("description"))
    if description:
        lines.extend(["", "Description:", description])

    comments = fields.get("comment", {}).get("comments", [])
    if comments:
        lines.extend(["", "Comments:"])
        for comment in comments[-10:]:
            author = simple_value(comment.get("author"))
            created = comment.get("created", "")
            body = adf_to_text(comment.get("body")) or simple_value(comment.get("body"))
            lines.append(f"- {author} at {created}: {body}")

    custom_lines: list[str] = []
    for key, value in fields.items():
        if not key.startswith("customfield_"):
            continue
        rendered = simple_value(value)
        if rendered and len(rendered) <= 4000:
            custom_lines.append(f"{names.get(key, key)}: {rendered}")

    if custom_lines:
        lines.extend(["", "Additional fields:"])
        lines.extend(custom_lines[:40])

    return truncate("\n".join(line for line in lines if line is not None), max_chars)


def format_html(body: bytes, max_chars: int) -> str:
    parser = VisibleTextParser()
    parser.feed(body.decode("utf-8", errors="replace"))
    return truncate(html.unescape(parser.text()), max_chars)


def save(path: Path, body: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(body)


def main() -> int:
    args = parse_args()
    base_url = args.base_url.rstrip("/")
    cookie_path = Path(args.cookies).expanduser()
    opener = make_opener(cookie_path)
    check_authentication(opener, base_url)
    if args.check_auth:
        print("Jira authentication verified. No ticket was read or changed.")
        return 0
    ticket = normalize_ticket(args.ticket)
    output_dir = Path(args.output_dir).expanduser() / ticket

    rest_url = f"{base_url}/rest/api/3/issue/{urllib.parse.quote(ticket)}?expand=names,renderedFields"
    rest = fetch(opener, rest_url, "application/json")
    save(output_dir / "issue-rest.json", rest.body)

    print(f"Ticket: {ticket}")
    print(f"REST URL: {rest.url}")
    print(f"REST status: {rest.status}")
    print(f"REST artifact: {output_dir / 'issue-rest.json'}")

    if rest.status == 200 and "json" in rest.content_type:
        data = json.loads(rest.body.decode("utf-8"))
        if not isinstance(data, dict) or data.get("key") != ticket or not data.get("fields", {}).get("summary"):
            raise SystemExit("Jira returned no identifiable ticket content; the read did not succeed.")
        print("\n" + format_issue(data, args.max_chars))
        return 0

    if rest.status in {401, 403, 404}:
        if rest.status == 401:
            raise SystemExit("Jira authentication was rejected during the ticket read (HTTP 401). Refresh the cookie export.")
        raise SystemExit(
            f"Jira authentication passed, but {ticket} is unavailable (HTTP {rest.status}). "
            "Check the ticket key and project/issue permissions."
        )

    browse_url = f"{base_url}/browse/{urllib.parse.quote(ticket)}"
    page = fetch(opener, browse_url, "text/html,application/xhtml+xml")
    save(output_dir / "browse.html", page.body)
    print(f"Browse URL: {page.url}")
    print(f"Browse status: {page.status}")
    print(f"Browse artifact: {output_dir / 'browse.html'}")

    if page.status != 200:
        snippet = page.body[:1000].decode("utf-8", errors="replace")
        print("\nFetch failed. Response snippet:")
        print(textwrap.shorten(snippet, width=1000, placeholder="..."))
        return 1

    visible = format_html(page.body, args.max_chars)
    if ticket not in visible or len(visible.strip()) < 40 or re.search(r"sign in|log in|issue not found|issue does not exist", visible, re.IGNORECASE):
        raise SystemExit(
            "Jira returned no readable ticket content. An HTTP 200 page shell or login page "
            "does not count as a successful read. Inspect the saved HTML or use an authenticated browser."
        )
    print("\n" + visible)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
