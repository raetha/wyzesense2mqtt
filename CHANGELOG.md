# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.0.0/).

## [Unreleased]

### Maintenance

- Migrated MQTT client to `paho-mqtt` v2 (`CallbackAPIVersion.VERSION2`),
  updating `on_connect`/`on_disconnect` callback signatures and pinning
  `requirements.txt` to `paho-mqtt >= 2, < 3` (#79).
- Removed the unguarded `MQTT_CLIENT.reconnect()` call in the main loop that
  could crash the bridge; automatic reconnection is now handled via
  `connect_async`, `reconnect_delay_set`, and `loop_start`.
- Bridge now publishes an "online" status on `on_connect`, including on
  reconnects after a dropped connection.
- Full `flake8` cleanup across `wyzesense2mqtt.py`, `wyzesense.py`, and
  `bridge_tool_cli.py` (clean at `--max-line-length=200`).
- Bumped GitHub Actions workflow dependencies to their latest major versions
  (`actions/checkout`, `actions/setup-python`, the Docker
  setup/login/metadata/build-push actions, and `github/codeql-action`).
- Added an automated release notes workflow that publishes a GitHub Release
  from this changelog whenever a `vX.Y.Z` tag is pushed, and now also builds
  and pushes versioned container images to ghcr.io and Docker Hub as part of
  that same workflow.
- Renamed `build.yml` to `devel_package.yml` and scoped it to publishing
  container images on pushes to `devel` only. It no longer triggers on
  pull requests (to avoid publishing unreviewed images) or on `release:
  published` (image publishing for tagged releases is now handled directly
  by `release.yml`).
- Removed the explicit `codeql-analysis.yml` workflow. CodeQL scanning for
  Python is now provided via GitHub's "default setup" code scanning (enabled
  in repository security settings), which doesn't require a workflow file.
- Renamed `pythonapp.yaml` to `ci.yml` and replaced `flake8` with `ruff` for
  linting (configured via `.ruff.toml`, mirroring the rule set used in
  `ha-dockhand`: pycodestyle, pyflakes, isort, bugbear, pyupgrade, and
  refurb). The CI workflow now also installs `requirements.txt` and
  byte-compiles the sources as a basic sanity check, and runs on pushes/PRs
  to both `master` and `devel`.
- Modernized `wyzesense.py`, `wyzesense2mqtt.py`, and `bridge_tool_cli.py`
  per the new ruff rules: dropped Python 2 `from __future__`/`from builtins
  import ...` compatibility imports, replaced `IOError` with `OSError`,
  removed redundant `object` base classes, replaced `bytes()` calls with
  `b""` literals, replaced `%`-style string formatting with f-strings, and
  sorted/organized imports.
- Ran `ruff format` across `wyzesense.py`, `wyzesense2mqtt.py`, and
  `bridge_tool_cli.py` for consistent style, and added a `ruff format
  --check` step to CI to enforce it going forward.
- Changed the ruff/lint line-length from 200 (an artifact of the old
  flake8 config) to 120, wrapping the handful of long log messages and
  comments that exceeded it.

[Unreleased]: https://github.com/raetha/wyzesense2mqtt/compare/v3.0.2...HEAD
