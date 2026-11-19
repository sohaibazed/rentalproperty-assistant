# Entry points for setup, checks, and each agent job.
# Secrets come from the environment; see .env.example.

PY := uv run python -m rental_agent

.PHONY: setup test lint gmail-auth sync-email sync-email-dry tax-report generate-lease research-rent \
        tax-protest reference-sql import-document backup restore-backup init-keys set-login-email \
        tls-cert up down logs

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

tax-protest:  ## Collin County protest evidence, forms and dates: make tax-protest PROPERTY=1 [YEAR=2027] [CAD_ID=123456] [HEARING=2027-06-10]
	$(PY) tax-protest --property-id $(PROPERTY) $(if $(YEAR),--year $(YEAR)) $(if $(CAD_ID),--cad-id $(CAD_ID)) $(if $(HEARING),--hearing-date $(HEARING))

reference-sql:  ## Build reference/supabase-documents.sql from your lease and LLC documents
	uv run python scripts/build_reference_sql.py

import-document:  ## Preview a document import; APPLY=1 saves. FILE= and DATA= under artifacts/imports/
	@test -n "$(FILE)" -a -n "$(DATA)" || { echo "Usage: make import-document FILE=artifacts/imports/x.pdf DATA=artifacts/imports/x.json [APPLY=1]"; exit 1; }
	docker exec rental-agent python -m rental_agent import-document "/app/$(FILE)" --data "/app/$(DATA)" $(if $(APPLY),--apply)

backup:  ## Back up the database, artifacts/ and memory/ now (the scheduler does this daily)
	docker exec rental-agent python -m rental_agent backup

# RESTORE_DB_URL is read from your shell and passed by name, so it never shows in a command line.
restore-backup:  ## Load a backup into an empty database: RESTORE_DB_URL=... make restore-backup FILE=artifacts/backups/x.zip [OVERWRITE=1]
	@test -n "$(FILE)" -a -n "$$RESTORE_DB_URL" || { echo "Usage: RESTORE_DB_URL=<owner connection string> make restore-backup FILE=artifacts/backups/rental-backup-YYYY-MM-DD.zip [OVERWRITE=1]"; exit 1; }
	docker exec -e RESTORE_DB_URL rental-agent python -m rental_agent restore-backup "/app/$(FILE)" $(if $(OVERWRITE),--overwrite)

# --- Web UI in Docker ---------------------------------------------------------------------
# The encryption key and UI password live in your macOS Keychain and reach the container as
# Docker secrets (files under /run/secrets), not as environment variables. The login email
# is also kept in Keychain (it's personal, so not in this repo) and passed as a plain variable.
KEY_ITEM := rental-agent-secret-key
PW_ITEM := rental-agent-ui-password
USER_ITEM := rental-agent-ui-username

init-keys:  ## One time: create the encryption key and choose a login email + password (Keychain)
	@security find-generic-password -s $(KEY_ITEM) >/dev/null 2>&1 \
	  && echo "Encryption key already in Keychain." \
	  || security add-generic-password -a "$$USER" -s $(KEY_ITEM) \
	       -w "$$(uv run python -c 'from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())')"
	@security find-generic-password -s $(PW_ITEM) >/dev/null 2>&1 \
	  && echo "UI password already in Keychain." \
	  || { echo "Choose a password for the web UI:"; security add-generic-password -a "$$USER" -s $(PW_ITEM) -w; }
	@security find-generic-password -s $(USER_ITEM) >/dev/null 2>&1 \
	  && echo "Login email already in Keychain." \
	  || { read -r -p "Login email for the web UI: " email \
	       && security add-generic-password -a "$$USER" -s $(USER_ITEM) -w "$$email"; }

set-login-email:  ## Change the login email: make set-login-email EMAIL=you@example.com
	@test -n "$(EMAIL)" || { echo "Usage: make set-login-email EMAIL=you@example.com"; exit 1; }
	@security add-generic-password -U -a "$$USER" -s $(USER_ITEM) -w "$(EMAIL)"
	@echo "Login email saved. Run make up to apply it."

# HTTPS uses a self-signed certificate for localhost. The certificate and its private key are
# files in a folder outside this repo (set RENTAL_TLS_DIR to move it) and reach the container
# as Docker secrets. The key is not a login secret: it only encrypts the connection between
# your browser and the app on this Mac.
RENTAL_TLS_DIR ?= $(HOME)/.config/rental-agent/tls

tls-cert:  ## Create the self-signed HTTPS certificate if there is none (make up runs this)
	@if [ -f "$(RENTAL_TLS_DIR)/cert.pem" ] && [ -f "$(RENTAL_TLS_DIR)/key.pem" ]; then \
	   echo "HTTPS certificate already in $(RENTAL_TLS_DIR)."; \
	 else \
	   mkdir -p "$(RENTAL_TLS_DIR)" && chmod 700 "$(RENTAL_TLS_DIR)" && umask 077 \
	   && out="$$(openssl req -x509 -newkey rsa:2048 -nodes -days 825 \
	        -keyout "$(RENTAL_TLS_DIR)/key.pem" -out "$(RENTAL_TLS_DIR)/cert.pem" \
	        -subj "/CN=localhost" \
	        -addext "subjectAltName=DNS:localhost,IP:127.0.0.1,IP:::1" \
	        -addext "extendedKeyUsage=serverAuth" 2>&1)" \
	   || { echo "$$out"; echo "Could not create the HTTPS certificate."; exit 1; }; \
	   echo "Created a self-signed HTTPS certificate in $(RENTAL_TLS_DIR), valid for 825 days."; \
	 fi

up: tls-cert  ## Build and start the web UI at https://localhost:8080
	@RENTAL_SECRET_KEY="$$(security find-generic-password -s $(KEY_ITEM) -w)" \
	 RENTAL_UI_PASSWORD="$$(security find-generic-password -s $(PW_ITEM) -w)" \
	 RENTAL_UI_USERNAME="$$(security find-generic-password -s $(USER_ITEM) -w)" \
	 RENTAL_TLS_DIR="$(RENTAL_TLS_DIR)" \
	 docker compose up -d --build
	@echo "Open https://localhost:8080 (the browser warns once about the self-signed certificate)"

down:  ## Stop the web UI (data and secrets are kept)
	docker compose down

logs:  ## Follow the web UI's logs
	docker compose logs -f
