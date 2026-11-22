PY ?= python3
TENANT ?= acme

.PHONY: setup load profile chat scan test eval eval-live fixture lint

setup:
	$(PY) -m pip install -e ".[all,dev]"

load:
	fleet load

profile:
	fleet profile

chat:
	fleet chat --tenant $(TENANT)

scan:
	fleet scan --tenant $(TENANT)

test:
	$(PY) -m pytest

fixture:
	$(PY) -m evals.fixtures.generate

eval:
	fleet eval --mode recorded

eval-live:
	fleet eval --mode live --record

lint:
	ruff check fleet_copilot evals tests
