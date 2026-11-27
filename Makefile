SOURCE_FILES ?= $(shell git ls-files "**.py")
ISOLATE ?= 1

ifeq ($(ISOLATE),1)
UNSHARE := unshare -r -n
else
UNSHARE :=
endif

.PHONY: help
help: ## Display this help
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) | sort | awk 'BEGIN {FS = ":.*?## "}; {printf "\033[36m%-30s\033[0m %s\n", $$1, $$2}'

.PHONY: all
all: help

# Run tools through the locked uv environment by default; fall back to distribution packages on PATH
# if uv is missing or cannot be called. Override, e.g. PYTHON_RUN="" to skip uv.
UV_AVAILABLE := $(shell uv --version >/dev/null 2>&1 && echo 1)
ifeq ($(UV_AVAILABLE),1)
PYTHON_RUN ?= uv run
else
$(warning uv not found or not callable; falling back to distribution-installed packages, versions may differ from uv.lock)
PYTHON_RUN ?=
endif

# Detect if pytest-xdist is installed for parallel testing
PYTEST_XDIST := $(shell $(PYTHON_RUN) python3 -c "import xdist" 2>/dev/null && echo "-n auto" || echo "")

.PHONY: test-unit
test-unit: ## Run dynamic tests with coverage report
	$(UNSHARE) $(PYTHON_RUN) python3 -m pytest $(PYTEST_XDIST) --cov --cov-report=xml --cov-report=term-missing

.PHONY: test-unit-no-coverage
test-unit-no-coverage: ## Run dynamic tests without coverage analysis and without style checks
	$(UNSHARE) $(PYTHON_RUN) python3 -m pytest $(PYTEST_XDIST)

.PHONY: only-test-with-coverage
only-test-with-coverage: test-unit  ## Alias for "test-unit"

.PHONY: check-ruff
check-ruff: ## Run ruff linting and formatting checks
	$(PYTHON_RUN) ruff check
	$(PYTHON_RUN) ruff format --check

.PHONY: tidy
tidy: ## Format code and fix linting issues
	$(PYTHON_RUN) ruff format
	$(PYTHON_RUN) ruff check --fix

.PHONY: check-conventions
check-conventions: ## Check for banned coding patterns
	@if git grep -nE '^\s*@(unittest\.mock\.|mock\.)?patch' tests/; then \
		echo "Error: @patch decorator detected. Avoid to prevent argument ordering bugs."; \
		echo "   Fix: Use the 'mocker' fixture (pytest-mock) or a 'with patch():' context manager."; \
		exit 1; \
	fi

.PHONY: check-code-health
check-code-health: ## Find dead code (vulture)
	@echo "Checking code health…"
	@$(PYTHON_RUN) vulture ${SOURCE_FILES} --min-confidence 80

.PHONY: check-maintainability
check-maintainability: ## Ensure Radon maintainability index grade A for source files
	@out=$$($(PYTHON_RUN) radon mi -s review_assistant) || exit 1; \
	echo "$$out"; \
	if echo "$$out" | grep -qv ' - A '; then \
		echo "maintainability below grade A" >&2; \
		exit 1; \
	fi

.PHONY: check-types-ty
check-types-ty: ## Run ty type checker
	$(PYTHON_RUN) ty check

.PHONY: check-types
check-types: check-types-ty

.PHONY: check-k8s
check-k8s: ## Validate Kubernetes manifests and scan for committed credentials
	deploy/kubernetes/validate.sh

.PHONY: check-lock
check-lock: ## Verify uv.lock is in sync with pyproject.toml
ifeq ($(UV_AVAILABLE),1)
	uv lock --check
else
	@echo "uv not available; skipping uv.lock check"
endif

.PHONY: check-tool-versions
check-tool-versions: ## Warn if the ty on PATH differs from the locked version
	@if [ "$(UV_AVAILABLE)" = "1" ]; then \
		locked=$$(uv export --locked --only-dev --no-hashes 2>/dev/null | sed -n 's/^ty==//p'); \
		local=$$($(PYTHON_RUN) ty --version 2>/dev/null | awk '{print $$2}'); \
		if [ -n "$$locked" ] && [ "$$locked" != "$$local" ]; then \
			echo "Warning: ty $$local on PATH but uv.lock pins ty $$locked; results may differ from CI"; \
		fi; \
	else \
		echo "uv not available; skipping tool version comparison"; \
	fi

# aggregate targets

.PHONY: checkstyle
checkstyle: ## Run fast style and static analysis checks
	@$(MAKE) -j check-ruff check-conventions check-types check-lock check-tool-versions check-maintainability

.PHONY: checkstyle-all
checkstyle-all: ## Run all style and static analysis checks
	@$(MAKE) -j checkstyle check-code-health

.PHONY: test
test: ## Run all tests with coverage analysis and style checks
	@$(MAKE) -j test-unit checkstyle-all

.PHONY: test-no-coverage
test-no-coverage: ## Run all tests *without* coverage analysis and style checks (faster)
	@$(MAKE) -j test-unit-no-coverage checkstyle-all

.PHONY: test-with-coverage
test-with-coverage: test  ## Alias for "test"

.PHONY: setup-hooks
setup-hooks: ## Install pre-commit git hooks
	pre-commit install --install-hooks -t commit-msg -t pre-commit
