# PM Agent reference implementation — developer entry points (workspace rule: always use .venv).
PY ?= .venv/bin/python

.PHONY: help install lint typecheck policy test test-unit test-contract test-component evals placeholders check clean

help:
	@echo "install       install the package + dev tools into .venv (editable)"
	@echo "check         policy lint + pylint + mypy --strict + pytest (CI gates 1-4)"
	@echo "evals         offline evaluation baseline (synthetic golden signals, no credentials)"
	@echo "placeholders  regenerate PLACEHOLDERS.md from PLACEHOLDER tokens"

install:
	$(PY) -m pip install -e ".[dev]"

policy:
	$(PY) tools/policy_lint.py

lint:
	$(PY) -m pylint src/pm_agent tools

typecheck:
	$(PY) -m mypy

test:
	$(PY) -m pytest

test-unit:
	$(PY) -m pytest tests/unit

test-contract:
	$(PY) -m pytest -m contract

test-component:
	$(PY) -m pytest -m component

evals:
	$(PY) evals/run_offline.py

placeholders:
	$(PY) tools/list_placeholders.py --write PLACEHOLDERS.md

check: policy lint typecheck test

clean:
	rm -rf .pytest_cache .mypy_cache build dist src/*.egg-info
