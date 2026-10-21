.PHONY: up down reset seed ledger-schema backend-schema validate lint test test-integration test-llm check

up:                 ## start the core stack: Postgres, object store and its bucket-init job
	docker compose up -d

down:               ## stop the stack, keep data
	docker compose down

reset:              ## stop and WIPE all data (re-runs init scripts)
	docker compose down -v

seed:               ## load vendors data, draw invoices, upload them, run the 8 data checks
	uv run python loader/generate.py
	uv run python loader/render.py
	uv run python loader/upload.py
	uv run python loader/validate.py

ledger-schema:      ## create the ledger tables and chart of accounts (dev only: drops and recreates)
	uv run python ledger/apply_schema.py

backend-schema:     ## create the agent schema: runs and steps (dev only: drops and recreates)
	uv run python backend/apply_schema.py

validate:           ## run only the 8 Great Expectations checks
	uv run python loader/validate.py

lint:               ## ruff static checks
	uv run ruff check .

test:               ## fast tests, no Docker needed
	uv run python -m pytest -m "not integration"

test-integration:   ## needs `make up` and `make seed` first; no live model calls
	uv run python -m pytest -m "integration and not live_llm" -v

test-llm:           ## local only: calls the real model through the gateway (loads models, slow)
	uv run python -m pytest -m live_llm -v

check: lint test    ## what CI runs