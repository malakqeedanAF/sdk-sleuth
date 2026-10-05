#!/usr/bin/env bash
# Launcher for sdk_sleuth.py — keeps this file double-clickable from Finder.
set -euo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")"
exec python3 sdk_sleuth.py
