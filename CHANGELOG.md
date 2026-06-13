# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.0.0/).

## [4.0.0] — TBD

### Added

- Migrated Home Assistant MQTT discovery to the device-based format (one
  config topic per device under `components`), per the HA 2026.6.2 MQTT
  integration docs. See `docs/HA_MQTT_COMPLIANCE.md`.
- Added `has_entity_name`, `origin`, and `suggested_display_precision` to
  discovery payloads; removed the deprecated `platform: mqtt` key.
- Added a versioned discovery-schema migration system
  (`mqtt_common.DISCOVERY_SCHEMA_VERSION`, `config/migrations.yaml`) that
  automatically clears stale discovery topics from older schema versions on
  upgrade.
- Discovery payloads are now tagged with `origin.name` and `schema_version`,
  and sensor data payloads include `wyzesense2mqtt_version` /
  `discovery_schema_version` as entity attributes.
- New `wyzesense2mqtt_cli.py` maintenance CLI with a `cleanup-discovery`
  command for finding/clearing orphaned discovery topics for sensors no
  longer in `sensors.yaml`.
- New `mqtt_common.py` module shared between the gateway and CLI tools.

### Fixed

- Fixed a bug in the old `clear_topics()` where `entity_types.add(...)` was
  called on a `list` (would have raised `AttributeError` for binary-sensor
  types on sensor removal).

## [3.1.0] — 2026-06-13

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
- Added an explicit `permissions` block to `pull_request_description.yml`
  (`contents: read`, `pull-requests: write`), resolving CodeQL's
  "workflow does not contain permissions" warning. All other workflows
  already scoped `GITHUB_TOKEN` permissions per job.
- `devel_package.yml` now triggers on `workflow_run` of the CI workflow
  (rather than directly on `push: devel`), and only runs its publish jobs
  when that CI run concluded successfully. This prevents a failing
  lint/build on `devel` from overwriting the `:devel` container image with
  broken code; it checks out the exact commit (`head_sha`) that CI tested.


[Unreleased]: https://github.com/raetha/wyzesense2mqtt/compare/v3.0.2...HEAD
