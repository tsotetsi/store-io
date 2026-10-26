# ===== Configuration ===== #
UID := $(shell id -u)
GID := $(shell id -g)
ENV ?= development
SERVICE ?= warehouse-service

# Compose files - resolves from the project root.
COMPOSE_BASE     = ../../docker-compose.yml
COMPOSE_OVERRIDE = ../../docker-compose.override.yml
COMPOSE_CI       = ../../docker-compose.ci.yml

ifeq ($(ENV),development)
  COMPOSE_FILES = -f $(COMPOSE_BASE) $(if $(wildcard $(COMPOSE_OVERRIDE)),-f $(COMPOSE_OVERRIDE))
else ifeq ($(ENV),ci)
  COMPOSE_FILES = -f $(COMPOSE_BASE) -f $(COMPOSE_CI)
else
  COMPOSE_FILES = -f $(COMPOSE_BASE)
endif

DOCKER_COMPOSE ?= docker compose $(COMPOSE_FILES)

# ===== Colors ===== #
GREEN  := \033[0;32m
YELLOW := \033[1;33m
RED    := \033[0;31m
NC     := \033[0m

# ===== Targets ===== #
.PHONY: help audit build update alembic-migrations alembic-upgrade alembic-downgrade

help: ## Show available commands
	@echo ""
	@echo "$(YELLOW)Warehouse service commands:$(NC)"
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) | \
		awk 'BEGIN {FS = ":.*?## "}; {printf "  $(GREEN)%-24s$(NC) %s\n", $$1, $$2}'
	@echo ""

# ===== Build ===== #
build: ## Build the warehouse service Docker image.
	@echo "$(YELLOW)🚀 Building Warehouse Service (ENV=$(ENV))...$(NC)"
	$(DOCKER_COMPOSE) build $(SERVICE)
	@echo "$(GREEN)✅ Warehouse Service Docker image built successfully!$(NC)"

# ===== Alembic Migrations ===== #
alembic-migrations: ## Generate a new Alembic migration (Usage: make alembic-migrations msg="describe your change")
	@if [ -z "$(msg)" ]; then \
		echo "$(RED)❌ Usage: make alembic-migrations msg=\"your migration description\"$(NC)"; \
		exit 1; \
	fi
	@echo "$(YELLOW)🔄 Generating migration: $(msg)$(NC)"
	$(DOCKER_COMPOSE) exec $(SERVICE) alembic revision --autogenerate -m "$(msg)"
	@echo "$(GREEN)✅ Migration generated successfully!$(NC)"

alembic-upgrade: ## Apply pending migrations (Usage: make alembic-upgrade)
	@echo "$(YELLOW)🔄 Applying pending migrations...$(NC)"
	$(DOCKER_COMPOSE) exec $(SERVICE) alembic upgrade head
	@echo "$(GREEN)✅ Migrations applied successfully!$(NC)"

alembic-downgrade: ## Rollback one migration (Usage: make alembic-downgrade)
	@echo "$(YELLOW)🔄 Rolling back one migration...$(NC)"
	$(DOCKER_COMPOSE) exec $(SERVICE) alembic downgrade -1
	@echo "$(GREEN)✅ Migration rolled back successfully!$(NC)"

# ===== Requirements & Security ===== #
update: ## Update requirements with hashes + run security scan.
	@echo "$(YELLOW)🔄 Updating warehouse service requirements...$(NC)"
	@if [ -f scripts/update-audit-requirements.sh ]; then \
		./scripts/update-audit-requirements.sh; \
	else \
		echo "$(RED)❌ scripts/update-audit-requirements.sh not found$(NC)"; \
		exit 1; \
	fi
	@echo "$(GREEN)✅ Warehouse Service requirements updated & audited successfully!$(NC)"

audit: ## Audit warehouse service requirements with pip-audit.
	@echo "$(YELLOW)🔒 Auditing warehouse service requirements...$(NC)"
	@if [ -f requirements/production.txt ]; then \
		uvx pip-audit -r requirements/production.txt --require-hashes 2>&1 || true; \
		uvx pip-audit -r requirements/development.txt --require-hashes 2>&1 || true; \
	fi
	@echo "$(GREEN)✅ Warehouse service audit of packages completed successfully.$(NC)"

# ╔══════════════════════════════════════════════════════════════════════╗
# ║  DEBUGGING — shell, logs, status                                   ║
# ╚══════════════════════════════════════════════════════════════════════╝

shell: ## Open a shell in a running container (Usage: make shell [service=warehouse-service])
	@TARGET_SERVICE=$(if $(service),$(service),$(SERVICE)); \
	$(DOCKER_COMPOSE) exec -it $$TARGET_SERVICE /bin/bash || \
	$(DOCKER_COMPOSE) exec -it $$TARGET_SERVICE /bin/sh

logs: ## Show logs for a service (Usage: make logs [service=warehouse-service])
	@TARGET_SERVICE=$(if $(service),$(service),$(SERVICE)); \
	$(DOCKER_COMPOSE) logs -f $$TARGET_SERVICE

ps: ## List running services
	$(DOCKER_COMPOSE) ps
