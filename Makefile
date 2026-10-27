# =============================================================================
# Store-IO - Root Makefile
# =============================================================================

SHELL := /bin/bash
.DEFAULT_GOAL := help

SECRETS_DIR ?= .secrets
FORCE ?= false

PRE_COMMIT ?= pre-commit
RUFF_VERSION ?= 0.17.2
RUFF ?= uvx ruff@$(RUFF_VERSION)

DEMO_PY ?= uv run --with httpx==0.28.1 --no-project python
EVENTS_PY ?= uv run --with aio-pika==10.1.1 --no-project python

# ===== Services ===== #
# Docker Compose service names (must match docker-compose.yml).
SERVICES := warehouse-service order-service

SERVICE_DIR_order-service    := services/order
SERVICE_DIR_warehouse-service := services/warehouse

# Default service for logs/shell when no [service=...] is given.
service ?= warehouse-service

# ===== Docker Compose ===== #
COMPOSE_BASE     := docker-compose.yml
COMPOSE_OVERRIDE := docker-compose.override.yml
COMPOSE_CI       := docker-compose.ci.yml

ifeq ($(ENV),development)
  COMPOSE_FILES = -f $(COMPOSE_BASE) $(if $(wildcard $(COMPOSE_OVERRIDE)),-f $(COMPOSE_OVERRIDE))
else ifeq ($(ENV),ci)
  COMPOSE_FILES = -f $(COMPOSE_BASE) -f $(COMPOSE_CI)
else
  COMPOSE_FILES = -f $(COMPOSE_BASE)
endif

DOCKER_COMPOSE := docker compose $(COMPOSE_FILES)

