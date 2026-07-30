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

.PHONY: demo-ch02
demo-ch02:  ## Checkpoint placement, a killed worker, and a clean resume
	$(BIN)/python artifacts/ch02-harness/demo.py

.PHONY: demo-ch03
demo-ch03:  ## One triage agent three ways, and the seven-criteria scorecard
	$(BIN)/python artifacts/ch03-three-ways/demo.py

.PHONY: demo-ch04
demo-ch04:  ## Six reasoning patterns, one task, measured cost
	$(BIN)/python artifacts/ch04-patterns/demo.py

.PHONY: demo-ch05
demo-ch05:  ## Isolated readers, then two writers who disagree
	$(BIN)/python artifacts/ch05-orchestrator/demo.py

.PHONY: demo-ch06
demo-ch06:  ## Supervisor versus swarm, and the handoff that pays twice
	$(BIN)/python artifacts/ch06-topologies/demo.py

.PHONY: demo-ch25
demo-ch25:  ## Budgets, a tenant-scoped cache, and a router priced per success
	$(PY_RUN) artifacts/ch25-cost/demo.py

.PHONY: demo-ch26
demo-ch26:  ## The release gates, the shadow diff, the canary, and the drill
	$(PY_RUN) artifacts/ch26-cicd/demo.py

.PHONY: demo-ch27
demo-ch27:  ## Validate trend-tracker.md, the dated claims file
	$(PY_RUN) artifacts/ch27-trends/demo.py

# ARGS is passed through, so `make demo-ch28 ARGS="--grade --drift 0.2"`
# works as well as the dedicated grade target below.
ARGS ?=

.PHONY: demo-ch28
demo-ch28:  ## The capstone: four Northstar cases, end to end
	$(PY_RUN) artifacts/ch28-capstone/demo.py $(ARGS)

.PHONY: demo-ch28-grade
demo-ch28-grade:  ## The capstone's pass^k report with confidence intervals
	$(PY_RUN) artifacts/ch28-capstone/demo.py --grade

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
