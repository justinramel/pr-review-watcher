---
name: pr-review
description: >-
  Fast PR review with a fixed parallel panel: Standards, Spec, and Architecture & DDD. Produces actionable developer findings and deterministic merge status.
  MUST use for a GitHub pull-request URL, "review PR", "review since a reference", or a request to publish a completed review as a PR comment.
---

# PR Review

Review every change through three independent axes over the complete diff.
Do not edit application files, run formatters, or write to git during a review.

## Quick start

```text
review https://github.com/OWNER/REPO/pull/123
review since origin/main
review and comment on https://github.com/OWNER/REPO/pull/123
```

The last form authorizes a top-level PR comment.
A plain review request never authorizes publication.

## Required workflow

1. Use the capability routing in [`references/workflow.md`](references/workflow.md): Review for Standards and Spec, Deep review for Architecture & DDD, and an economical publication profile for Gitkeeper.
2. Pin the declared base and target, preferably with the bundled evidence collector, and capture the complete diff, commit messages, standards candidates, specification candidates, and current checks.
3. Gather repository standards, the strongest available specification, and architecture/domain context concurrently. Fall back to the PR description and complete commit messages when no external specification resolves; missing architecture context never removes that axis.
4. Compile the fixed three-reviewer panel and start Standards, Spec, and Architecture & DDD in one fan-out over the complete diff.
5. Enforce [`schemas/reviewer-result.schema.json`](schemas/reviewer-result.schema.json) at invocation time, require exactly one valid result from each axis, then aggregate without losing finding provenance.
6. Publish through the Gitkeeper only when the user explicitly authorized an external change.

Follow the complete fixed-panel procedure in [`references/workflow.md`](references/workflow.md).
Use [`references/reporting.md`](references/reporting.md) for deterministic verdict, merge status, merge readiness, and the developer report.

## Fixed review panel

- `standards-only`: Standards reviews the complete diff against repository rules and the smell baseline.
- `spec-only`: Spec reviews the complete diff against the originating requirement or declared intent.
- `architecture-only`: Architecture & DDD reviews the complete diff for module ownership, seams, dependencies, invariants, and domain modelling.

Use [`references/review-contract.md`](references/review-contract.md) for Standards and Spec. Use [`references/architecture-review.md`](references/architecture-review.md) for the Architecture & DDD lens.

## Non-negotiable inputs

Every verdict-bearing brief must include:

- Exactly one fixed review mode and the complete changed-file list.
- The complete diff, pasted into the brief rather than rediscovered by the reviewer.
- Complete standards content at the target revision, or an immutable explicitly authorized URL, for Standards.
- Complete specification content for Spec, using declared intent only as the final fallback.
- Available architecture context and [`references/architecture-review.md`](references/architecture-review.md) for Architecture & DDD.
- The reviewer stance and axis rules in [`references/review-contract.md`](references/review-contract.md).
- The strict invocation-level schema in [`schemas/reviewer-result.schema.json`](schemas/reviewer-result.schema.json).

Reviewers must not read the target's local working tree, edit files, run formatters, run tests, or write to git.
