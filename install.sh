#!/usr/bin/env bash
# Jarvis Engine - Conda environment setup and package installation
# Creates the 'jarvis' conda env and installs dependencies from pyproject.toml

set -e

ENV_NAME="${JARVIS_ENV_NAME:-jarvis}"
PYTHON_VERSION="3.11"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

# --- 1. Check for conda ---
if command -v conda &>/dev/null; then
    CONDA_CMD="conda"
elif command -v mamba &>/dev/null; then
    CONDA_CMD="mamba"
else
    echo "Error: Neither conda nor mamba found. Please install Miniconda or Anaconda first:"
    echo "  https://docs.conda.io/en/latest/miniconda.html"
    exit 1
fi

# --- 2. Platform check (optional warning for non-Apple Silicon) ---
if [[ "$(uname -s)" != "Darwin" ]]; then
    echo "Warning: This stack is optimized for Apple Silicon (M-series). MLX and MPS require macOS."
fi
if [[ "$(uname -m)" != "arm64" ]] && [[ "$(uname -s)" == "Darwin" ]]; then
    echo "Warning: Running on Intel Mac. MLX performance is best on Apple Silicon (arm64)."
fi

# --- 3. Create conda environment ---
if $CONDA_CMD env list | grep -q "^${ENV_NAME}\s"; then
    echo "Conda env '$ENV_NAME' already exists. Skipping create. Use --force to recreate."
    if [[ "${1:-}" == "--force" ]]; then
        $CONDA_CMD env remove -n "$ENV_NAME" -y
        $CONDA_CMD create -n "$ENV_NAME" python="$PYTHON_VERSION" -y
    fi
else
    $CONDA_CMD create -n "$ENV_NAME" python="$PYTHON_VERSION" -y
fi

# --- 4. Activate and install packages ---
echo "Installing packages into env '$ENV_NAME' (includes ortools for Phase 2 scheduler)..."
$CONDA_CMD run -n "$ENV_NAME" pip install --upgrade pip
$CONDA_CMD run -n "$ENV_NAME" pip install --no-user -e .

# --- 5. Post-install smoke test ---
echo "Running smoke test..."
$CONDA_CMD run -n "$ENV_NAME" python -c "
import fastapi
import mlx.core as mx
from ortools.sat.python import cp_model
print('OK: fastapi, mlx, and ortools imports succeeded.')
" || {
    echo "Warning: Smoke test failed. Check dependencies manually."
}

echo ""
echo "Done. Activate the environment with:"
echo "  conda activate $ENV_NAME"
echo ""
echo "Run the API with:"
echo "  uvicorn app.main:app --reload"
