# Zapproof — Makefile
# Targets per prompt Section 4.
#
# NOTE: recipe lines begin with a real TAB character. Do not convert to spaces.
#
# env-check runs on the *system* python on purpose: one of the things it checks is
# whether uv is installed, so it must not depend on uv. Every other target runs
# inside the project environment that uv manages.

UV  ?= uv
PY  ?= python
RUN ?= $(UV) run python

.PHONY: help env-check test lint format gate-0 gate-A gate-B gate-C gate-D report metrics clean

help:
	@echo "targets: env-check test lint format gate-0 gate-A gate-B gate-C gate-D report metrics clean"

env-check:
	$(PY) scripts/env_check.py

test:
	$(RUN) -m pytest

lint:
	$(RUN) -m ruff check .
	$(RUN) -m mypy

format:
	$(RUN) -m ruff format .
	$(RUN) -m ruff check --fix .

gate-0:
	$(RUN) gates/gate_0.py

# Gate targets for later phases. The gate script MUST be written before the phase
# is implemented (prompt Section 3.4.1), so these fail loudly rather than
# pretending to pass.
gate-A:
	$(RUN) gates/gate_A.py

gate-B:
	$(RUN) gates/gate_B.py

gate-C:
	@echo "gates/gate_C.py does not exist yet. Write it BEFORE implementing Phase C (prompt 3.4.1)."; exit 1

gate-D:
	@echo "gates/gate_D.py does not exist yet. Write it BEFORE implementing Phase D (prompt 3.4.1)."; exit 1

# Generated report. Not implemented until Phase A (prompt Section 7, task A7).
report:
	$(RUN) scripts/generate_report.py

# Usage metrics (task B7). Reports unique external callers over the market window.
# Configure the owner's own addresses in USAGE_OWNER_IPS so they are excluded.
metrics:
	$(RUN) scripts/metrics.py

clean:
	@echo "nothing to clean yet"
