#!/usr/bin/env bash
set -euo pipefail

python3 -m pip install -e .[dev]
echo "Installed occtl in your Python environment."
echo "To run from this project: ./oc status"
