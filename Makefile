# PM Agent reference implementation — developer entry points (workspace rule: always use .venv).
PY ?= .venv/bin/python
PYTEST = $(PY) -m pytest -p no:cacheprovider

.PHONY: help install lint typecheck policy complexity test test-unit test-contract test-component test-arch \
        test-property coverage evals placeholders check clean

help:
	@echo "install       install the package + dev tools into .venv (editable)"
	@echo "check         policy lint + pylint + mypy --strict + complexity budget + tests with coverage gate"
	@echo "test-arch     architecture fitness tests (layering, LLM-side import closure)"
	@echo "test-property property-based tests (HYPOTHESIS_PROFILE=dev explores more examples)"
	@echo "coverage      tests with the branch-coverage gate and a missing-lines report"
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

complexity:
	$(PY) tools/complexity_budget.py

test:
	$(PYTEST)

test-unit:
	$(PYTEST) tests/unit

test-contract:
	$(PYTEST) -m contract

test-component:
	$(PYTEST) -m component

test-arch:
	$(PYTEST) -m architecture

test-property:
	$(PYTEST) -m property

coverage:
	$(PYTEST) --cov --cov-report=term-missing

evals:
	$(PY) evals/run_offline.py

placeholders:
	$(PY) tools/list_placeholders.py --write PLACEHOLDERS.md

check: policy lint typecheck complexity coverage

clean:
	rm -rf .pytest_cache .mypy_cache .coverage htmlcov build dist src/*.egg-info
