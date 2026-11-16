# Entry points for setup, checks, and each agent job.
# Secrets come from the environment; see .env.example.

PY := uv run python -m rental_agent

.PHONY: setup test lint gmail-auth sync-email sync-email-dry tax-report generate-lease research-rent

setup:  ## Create .venv and install dependencies
	uv sync

test:
	uv run pytest -q

lint:
	uv run ruff check src tests

gmail-auth:  ## One-time browser consent for read-only Gmail access
	$(PY) gmail-auth

sync-email:  ## Import rent/expense emails since the last successful run
	$(PY) sync-email

sync-email-dry:  ## Show what sync-email would import, without writing anything
	$(PY) sync-email --dry-run

tax-report:  ## Year-end Schedule E summary; override with YEAR=2026
	$(PY) tax-report $(if $(YEAR),--year $(YEAR))

generate-lease:  ## Yearly lease extension: make generate-lease PROPERTY=1 RENT=2600 [DRY=1]
	$(PY) generate-lease --property-id $(PROPERTY) $(if $(RENT),--rent $(RENT)) $(if $(DRY),--dry-run)

research-rent:  ## Rent recommendation from comps: make research-rent PROPERTY=1 [YEAR=2027] [DRY=1]
	$(PY) research-rent --property-id $(PROPERTY) $(if $(YEAR),--year $(YEAR)) $(if $(DRY),--dry-run)
