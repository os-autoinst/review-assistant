#!/bin/bash
# Started by `review-assistant check-isolation` with the environment and write confinement the review agent gets.
# Exits 0 only if the agent cannot reach a GitHub token or credential store and cannot write outside its scratch.
# The Vertex ADC stays readable by design: accepted risk D21 of tasks/001_210027_automated_model_review.md.
set -uo pipefail

state=$1 clone=$2 skill=$3 scratch=$4
failures=0

check() {
	local desc=$1
	shift
	if "$@" >/dev/null 2>&1; then
		echo "ok: $desc"
	else
		echo "FAIL: $desc"
		failures=$((failures + 1))
	fi
}
not_() { ! "$@"; }
token_in_env() { env | grep -qE '^(REVIEW_ASSISTANT_|GH_TOKEN=|GITHUB_TOKEN=|GH_ENTERPRISE_TOKEN=)'; }
git_fills_password() {
	printf 'protocol=https\nhost=github.com\n\n' |
		GIT_TERMINAL_PROMPT=0 GIT_ASKPASS=/bin/false SSH_ASKPASS=/bin/false git credential fill 2>/dev/null |
		grep -q '^password='
}
hermes_denies_write() {
	(cd /opt/hermes-agent && .venv/bin/python -c '
import sys
from agent.file_safety import get_write_denied_error
sys.exit(0 if get_write_denied_error(sys.argv[1]) else 1)' "$1")
}

check "no token variable in the agent environment" not_ token_in_env
check "environment of the CLI (pid $PPID) unreadable" not_ cat "/proc/$PPID/environ"
for token_file in /run/secrets/gh-read-token /etc/review-assistant/secrets/gh-read-token; do
    check "no read-token file at $token_file" test ! -e "$token_file"
done
for store in "$HOME/.config/gh/hosts.yml" "$HOME/.git-credentials" "$HOME/.netrc" "$HOME/.config/git/credentials"; do
	check "no credential store $store" test ! -e "$store"
done
check "gh auth token yields nothing" not_ gh auth token
check "git credential fill yields no password" not_ git_fills_password
for path in "$state" "$clone/.git/config" "$skill/skills/openqa/scripts/review-lint.py" "$HOME/.gitconfig" \
    "${HERMES_HOME:-/hermes}/config.yaml" /config/reviewer.yaml "$(dirname "$scratch")/pr-1/review-1.json"; do
	check "hermes refuses writing $path" hermes_denies_write "$path"
done
check "hermes allows writing the scratch $scratch" not_ hermes_denies_write "$scratch/review.json"
check "skill checkout not writable" not_ touch "$skill/.isolation-probe"
echo "accepted (D21): the Vertex ADC under ${CLOUDSDK_CONFIG:-unset} is readable by the agent"
[[ $failures -eq 0 ]]
