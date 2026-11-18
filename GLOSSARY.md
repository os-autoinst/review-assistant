# OpenQA Model Review Assistant

Autonomous, model-based pull request review service for `os-autoinst/openQA`.

## Language

**Reviewer Daemon**:
A continuous background process that monitors pull requests via outbound conditional polling and executes reviews sequentially.
_Avoid_: Webhook listener, cron runner, polling bot

**Deduplication State**:
A persistent record mapping pull request identifiers and head commit SHAs to review statuses to prevent duplicate reviews on identical revisions.
_Avoid_: Cache, review database, state cache

**Advisory Review**:
A model-generated GitHub pull request review posted with `COMMENT` that gives feedback without changing the merge
state. `APPROVE` is allowed by D1, and the bot holds `write`, so its `APPROVE` counts towards Mergify's two approvals.
_Avoid_: Advisory-only guarantee, merge check

**Skill Checkout**:
The pinned checkout of `os-autoinst/openQA-skill` baked into the container image, defining domain review rules and
prompt playbooks. It is read-only to the agent.
_Avoid_: System prompt, prompt template, instructions repo

**Ephemeral Clone**:
The shallow `os-autoinst/openQA` clone a run makes in its own scratch directory; the PR heads are fetched into it.
_Avoid_: Persistent clone, host clone, mounted clone

**Isolation Check**:
The verification, run inside the image as the agent runs, that the GitHub tokens, credential stores, and the state
file, clone and skill are unreachable or unwritable from the agent.
_Avoid_: Permission rules, sandbox config
