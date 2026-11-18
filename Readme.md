# review-assistant

Automated, model-based pull request review for `os-autoinst/openQA`, run from a separate, non-personal GitHub
account. Reviews follow the `os-autoinst/openQA-skill` playbook and use `google/gemini-3.8-flash` through Hermes'
`google-vertex` provider. Only `COMMENT` and `APPROVE` events are allowed (D1, D5); merging is out of scope.

Status: pilot. OpenPlatform deployment runbook: `docs/openplatform-rancher-deployment.md`. Tracker:
https://progress.opensuse.org/issues/210027

## Setup on this host

1. Operator config, no credentials: `cp config/reviewer.example.yaml config/reviewer.yaml` (provider, model, toolset,
   pinned skill SHA).
2. Secrets under `~/.config/review-assistant`, mode `600`, outside the repository (D19):
   - `read-token`: read-only fine-grained token (contents, metadata, pull requests read)
   - `write.env`: one line `REVIEW_ASSISTANT_GH_TOKEN=<token>`
   - ADC: `~/.config/gcloud/application_default_credentials.json`; set the Vertex project ID in
     `deploy/hermes/config.yaml` (the committed file holds the placeholder; do not commit the real value)
3. Host tools: `uv sync` creates `.venv` with `review-assistant-container`.
4. Build: `podman build -t review-assistant:dev .`
5. Isolation check (the agent's own view of what it can reach): `.venv/bin/review-assistant-container --check-isolation`
   exits 0.
6. Install the units: `mkdir -p ~/.config/systemd/user && cp deploy/review-assistant.service deploy/review-assistant.timer ~/.config/systemd/user/ && systemctl --user daemon-reload`
7. Hourly dry-run timer: `systemctl --user enable --now review-assistant.timer`

No openQA clone setup is needed: each run clones the repository shallow into its own scratch directory.

## Runs

- Dry-run is the default: reviews are printed, nothing is posted, no state is written.
- `review-assistant-container --post` posts reviews. `--limit N` caps new reviews per run (default `pilot_cap`).
  State records each posted review by head SHA, so a PR already reviewed at its current head is skipped.
- `--post` fails before any listing when `REVIEW_ASSISTANT_GH_TOKEN` is unset.
- The CLI takes both tokens out of its own environment before Hermes starts and makes itself non-dumpable, so the
  agent cannot read them. The agent can read the ADC (accepted risk, D21); reviews carrying a token or credential
  pattern are refused before they are posted, and token values are redacted from log lines.

## Rollback

1. `systemctl --user disable --now review-assistant.timer`
2. Revoke both GitHub tokens, and rotate the ADC if a key leaked.
3. Delete `~/.local/state/review-assistant/state.json`.
4. D10: 3 or more AI-vs-human verdict conflicts stop the pilot.
