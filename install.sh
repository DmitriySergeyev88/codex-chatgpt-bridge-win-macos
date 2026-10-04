#!/bin/sh
set -eu
bridge_dir=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
cd "$bridge_dir"
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements.lock
if [ ! -f config/instance.local.json ]; then
  printf '%s\n' 'Copy config/instance.example.json to config/instance.local.json and add a project profile under config/projects, then rerun.'
  exit 1
fi
.venv/bin/python -m bridge.cli init
.venv/bin/python -m bridge.cli keychain-init
.venv/bin/python -m bridge.cli launchd-generate
printf '%s\n' 'Installed locally. See README.md for LaunchAgent and ChatGPT connection setup.'
