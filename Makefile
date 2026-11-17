PYTHON ?= python3.12
VENV := .venv
PY := $(VENV)/bin/python

.PHONY: setup db-up db-down migrate doctor test run demo-data demo demo-m2

setup:
	@command -v $(PYTHON) >/dev/null || { echo "Python 3.12 não encontrado (ex.: brew install python@3.12)"; exit 1; }
	$(PYTHON) -m venv $(VENV)
	$(PY) -m pip install --quiet --upgrade pip
	$(PY) -m pip install --quiet -e ".[test]"
	$(PY) scripts/init_env.py

db-up:
	docker compose up -d --wait postgres

db-down:
	docker compose stop postgres

migrate:
	$(PY) -m nemec.dbadmin migrate

doctor:
	$(PY) scripts/doctor.py

test:
	QT_QPA_PLATFORM=offscreen $(PY) -m pytest

run:
	$(PY) -m nemec

demo-data:
	$(PY) scripts/make_demo_data.py

demo:
	$(PY) scripts/demo_m1.py

demo-m2:
	$(PY) scripts/demo_m2.py
