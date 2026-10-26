#!/usr/bin/env bash
set -Eeuo pipefail

# ANSI color codes
GREEN='\033[0;32m'
RED='\033[0;31m'
NC='\033[0m' # No Color

# Configuration
REQUIREMENTS_DIR="${REQUIREMENTS_DIR:-$(pwd)/requirements}"
AUDIT_PYTHON_VERSION="3.14"

echo "=========================================================================="
echo "  Dependency Compilation(with hashes) & Security Audit"
echo "=========================================================================="

# Step 1: Validate prerequisites
echo "Step 1: Checking prerequisites..."

if ! command -v uv >/dev/null 2>&1; then
    echo "ERROR: uv not found. Install it from https://docs.astral.sh/uv/"
    exit 1
fi

if [[ ! -d "$REQUIREMENTS_DIR" ]]; then
    echo "ERROR: Requirements directory not found: $REQUIREMENTS_DIR"
    exit 1
fi

PYTHON=""

if command -v "python${AUDIT_PYTHON_VERSION}" >/dev/null 2>&1; then
    PYTHON="$(command -v "python${AUDIT_PYTHON_VERSION}")"
else
    echo "ERROR: Python ${AUDIT_PYTHON_VERSION} is required."
    echo "Install it or configure uv to manage this Python version."
    exit 1
fi

echo "Using: $(uv --version)"
echo "Python: $("$PYTHON" --version)"
echo "Requirements: $REQUIREMENTS_DIR"

# Step 2: Compile requirement files
echo
echo "Step 2: Compiling requirement files..."

compile_requirements() {
    local name="$1"
    local options=("${@:2}")

    echo "Compiling ${name}..."
    uv pip compile \
        --python "$PYTHON" \
        --strip-extras \
        "${options[@]}" \
        "$REQUIREMENTS_DIR/${name}.in" \
        -o "$REQUIREMENTS_DIR/${name}.txt"
}

# Build tooling: hashes intentionally omitted.
compile_requirements build

# Runtime and development dependencies: hashes enabled.
compile_requirements base --generate-hashes
compile_requirements development --generate-hashes
compile_requirements production --generate-hashes

# Step 3: Prepare an isolated audit environment
echo
echo "Step 3: Preparing security audit environment..."

AUDIT_VENV="$(mktemp -d)"
trap 'rm -rf "$AUDIT_VENV"' EXIT

uv venv --python "$PYTHON" "$AUDIT_VENV"
AUDIT_PYTHON="$AUDIT_VENV/bin/python"
AUDIT_BIN="$AUDIT_VENV/bin/pip-audit"

# Track audit failures without hiding them.
AUDIT_FAILED=0

audit_requirements() {
    local file="$1"

    echo
    echo "Auditing $(basename "$file")..."

    # Install exactly the dependencies in this requirements file.
    if ! uv pip sync "$file" --python "$AUDIT_PYTHON"; then
        echo "ERROR: Failed to synchronize $file"
        AUDIT_FAILED=1
        return 0
    fi

    # Install the audit tool into the same isolated environment.
    if ! uv pip install pip-audit --python "$AUDIT_PYTHON"; then
        echo "ERROR: Failed to install pip-audit"
        AUDIT_FAILED=1
        return 0
    fi

    audit_exit_code=0
    audit_output="$("$AUDIT_BIN" --strict --format markdown 2>&1)" || audit_exit_code=$?
    echo "$audit_output" | while IFS= read -r line; do
        if [[ "$line" == *"No known vulnerabilities found"* ]]; then
            echo -e "${GREEN}${line}${NC}"
        else
            echo "$line"
        fi
    done

    if [[ $audit_exit_code -ne 0 ]]; then
        echo -e "${RED}WARNING: Audit failed or vulnerabilities were detected in $file${NC}"
        AUDIT_FAILED=1
    fi
}

audit_requirements "$REQUIREMENTS_DIR/base.txt"
audit_requirements "$REQUIREMENTS_DIR/development.txt"
audit_requirements "$REQUIREMENTS_DIR/production.txt"

# Step 4: Report the final outcome.
echo
echo "=========================================================================="

if (( AUDIT_FAILED )); then
    echo -e "${RED}┌──────────────────────────────────────────────────────────┐${NC}"
    echo -e "${RED}│  COMPLETED WITH ERRORS: Review the audit output above.   │${NC}"
    echo -e "${RED}│  Do not treat these requirements as security-cleared.    │${NC}"
    echo -e "${RED}└──────────────────────────────────────────────────────────┘${NC}"
    exit 1
fi

echo -e "${GREEN}┌─────────────────────────────────────────────────────────────┐${NC}"
echo -e "${GREEN}│  SUCCESS: All requirement files compiled and audits passed. │${NC}"
echo -e "${GREEN}│  Review the generated files before committing them.         │${NC}"
echo -e "${GREEN}└─────────────────────────────────────────────────────────────┘${NC}"
echo "=========================================================================="
