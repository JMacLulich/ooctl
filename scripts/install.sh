#!/usr/bin/env bash
set -euo pipefail

python3 -m pip install -e .[dev]
echo "Installed occtl. Try: oc status"
