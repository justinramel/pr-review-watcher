# Review workflow

Use this procedure after the main skill selects `pr-review`.
Review existing changes only.

## Prerequisites

The harness needs complete diff access, parallel reviewers, and invocation-level JSON Schema enforcement.
Use the strongest available general reasoning profile with high effort for the orchestrator, a strong long-context Review profile with high effort for Standards and Spec, the deepest review profile with high effort for Architecture & DDD, and an economical profile with low effort for Gitkeeper.
When per-task profiles are unavailable, use the deepest required profile for the whole panel.
If parallel execution is unavailable, run the same three reviewers sequentially.
Never change the user's model configuration or infer model and effort values the runtime did not expose.

## Tool-assisted path

Run the dependency-free tool from the skill directory:

```bash
node scripts/review-tools.mjs collect --pr "$PR_URL" --out /tmp/pr-review-evidence.json
node scripts/review-tools.mjs compile-panel --evidence /tmp/pr-review-evidence.json --out /tmp/pr-review-panel.json
```

Use `collect --range <base...target>` for a local review.
When a stronger specification or repository-specific architecture source exists outside the evidence pack, pass `compile-panel` an optional plan containing `specification` and/or `architectureContext`.
The plan shape is `{"specification":{"path":"...","content":"..."},"architectureContext":[{"path":"...","content":"..."}]}`; omit either key when unused.
Use `node scripts/review-tools.mjs --help` for command options.
The tool handles repeatable mechanics; the orchestrator selects source material, validates results, and writes the report.

## 1. Pin the change

### GitHub pull request

Read `pr://<owner>/<repo>/<n>` for the title, body, linked issue, declared base, declared head, and head SHA.
Read `pr://<owner>/<repo>/<n>/diff/all` for the complete unified diff.
Fetch every PR commit's complete message and the current check rollup for the head SHA.
Use the declared base and head rather than assuming a default branch.
Treat verification claims in the PR body as context rather than proof.

### Local range

For `review since <ref>`, use `<ref>` as base and `HEAD` as target.
For a named branch or ref, use it as target and resolve the repository's default branch as base.
Capture the complete three-dot diff and complete commit messages.

Stop before spawning reviewers when a ref does not resolve or the diff is empty.

## 2. Gather fixed-panel inputs

Gather repository standards, resolve the strongest specification, and find architecture/domain context concurrently.

### Repository standards

Read the target revision's `AGENTS.md`, `CONTRIBUTING.md`, and other coding-standard documents completely.
Supply their complete content or an immutable target-revision URL to Standards.

### Specification

Use the first source that resolves:

1. A Jira ticket referenced by the title, branch, or complete commit messages.
2. A linked or referenced GitHub issue.
3. A user-supplied specification or matching repository spec.
4. The PR title, PR body, and complete commit messages as declared-intent fallback.

For a local range, use complete commit messages as the final fallback.
Always run Spec and label the fallback source honestly.

### Architecture and domain context

Gather applicable architecture decisions, ADRs, module maps, and domain glossaries from the target revision.
When none exist, say so in the brief and apply the bundled Architecture & DDD lens directly to the complete diff.
Always run Architecture & DDD.

## 3. Compile the fixed panel

The panel is always:

1. Standards — `standards-only`, Review profile.
2. Spec — `spec-only`, Review profile.
3. Architecture & DDD — `architecture-only`, Deep review profile.

All three receive the complete changed-file list and complete diff.
Each receives only the source material for its axis.
The panel compiler creates these reviewers; the orchestrator does not choose modes, buckets, owners, depth, or reviewer count.

## 4. Start the panel

Load [`../schemas/reviewer-result.schema.json`](../schemas/reviewer-result.schema.json) once.
Pass it as every task's strict invocation-level output schema.
Start all three reviewers in one fan-out.
When exact-head runtime validation will add evidence beyond available checks, start immutable snapshot validation beside the reviewer fan-out, collect it before aggregation, and remove the snapshot afterward.
On Oh My Pi, use the bundled `reviewer` agent for Standards and Spec and the bundled `task` agent for Architecture & DDD.
Set high effort when the runner exposes it.
Never change the user's model configuration during a review.

Reviewers must not read the target's local working tree, edit files, run formatters, run builds, run linters, run tests, or write to git.

## 5. Validate and aggregate

Require exactly one structurally and semantically valid result named `Standards`, `Spec`, and `Architecture & DDD`.
Reject wrappers, renamed axes, duplicate axes, missing axes, and malformed results.
Preserve every finding's reviewer and location. Consolidate exact duplicates into one developer action only when all provenance remains visible.
Write an aggregation input containing the three `reviewers`, collected `checks`, runtime `validation`, `conflictingEvidence`, and `securityOrDataLossRisk`.
Each validation entry is `{"name":"...","status":"passed|failed|pending|not-run|not-applicable"}`.
Run `node scripts/review-tools.mjs aggregate --input <file> --out <file>`, then follow [`reporting.md`](reporting.md) for the developer report.

## 6. Draft or publish a PR comment

Do this only when the user requested a comment draft or publication.
Start one non-verdict-bearing Gitkeeper after the technical report is settled.
Give it the PR URL, final report, changed-file list, publication authorization, and [`gitkeeper-role.md`](gitkeeper-role.md).

Use a top-level PR comment rather than a formal approval or request-changes review.
Update the authenticated user's prior comment containing `<!-- pr-review-skill -->` instead of creating a duplicate.
Without explicit authorization or a write-capable GitHub client, return the complete draft without changing GitHub.
