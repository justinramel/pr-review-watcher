# PR review watcher

A local service that monitors configured GitHub repositories for draft-to-ready transitions,
runs the bundled Pi `pr-review` skill and sends reports through an existing Slack workflow.

## Fresh machine quick start

Use Linux with a systemd user session. Install Git, Python 3.11+, Node 24 and
GitHub CLI first. There are no Python dependencies to install.

Install Pi; version `1.2.0` is the version tested with this watcher:

```bash
npm install -g --ignore-scripts @earendil-works/pi-coding-agent@1.2.0
```

Clone the repository and run the guided setup:

```bash
git clone https://github.com/justinramel/pr-review-watcher.git
cd pr-review-watcher
./setup.sh
```

The wizard guides you through GitHub and Pi login, repositories, Slack email,
webhook, Jira cookies and starting the background service. Use a GitHub account
with access to the repositories you want reviewed. You can reuse an existing
Slack workflow URL. The review skills and Jira reader are bundled in the repo.

After setup, check the status and watch the logs:

```bash
pr-review-watcher status
pr-review-watcher logs --follow
```

Wait for the first scan to finish, then test by adding a new comment to one of
your configured open, ready PRs. New installations watch only your own PRs first.

If you are replacing another machine, stop its watcher before starting this one
to avoid duplicate reviews. Run this on the old machine:

```bash
systemctl --user stop pr-review-watcher.service
```

## Setup details

Configuration prompts include your GitHub login, Slack email, organisation,
repositories, Pi executable and Jira cookie path. New configurations watch only
your own PRs first. Existing settings and author mappings are kept; check the
shown enabled-author list before starting. Slack webhook entry is hidden. Config
and secret files use mode `600` outside the checkout. Replaced config and service
files have local `.bak` copies. Rerun the wizard to adjust settings.

For direct installation without the guide:

```bash
python3 install.py --configure  # guided local settings; install without starting
python3 install.py             # install files only; does not create live config
python3 install.py --start     # verify access, enable and restart the service
```

The guide does not send a Slack test message. To exercise the complete flow after
starting, post a new comment on one of your configured open, ready PRs. The first
scan baselines older comments; post after it has completed. Keep other author
mappings disabled until your own test succeeds.

Guided setup stores your webhook with mode `600` in
`~/.config/pr-review-watcher/slack-webhook-url`. For manual setup, create that file
yourself. Never commit it. The installer preserves existing config and only starts
or restarts the service when `--start` is supplied. A plain install creates no live
config; run `--configure` before starting a new installation. `doctor` checks GitHub identity, permissions,
executables and the private webhook. It does not send messages or prove model authentication;
a real review can still fail if a provider/Jira session expires. Pi uses your normal model
and credentials; `agent.thinking` defaults to `high` for reviews, and optional
`agent.model` overrides the top-level session. Your global Pi settings are not changed.

## Bundled skills

`skills/pr-review/` contains the review instructions, three-axis contracts, schema,
evidence collector and deterministic aggregator. `skills/jira-read-ticket/` contains
the authenticated Jira reader. The watcher resolves both from its own checkout,
independently of the working directory or globally installed skills. Edit these
copies to version review behavior with the service. Shared home-directory copies
are independent and are not changed by this project.

`agent.skill` can override the bundled review skill with an absolute path.
`sources.jira_command` can override the bundled Jira reader with an argument array.
Existing installations should remove an old `agent.skill` override to use the bundle.
The fixed launcher starts three fresh reviewers; `agent.launcher = "legacy"` is an
optional rollback that needs the separately installed Pi subagents extension.

Jira credentials remain local: export Netscape-format cookies to
`~/.agents/secrets/eaflood.atlassian.net_cookies.txt` with mode `600`, or set
`JIRA_COOKIES_FILE`. The watcher refreshes ticket requirements each review round.
Pi credentials, GitHub authentication, Slack webhook, live config, logs, caches and
review artifacts are not part of this repository. The example config contains only
settings and recipient mappings; replace these for another installation.

