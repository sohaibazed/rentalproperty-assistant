# Entry points for setup, checks, and each agent job.
# Secrets come from the environment; see .env.example.

PY := uv run python -m rental_agent

.PHONY: setup test lint gmail-auth sync-email sync-email-dry tax-report generate-lease research-rent \
        init-keys up down logs

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

# --- Web UI in Docker ---------------------------------------------------------------------
# The encryption key and UI password live in your macOS Keychain and reach the container as
# Docker secrets (files under /run/secrets), not as environment variables.
KEY_ITEM := rental-agent-secret-key
PW_ITEM := rental-agent-ui-password

init-keys:  ## One time: create the encryption key and choose a UI password (stored in Keychain)
	@security find-generic-password -s $(KEY_ITEM) >/dev/null 2>&1 \
	  && echo "Encryption key already in Keychain." \
	  || security add-generic-password -a "$$USER" -s $(KEY_ITEM) \
	       -w "$$(uv run python -c 'from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())')"
	@security find-generic-password -s $(PW_ITEM) >/dev/null 2>&1 \
	  && echo "UI password already in Keychain." \
	  || { echo "Choose a password for the web UI:"; security add-generic-password -a "$$USER" -s $(PW_ITEM) -w; }

up:  ## Build and start the web UI at http://localhost:8080
	@RENTAL_SECRET_KEY="$$(security find-generic-password -s $(KEY_ITEM) -w)" \
	 RENTAL_UI_PASSWORD="$$(security find-generic-password -s $(PW_ITEM) -w)" \
	 docker compose up -d --build
	@echo "Open http://localhost:8080"

down:  ## Stop the web UI (data and secrets are kept)
	docker compose down

logs:  ## Follow the web UI's logs
	docker compose logs -f
