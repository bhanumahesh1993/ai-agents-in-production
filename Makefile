# AI Agents in Production — companion code
#
# Every target below runs in mock mode: no API key, no network, no cloud.

PY ?= python3
VENV ?= .venv
BIN := $(VENV)/bin

.DEFAULT_GOAL := help

.PHONY: help
help:  ## Show this help
	@grep -E '^[a-zA-Z0-9_-]+:.*?## ' $(MAKEFILE_LIST) \
		| awk 'BEGIN {FS = ":.*?## "}; {printf "  %-18s %s\n", $$1, $$2}'

.PHONY: install
install:  ## Create the venv and install in editable mode with dev extras
	$(PY) -m venv $(VENV)
	$(BIN)/pip install --upgrade pip
	$(BIN)/pip install -e ".[dev]"

.PHONY: test
test:  ## Run the full offline test suite
	$(BIN)/pytest -q

.PHONY: lint
lint:  ## ruff + mypy
	$(BIN)/ruff check .
	$(BIN)/mypy

.PHONY: fmt
fmt:  ## Apply ruff's safe fixes
	$(BIN)/ruff check --fix .

.PHONY: clean
clean:  ## Remove caches and build output
	rm -rf .pytest_cache .mypy_cache .ruff_cache dist build
	find . -name __pycache__ -type d -prune -exec rm -rf {} +

# --- chapter demos ----------------------------------------------------------
# Each demo prints what it proves and exits non-zero if the property it
# demonstrates does not hold, so `make demos` doubles as a smoke test.

.PHONY: demo-ch01
demo-ch01:  ## The double refund, and the derived-idempotency-key repair
	$(BIN)/python -m artifacts.ch01_first_agent.demo

.PHONY: demos
demos:  ## Run every chapter demo that exists
	@for d in artifacts/*/demo.py; do \
		[ -f "$$d" ] || continue; \
		echo "--- $$d"; \
		$(BIN)/python "$$d" || exit 1; \
	done