Run the local checks without GitHub writes or Slack messages:

```bash
python -m unittest discover -s tests -q
node --test skills/pr-review/scripts/review-tools.test.mjs
python -m unittest discover -s skills/jira-read-ticket/tests -q
```

## Configuration

Edit `~/.config/pr-review-watcher/config.toml`; see [config.example.toml](config.example.toml).
Repo list, GitHub reviewer, skill, command array, model, polling interval, timeout, recipients
and paths are configurable. Only authors listed under enabled `[users.LOGIN]` entries are
eligible. Add more entries to expand the rollout. A new repository/author scope starts with
a baseline, so old ready PRs are not reviewed unexpectedly. Configuration reloads each poll;
restart after changing the state directory. Service PATH is captured during installation.

The example uses placeholder identities and repositories, with its sample user
disabled. Interactive setup creates a configuration for your own GitHub login and
Slack email. Your existing installation keeps its configured repositories and users.
Reports go to each enabled author’s mapped Slack email; genuinely GREEN, merge-ready
reports also go to the configured owner with a Files changed link. Approval remains manual. Failed reviews notify the configured owner.
No findings, comments or approvals are posted to GitHub. The sole PR write is adding
the configured account to requested reviewers; other reviewers are preserved. The request
remains outstanding until a GitHub review is submitted manually.
For self-authored test PRs, reviewer assignment is skipped because GitHub rejects requesting
the PR author as reviewer. The local review still runs and sends its report. If the author
and owner share a Slack address, a GREEN review produces one message with a Files changed
link; it does not suggest approving your own PR.

## Welcoming new users

Add a user under `[users.GITHUB_LOGIN]` with `enabled = true` and their Slack email.
The next poll sends them a plain-English welcome through the same private workflow,
explaining draft/ready, waiting for checks, fixing findings and commenting for another
review. Enabling a previously disabled user also sends the welcome. They do not need
to open a PR first.

Existing enabled users are baselined when this feature first runs and receive no
unexpected message. Successful welcome requests are recorded per GitHub login and
Slack email, so restarts and config reloads do not send another. A changed Slack email
gets a new welcome. Failed requests retry with backoff; disabling/removing the user
cancels pending messages. `pr-review-watcher status` includes welcome delivery state,
and the logs include `welcome_queued`, `welcome_accepted` and `welcome_failed`.

The default text lives in [pr_review_watcher/welcome.txt](pr_review_watcher/welcome.txt).
To change it locally, set `welcome_message` under `[slack]` to a TOML multiline string
(up to 2300 characters). Use `{owner_email}` to insert the configured reviewer’s
notification email, and `{repositories}` for the configured repository list. It is plain text with emojis; formatting markers are not
interpreted by the workflow. `welcome_enabled = false` disables welcome messages.
No review tokens are used to write or deliver them.

## Operations

```bash
pr-review-watcher status             # persisted runs, artifacts and delivery state
pr-review-watcher logs --follow      # rotating operational log
systemctl --user status pr-review-watcher
journalctl --user -u pr-review-watcher -f
systemctl --user stop pr-review-watcher
```

While the service is stopped, these commands share its exclusive lock:

```bash
pr-review-watcher scan               # discover/baseline/queue only; no review or messages
pr-review-watcher once               # scan, process at most one review, send due messages
pr-review-watcher enqueue https://github.com/DEFRA/REPO/pull/123
pr-review-watcher retry 1            # explicitly retry a failed run
```

`enqueue` is an explicit way to review a currently ready existing PR. It still enforces
the repository/author allowlist. Jobs start against the latest base/head, then pin them.
Draft/closed/disabled jobs are skipped. If the head/base changes during review, the author
receives a clearly marked report for the old commit, and no GREEN notification goes to the owner.
Pushes alone do not trigger another review. Post a new top-level PR comment when the batch
of fixes or clarifications is ready, for example:

```text
This PR delivers only the configuration part of the ticket.
UI, API, persistence and auditing are handled in separate PRs.
```

