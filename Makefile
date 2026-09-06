# Контракт курсу: lint, test, run, build. Імена цілей міняти не можна, їх
# перевіряє валідатор. Що саме стоїть за кожною ціллю, вирішуєте ви.

VENV ?= .venv
PY := $(VENV)/bin/python
PIP := $(VENV)/bin/pip
IMAGE ?= starter-service
TAG ?= dev

.PHONY: help install lint test run build clean

help:
	@echo "make install  створити .venv і поставити залежності"
	@echo "make lint     перевірити код лінтером"
	@echo "make test     прогнати тести"
	@echo "make run      підняти сервіс локально"
	@echo "make build    зібрати образ (працює після ЛР6)"
	@echo "make clean    прибрати .venv і кеші"

$(VENV)/bin/python:
	python3 -m venv $(VENV)
	$(PIP) install --upgrade pip
	$(PIP) install -e ".[dev]"

install: $(VENV)/bin/python

lint: install
	$(PY) -m ruff check .

test: install
	$(PY) -m pytest

run: install
	$(PY) -m app

build:
	docker build -t $(IMAGE):$(TAG) .

clean:
	rm -rf $(VENV) .pytest_cache .ruff_cache src/*.egg-info
	find . -name "__pycache__" -type d -prune -exec rm -rf {} +
