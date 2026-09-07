.PHONY: setup run test lint format check clean provision install-service

VENV = .venv
PYTHON = $(VENV)/bin/python
PIP = $(VENV)/bin/pip
PYTEST = $(VENV)/bin/pytest
RUFF = $(VENV)/bin/ruff

setup: $(VENV)/bin/activate
	$(PIP) install -U pip
	$(PIP) install -e ".[dev]"

$(VENV)/bin/activate:
	python -m venv $(VENV)

run: setup
	$(PYTHON) -m app.main

test: setup
	$(PYTEST) tests/ -v

lint: setup
	$(RUFF) check .

format: setup
	$(RUFF) format .

check: lint test

clean:
	rm -rf $(VENV)
	rm -rf *.egg-info
	rm -rf __pycache__
	rm -rf app/**/__pycache__
	rm -rf data/db/*.db
	rm -rf data/media/*
	rm -rf .pytest_cache
	rm -rf .mypy_cache
	rm -rf .ruff_cache

provision:
	bash scripts/provision.sh

install-service:
	bash scripts/install_service.sh

update:
	bash scripts/update.sh

factory-reset:
	bash scripts/factory_reset.sh