The PR must be open and ready, authored by an enabled mapped user. Only new,
nonempty comments from that PR's author trigger a review. Bot comments, comments
from other people, inline code comments and edits to previously seen comments do
not trigger a run. Comments in draft PRs are consumed without a run; mark ready
when finished. `watch.review_command = ""` enables ordinary comments; setting it
to `/review` optionally requires that prefix. Existing comments are baselined
when first enabled. A new comment works even without a new commit.

Multiple comments seen together queue one review of the latest head; a newer request
supersedes an already queued request for the same PR. Requests during an active review are
picked up on the next scan. Complete authorised top-level comments and the last completed
review are saved in `review-context.json` and passed to the next round. Scope clarifications
must reach all three reviewer briefs. Ticket coverage is assessed within the PR's declared
scope, while direct contradictions with explicit requirements remain reviewable. A follow-up
must explain every previous finding as resolved, accepted with justification, or still-open;
validated dispositions appear in the Slack report. GREEN notifications to the owner also
include the author’s comments used in that review, with direct GitHub links. Comments
are taken from the reviewed snapshot, rather than fetched after the verdict.
No GitHub comments are posted by the watcher.

You can also return to draft and ready, or enqueue manually.

State, reports, evidence, Pi output and sessions are in `~/.local/state/pr-review-watcher`.
Look at the run's artifacts path in `status` to diagnose review failures. Logs carry run IDs,
repositories, PR numbers and commits; transport errors never include webhook URLs or tokens.
The operational log rotates at 5 MB with five backups; report artifacts are retained until
you delete them. The service must be running on an awake, connected machine. Recovery uses
PR timeline events so transitions during downtime on currently open PRs can be detected.
Multiple unseen transitions coalesce to one current review per scan. PRs closed before
reconnection aren't reviewed. Don't delete the state database unless you want a fresh baseline.

## Slack semantics

The workflow accepts `recipient` (user email), `report` (text) and `pr_url` (text).
Slack resolves the email; it must match the account in this workspace. A workflow URL is a
secret. There is no separate Slack app or token required by the watcher. Delivery uses only
the configured map, never recipients suggested by PR contents.

Reports are rendered from validated structured findings and conclusions; Pi's complete
original narrative stays in the run directory. Long reports are split into <=2300-character
chunks, stored and retried independently.
Requests are spaced by at least 1.1 seconds. Failures retry with exponential delay up to an
hour without rerunning reviews. Each attempt rechecks that the repository and author
are still enabled and the recipient still matches the current mapping. Revoked or
changed destinations are cancelled rather than forwarded to a new address. `accepted` means the workflow accepted the request, not that
Slack has confirmed DM delivery; check the workflow activity log for downstream failures.
Before each GREEN notification is sent or retried, the head/base and readiness are checked
again; stale success notifications are cancelled.
Exactly-once delivery cannot be guaranteed after an ambiguous timeout or a crash between
Slack receipt and local commit. Outgoing reports omit the skill's environment section and
tool names; the complete original report is retained locally.

The review runs in a private evidence workspace, not your application checkout. The initial
live collector diff is replaced by an immutable base/head compare before review. Reviewers
receive immutable evidence; this is not an OS security sandbox. Pi retains your normal local
access. Repository code is not built or tested by this service. The existing skill's own
aggregator validates each review; missing checks or malformed panel outputs cannot trigger
a GREEN notification. Before broad rollout, inspect the first few reports and workflow runs.

## Tests and CI

```bash
python -m unittest discover -s tests -v
python -m unittest discover -s skills/jira-read-ticket/tests -v
node --test skills/pr-review/scripts/review-tools.test.mjs
bash -n setup.sh
```

[Checks](.github/workflows/checks.yml) runs on pushes and pull requests using Python
3.11 and 3.14 with Node 24. It tests the watcher, installer, Jira reader and review
tools, plus syntax checks. Tests use local fixtures and temporary directories;
no Pi login, Jira cookies or Slack webhook is needed in CI.