# ===== Colors ===== #
GREEN  := \033[0;32m
YELLOW := \033[1;33m
RED    := \033[0;31m
NC     := \033[0m

.PHONY: help init-secrets lint format typecheck test \
        build build-fast update audit ps logs shell \
        up down restart fresh demo demo-fast seed events migrate dashboard \
        $(SERVICES)

# =============================================================================
# Help
# =============================================================================

help: ## Show available commands.
	@printf "\n$(YELLOW)Store-IO root commands:$(NC)\n"
	@grep -hE '^[a-zA-Z0-9_-]+:.*?## .*$$' $(MAKEFILE_LIST) | \
		awk 'BEGIN {FS = ":.*?## "}; {printf "  $(GREEN)%-24s$(NC) %s\n", $$1, $$2}'
	@grep -hE '^## ' $(MAKEFILE_LIST) | \
		awk -F '## |—' '{ name = $$2; gsub(/[[:space:]]+$$/, "", name); desc = $$3; sub(/^[[:space:]]+/, "", desc); printf "  $(GREEN)%-24s$(NC) %s\n", name, desc }'
	@printf "\n"

# =============================================================================
# Secrets & Code Quality (root-level, unchanged)
# =============================================================================

init-secrets: ## Create local secrets (idempotent). Usage: make init-secrets [FORCE=true]
	@printf "$(YELLOW)🔐 Initialising secrets in $(SECRETS_DIR)...$(NC)\n"
	@FORCE="$(FORCE)" SECRETS_DIR="$(CURDIR)/$(SECRETS_DIR)" bash scripts/init-secrets.sh
	@printf "$(GREEN)✅ Secrets initialised.$(NC)\n"

lint: ## Run every pre-commit hook against all tracked files.
	@printf "$(YELLOW)🔍 Running pre-commit hooks...$(NC)\n"
	@$(PRE_COMMIT) run --all-files --show-diff-on-failure
	@printf "$(GREEN)✅ Lint & format checks passed.$(NC)\n"

format: ## Auto-fix and format Python code with ruff.
	@printf "$(YELLOW)🎨 Formatting Python code with ruff $(RUFF_VERSION)...$(NC)\n"
	@$(RUFF) format .
	@$(RUFF) check . --fix
	@printf "$(GREEN)✅ Python code formatted and fixed.$(NC)\n"

typecheck: ## Static type checking with mypy.
	@printf "$(YELLOW)🧠 Running mypy...$(NC)\n"
	@$(PRE_COMMIT) run mypy --all-files
	@printf "$(GREEN)✅ Type checks passed.$(NC)\n"

test: ## Run the unit test suite with pytest (no Docker required).
	@printf "$(YELLOW)🧪 Running tests...$(NC)\n"
	@uv run --no-project --with-requirements services/order/requirements/development.txt \
		python -m pytest services/order/tests
	@printf "$(GREEN)✅ Tests passed.$(NC)\n"

# =============================================================================
# 1. Global Multi-Service Commands (run against both services)
# =============================================================================

## build         - Build Docker images for all services (root-level docker compose).
build: build-warehouse-service build-order-service

## build-fast    - Build all service images in parallel. Usage: make -j build-fast
build-fast: build-warehouse-service build-order-service

## update        - Update requirements + security scan in every service directory.
update:
	@printf "$(YELLOW)🔄 Updating all services...$(NC)\n"
	@$(foreach svc,$(SERVICES), \
		printf "\n$(YELLOW)→ Updating $(svc) (in $(SERVICE_DIR_$(svc)))...$(NC)\n"; \
		$(MAKE) -C $(SERVICE_DIR_$(svc)) update; \
	)
	@printf "\n$(GREEN)✅ All services updated successfully.$(NC)\n"

## audit         - Audit Python dependencies in every service directory.
audit:
	@printf "$(YELLOW)🔒 Auditing all services...$(NC)\n"
	@$(foreach svc,$(SERVICES), \
		printf "\n$(YELLOW)→ Auditing $(svc) (in $(SERVICE_DIR_$(svc)))...$(NC)\n"; \
		$(MAKE) -C $(SERVICE_DIR_$(svc)) audit; \
	)
	@printf "\n$(GREEN)✅ All service audits completed.$(NC)\n"

# =============================================================================
# 2. Orchestration Commands (root-level, talk to Docker Compose)
# =============================================================================

## up            - Start the whole stack in the background.
up:
	@printf "$(YELLOW)🚀 Starting the stack...$(NC)\n"
	@$(DOCKER_COMPOSE) up -d
	@printf "$(GREEN)✅ Up. Dashboard: http://localhost:$${DASHBOARD_PORT:-5173}\n"
	@printf "   Orders API: http://localhost:$${ORDER_SERVICE_PORT:-8000}/docs$(NC)\n"

## down          - Stop the stack and remove containers (volumes are kept).
down:
	@printf "$(YELLOW)🛑 Stopping the stack...$(NC)\n"
	@$(DOCKER_COMPOSE) down

## restart       - Recreate every container without rebuilding images.
restart:
	@printf "$(YELLOW)🔄 Recreating containers...$(NC)\n"
	@$(DOCKER_COMPOSE) up -d --force-recreate

## fresh         - Destroy volumes, rebuild images and start from an empty database.
fresh:
	@printf "$(RED)🧨 Removing containers and volumes (all local demo data)...$(NC)\n"
	@$(DOCKER_COMPOSE) down -v
	@$(MAKE) build
	@$(DOCKER_COMPOSE) up -d
	@printf "$(GREEN)✅ Clean stack is up.$(NC)\n"

## ps            - List running containers (all services).
ps:
	$(DOCKER_COMPOSE) ps

## logs          - Stream logs for a service. Usage: make logs [service=order-service]
logs:
	@TARGET=$(service); \
	$(DOCKER_COMPOSE) logs -f $$TARGET

## shell         - Open a shell in a running container. Usage: make shell [service=order-service]
shell:
	@TARGET=$(service); \
	$(DOCKER_COMPOSE) exec -it $$TARGET /bin/bash 2>/dev/null || \
	$(DOCKER_COMPOSE) exec -it $$TARGET /bin/sh

## migrate       - Apply pending Alembic migrations in both services.
migrate:
	@printf "$(YELLOW)🔄 Applying migrations...$(NC)\n"
	@$(DOCKER_COMPOSE) exec order-service alembic upgrade head
	@$(DOCKER_COMPOSE) exec warehouse-service alembic upgrade head
	@printf "$(GREEN)✅ Migrations applied.$(NC)\n"

# =============================================================================
# 3. Demo & observability
# =============================================================================

## demo          - Seed products, fire duplicates, simulate an outage and catch-up.
demo:
	@printf "$(YELLOW)🎬 Running the full demo (seed -> duplicates -> outage -> catch-up)...$(NC)\n"
	@$(DEMO_PY) scripts/demo.py

## demo-fast     - Duplicate burst only (no stop/start of the warehouse).
demo-fast:
	@$(DEMO_PY) scripts/demo.py --no-outage

## seed          - Upsert the demo products and exit.
seed:
	@$(DEMO_PY) scripts/demo.py --seed-only

## events        - Print order/stock events from RabbitMQ (Option A consumer).
events:
	@$(EVENTS_PY) scripts/consume_events.py

## dashboard     - Print the dashboard and API URLs.
dashboard:
	@printf "\n$(GREEN)Dashboard :$(NC) http://localhost:$${DASHBOARD_PORT:-5173}\n"
	@printf "$(GREEN)Order API :$(NC) http://localhost:$${ORDER_SERVICE_PORT:-8000}/docs\n"
	@printf "$(GREEN)Warehouse :$(NC) http://localhost:$${WAREHOUSE_SERVICE_PORT:-8001}/docs\n"
	@printf "$(GREEN)RabbitMQ  :$(NC) http://localhost:$${RABBITMQ_MANAGEMENT_PORT:-15672}\n\n"

# =============================================================================
# 4. Per-Service Targets
# =============================================================================

# --- Build a single service via docker compose (root context) ---

## build-warehouse-service  - Build only the warehouse service image.
build-warehouse-service:
	@printf "$(YELLOW)🚀 Building Warehouse Service (ENV=$(ENV))...$(NC)\n"
	$(DOCKER_COMPOSE) build warehouse-service
	@printf "$(GREEN)✅ Warehouse Service Docker image built successfully!$(NC)\n"

## build-order-service      - Build only the order service image.
build-order-service:
	@printf "$(YELLOW)🚀 Building Order Service (ENV=$(ENV))...$(NC)\n"
	$(DOCKER_COMPOSE) build order-service
	@printf "$(GREEN)✅ Order Service Docker image built successfully!$(NC)\n"

# --- Per-service entry point ---
#     `make warehouse-service` / `make order-service` from root delegates to
#     the service's own Makefile, running its `build` target.

$(SERVICES):
	$(MAKE) -C $(SERVICE_DIR_$(@)) build
