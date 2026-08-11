SHELL := /bin/bash
COMPOSE := docker compose
PY := python3

.DEFAULT_GOAL := help

.PHONY: help
help: ## Show this help
	@grep -hE '^[a-zA-Z_-]+:.*?## ' $(MAKEFILE_LIST) \
		| awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-22s\033[0m %s\n", $$1, $$2}'

# --- setup -----------------------------------------------------------------

.PHONY: secrets
secrets: ## Generate a .env with fresh secrets (never overwrites an existing one)
	@$(PY) scripts/gen_secrets.py

.PHONY: install
install: ## Install the project with dev extras into the current environment
	uv pip install -e '.[dev]' 2>/dev/null || pip install -e '.[dev]'

# --- stand -----------------------------------------------------------------

.PHONY: build
build: ## Build the container images (Asterisk is compiled from source: 10-20 min)
	$(COMPOSE) build

.PHONY: up
up: ## Start the stand
	$(COMPOSE) up -d
	@echo "API: http://127.0.0.1:8000/api/v1/docs"

.PHONY: down
down: ## Stop the stand (volumes are kept)
	$(COMPOSE) down

.PHONY: clean
clean: ## Stop the stand and delete its volumes
	$(COMPOSE) down -v

.PHONY: logs
logs: ## Follow the logs of the control plane
	$(COMPOSE) logs -f masking-app

.PHONY: asterisk-cli
asterisk-cli: ## Attach to the Asterisk CLI
	$(COMPOSE) exec asterisk asterisk -rvvv

.PHONY: migrate
migrate: ## Apply database migrations
	$(COMPOSE) run --rm migrations

.PHONY: demo
demo: ## Full bootstrap: build, start, migrate, seed the pool, print SIP settings
	@if [ ! -f .env ]; then $(MAKE) secrets; fi
	$(COMPOSE) up -d --build
	@echo "waiting for the API to become healthy..."
	@for i in $$(seq 1 60); do \
		curl -fsS http://127.0.0.1:8000/health >/dev/null 2>&1 && break; \
		sleep 2; \
	done
	$(COMPOSE) exec -T -e DEMO_SIP_HOST="$$(hostname -I | awk '{print $$1}')" \
		masking-app python scripts/bootstrap_demo.py

# --- quality ---------------------------------------------------------------

.PHONY: test
test: ## Run unit tests (no external services required)
	pytest -m "not integration" -q

.PHONY: test-integration
test-integration: ## Run every test against PostgreSQL and Redis from the stand
	$(COMPOSE) up -d postgres redis
	@echo "waiting for postgres..."
	@for i in $$(seq 1 30); do \
		$(COMPOSE) exec -T postgres pg_isready -q && break; sleep 1; \
	done
	@# Credentials come from .env; the tests use a separate <db>_test database
	@# and Redis database 15, so the running stand is never touched.
	set -a && . ./.env && set +a && REDIS_URL=$${REDIS_URL%/*}/15 pytest -q

.PHONY: lint
lint: ## Static checks
	ruff check app tests scripts
	ruff format --check app tests scripts

.PHONY: fmt
fmt: ## Autoformat
	ruff format app tests scripts
	ruff check --fix app tests scripts

.PHONY: openapi
openapi: ## Export the OpenAPI specification to docs/openapi.json
	@$(PY) -c "import json, pathlib; from app.main import create_app; \
pathlib.Path('docs/openapi.json').write_text(json.dumps(create_app().openapi(), indent=2, ensure_ascii=False))"
	@echo "docs/openapi.json updated"

.PHONY: sounds
sounds: ## Regenerate the Russian voice prompts (Piper TTS, in a container)
	docker build -t masking-sounds asterisk/sounds
	docker run --rm -v "$(CURDIR)/asterisk/sounds/ru/custom:/out" masking-sounds /out

.PHONY: check-sounds
check-sounds: ## Recognise the prompts back and report how intelligible they are
	docker build -t masking-sounds-check tests/sounds
	docker run --rm -v "$(CURDIR)/asterisk/sounds/ru/custom:/sounds:ro" \
		masking-sounds-check /sounds

.PHONY: api-smoke
api-smoke: ## Run the Bruno collection against the running stand
	docker build -t masking-api-smoke tests/api
	set -a && . ./.env && set +a && \
	docker run --rm --network host -v "$(CURDIR)/docs/bruno:/collection" \
		masking-api-smoke run --env local --env-var apiKey="$$API_KEYS"

.PHONY: check-leaks
check-leaks: ## Verify that no full phone number appears in the logs
	@scripts/check_log_leaks.sh
