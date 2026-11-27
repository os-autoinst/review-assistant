# review-assistant Agent Guidelines

Python CLI tool for automated, model-based review of openQA pull requests. Uses: Python 3.13,
requests, PyYAML, pydantic, ruff, ty, pytest.

## Build & Test Commands

- `make tidy`: Format code with ruff.
- `make checkstyle`: Run fast linting (ruff, conventions, ty, lock, radon).
- `make check-types`: Type checking.
- `make test`: Run all tests with full coverage.
- `make setup-hooks`: Install pre-commit and gitlint hooks.

## Coverage Verification

- Always verify 100% statement AND branch coverage for all files using `make test`.
- Do NOT just look at the "passed" test count. You MUST check the exit code of `make test`. If it
  exits with an error (e.g. `Error 2` or `Coverage failure`), you must fix the coverage before
  committing.
- If an `elif` or `else` branch is logically unreachable (e.g. due to prior filtering), refactor the
  code to eliminate the unreachable branch to satisfy the coverage tool.

## Maintainability Verification

- Maintain Grade A (Radon MI >= 20) for all modified files.
- DO NOT use redundant comments or filler docstrings to artificially inflate MI scores.
- Achieve maintainability through genuine simplification: reduce complexity, nesting, and operands.
- Prefer private functions (`_` prefix) for internal helpers to bypass `ruff` docstring requirements
  (`D103`) instead of adding low-value documentation.

## Test Guidelines

- Prefer `@pytest.mark.parametrize` over individual single-assertion test functions for data-driven
  coverage. Use tuple form for argument names: `("arg1", "arg2")` not `"arg1,arg2"` (ruff PT006).
- Use the `mocker` fixture (pytest-mock) instead of `@patch` decorators.
- Test descriptions go in the test or parameter `id`/`desc` strings, never as in-file comments.
- Prefer adding test coverage in existing test files. Only when a completely new module is added
  should a new test file be created.

## Plan Execution

- When an execution plan file exists in `tasks/`, read it fully before starting implementation.
  Follow the steps in order.
- When the user amends a plan, re-read it to identify what changed rather than re-deriving from
  scratch.

## Commit guidelines

- Commit titles follow conventional commits (enforced by gitlint).
- Never leave `fixup!`, `squash!` or `amend!` commits in a branch: the CI commit message check rejects
  them. `gitlint --commits` skips cleanup commits locally unless the three options
  `-c general.ignore-fixup-commits=false`, `-c general.ignore-squash-commits=false` and
  `-c general.ignore-fixup-amend-commits=false` are passed, which is what CI does.
- For any non-trivial changes, especially "fix" or "feat" commits, mention motivation, design
  choices and user benefits in the commit message body.
- Commit message format: 50/80 rule, 80-char limit.

## Architecture invariants

- The GitHub client posts reviews with `event: COMMENT` or `event: APPROVE` only (approved 2026-11-10,
  D1). `REQUEST_CHANGES`, `DISMISS` and any other event are rejected in code and must stay rejected.
- The write token is read only from `REVIEW_ASSISTANT_GH_TOKEN` and the read-only token only from
  `REVIEW_ASSISTANT_GH_READ_TOKEN`. Neither may be read from or written to a configuration file.
- Posting to GitHub happens only with `--post`. Dry-run is the default.

## Constraints

- `tasks/`: Read/write for planning. Never run git operations on this directory.
- Never run `git clean` or any command that deletes unversioned files. Ask for confirmation.
- **Style Exclusions**: NEVER suppress linter, type-checker, or coverage errors just to bypass them;
  you MUST refactor instead. Exclusions are ONLY permitted when dictated by external library
  interfaces (e.g. `# noqa: ANN401` for `Any` types). Such unavoidable exclusions MUST target the
  specific rule and include an explanatory comment.
