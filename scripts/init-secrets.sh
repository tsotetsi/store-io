#!/usr/bin/env bash
#
# Generate the local development secrets consumed by docker-compose.yml and
# the service configurations (services/*/app/config.py).
#
# Idempotent: existing files are left untouched unless FORCE is true.
#
# Usage:
#   ./scripts/init-secrets.sh                # create missing secrets only
#   ./scripts/init-secrets.sh --force        # re-generate every secret
#   FORCE=true ./scripts/init-secrets.sh     # same as --force (used by `make`)
#
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
SECRETS_DIR="${SECRETS_DIR:-${ROOT_DIR}/.secrets}"
FORCE="${FORCE:-false}"

usage() {
    cat <<'EOF'
Generate the local development secrets consumed by docker-compose.yml and
the service configurations (services/*/app/config.py).

Idempotent: existing files are left untouched unless FORCE is true.

Usage:
  ./scripts/init-secrets.sh                # create missing secrets only
  ./scripts/init-secrets.sh --force        # re-generate every secret
  FORCE=true ./scripts/init-secrets.sh     # same as --force (used by `make`)
EOF
}

for arg in "$@"; do
    case "${arg}" in
        --force) FORCE=true ;;
        -h | --help)
            usage
            exit 0
            ;;
        *)
            echo "Unknown argument: ${arg}" >&2
            usage >&2
            exit 1
            ;;
    esac
done

case "${FORCE,,}" in
    true | 1 | yes) FORCE=true ;;
    *) FORCE=false ;;
esac

# Files that docker-compose.yml mounts as Docker secrets, relative to .secrets.
SECRET_FILES=(
    "postgres/order-service/postgres_user"
    "postgres/order-service/postgres_password"
    "postgres/warehouse-service/postgres_user"
    "postgres/warehouse-service/postgres_password"
    "rabbitmq/rabbitmq_user"
    "rabbitmq/rabbitmq_password"
    "redis/redis_password"
)

# 20 URL-safe alphanumeric characters: no characters that would need escaping
# inside a DATABASE_URL / AMQP_URL.
generate_secret() {
    openssl rand -base64 32 | tr -d '=+/ ' | cut -c1-20
}

if ! command -v openssl >/dev/null 2>&1; then
    echo "❌ openssl is required to generate secrets" >&2
    exit 1
fi

# The files are bind-mounted into containers that run as a non-root user
# (uid 10001), and Docker does not remap file ownership for file-based secrets.
# They must therefore be readable inside the container, so use 0644: the
# directory stays 0700 and this file tree is gitignored. Do not reuse these
# values outside local development.
umask 022

if [ ! -d "${SECRETS_DIR}" ]; then
    mkdir -p "${SECRETS_DIR}"
    echo "📁 Created ${SECRETS_DIR}"
fi

created=0
regenerated=0
kept=0

for relative_path in "${SECRET_FILES[@]}"; do
    secret_path="${SECRETS_DIR}/${relative_path}"
    mkdir -p "$(dirname "${secret_path}")"

    if [ -s "${secret_path}" ] && [ "${FORCE}" != "true" ]; then
        chmod 644 "${secret_path}"
        kept=$((kept + 1))
        continue
    fi

    if [ -s "${secret_path}" ]; then
        regenerated=$((regenerated + 1))
        echo "🔄 Regenerated ${relative_path}"
    else
        created=$((created + 1))
        echo "✨ Created ${relative_path}"
    fi

    generate_secret >"${secret_path}.tmp"
    chmod 644 "${secret_path}.tmp"
    mv "${secret_path}.tmp" "${secret_path}"
done

echo ""
echo "🔐 Secrets directory: ${SECRETS_DIR} (created: ${created}, regenerated: ${regenerated}, kept: ${kept})"
if [ "${regenerated}" -gt 0 ]; then
    echo "⚠️  Re-generated credentials do not match existing database/broker state."
    echo "    Run 'docker compose down -v' before starting the services again."
fi
