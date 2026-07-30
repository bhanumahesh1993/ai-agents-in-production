# AI Agents in Production — companion code
#
# Every target below runs in mock mode: no API key, no network, no cloud.

PY ?= python3
VENV ?= .venv
BIN := $(VENV)/bin
# Use the venv when present, otherwise whatever python is active. CI
# installs into the job environment and has no .venv.
PY_RUN := $(shell [ -x $(VENV)/bin/python ] && echo $(VENV)/bin/python || echo python)

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
	$(BIN)/python artifacts/ch01-first-agent/demo.py

.PHONY: demos
demos:  ## Run every chapter demo, offline; fails if any exits non-zero
	@set -e; \
	found=0; \
	for d in artifacts/ch*/demo.py; do \
		[ -f "$$d" ] || continue; \
		found=$$((found+1)); \
		echo "--- $$d"; \
		$(PY_RUN) "$$d"; \
	done; \
	echo "ran $$found chapter demo(s)"

.PHONY: demo-all-offline
demo-all-offline: demos  ## Alias: run every chapter demo offline

.PHONY: paths
paths:  ## Prove every artifact path printed in the book exists here
	$(PY_RUN) tools/check_printed_paths.py

.PHONY: manifest
manifest:  ## Print the chapter-to-files-and-tests manifest
	@$(PY_RUN) tools/manifest.py
