# Development

How WyzeSense2MQTT is built, tested and released. User documentation is the [README](../README.md).

## Layout

| Path | What it is |
| --- | --- |
| `hub/` | The hub: dongles, sensor registry, MQTT and Home Assistant discovery, the WebSocket listener for remotes. `hub/service.sh` is the image's entry point (also reads Home Assistant app options). |
| `hub/cli/` | `dongle_tool`, `mqtt_tool` ([docs/cli-tools.md](cli-tools.md)). |
| `remote/` | The remote relay: forwards a dongle's raw frames to a hub over WebSocket. |
| `shared/` | Code both use (`dongle_protocol.py`, `device_discovery.py`), flat-copied into each image. |
| `docker/` | `Dockerfile.hub`, `Dockerfile.remote`. |
| `examples/` | Compose files, `.env` examples, systemd units, the udev rule. |
| `tests/` | pytest suite; hardware tests are marked `dongle`. |
| `tools/` | Capture and fuzzing helpers for protocol work. |
| `scripts/` | `run_tests.sh`, `release_notes.py`. |
| `CHANGELOG.md` | One changelog for hub and remote, which release together. |

## Setup and everyday loop

```bash
git clone -b dev https://github.com/raetha/wyzesense2mqtt && cd wyzesense2mqtt
bash scripts/run_tests.sh          # makes .venv/ the first time, then ruff + pytest
bash scripts/run_tests.sh --hardware --dongle /dev/hidraw0   # with a dongle attached
```

Dependencies: `hub/requirements.txt` and `remote/requirements.txt` go into the images; `requirements-dev.txt` holds the lint and test tools, pinned. Dependabot proposes updates to all of them, the base images and the Actions.

## Branches and releases

`main` holds releases only: each commit on it is one release, squash-merged from a pull request. Day-to-day work happens on other branches.

| Branch | What goes there | What a push does |
| --- | --- | --- |
| `main` | One squash-merged commit per release. Never pushed to directly. | Releases the version in `hub/config.py` if `v<version>` isn't tagged yet; resets `dev` to `main`. |
| `dev` | Work that's been adopted, ready for the next release, and Dependabot's merges. `## [Unreleased]` in `CHANGELOG.md` is kept up to date here. | Publishes the test images and updates the WS2M Hub (devel) app. |
| `try/<topic>` | Experiments that may be dropped. Branched from `dev`. | Nothing by itself; open a (draft) pull request into `dev` to run CI. |

Day to day:

1. Claude runs `dev`: small, settled changes go straight onto it, and `dev` must stay releasable.
2. Anything exploratory or larger starts on `try/<topic>` with a pull request into `dev`. If it's adopted, Claude squash-merges it once CI is green; a change that needs the owner's decision waits on its branch. If it's dropped, delete the branch and note the idea in `docs/backlog.md`.
3. With the first change after a release, `VERSION` in `hub/config.py` moves to the expected next version (what that change calls for under semver), and a later change that calls for more raises it. Test builds carry it.
4. Each change users would notice adds a line under `## [Unreleased]` in `CHANGELOG.md` ([Keep a Changelog](https://keepachangelog.com/en/1.1.0/)), written for someone installing ws2m.
5. Try the test build: `ghcr.io/raetha/ws2m-hub:dev` (or the WS2M Hub (devel) app) after each push to `dev`.

Releasing (only when the owner asks):

1. On `dev`: rename `## [Unreleased]` to `## [X.Y.Z] - YYYY-MM-DD` with a fresh empty `## [Unreleased]` above it, update the compare links at the bottom (`[Unreleased]: …/compare/vX.Y.Z...dev`, `[X.Y.Z]: …/compare/v<previous>...vX.Y.Z`), and check `VERSION` in `hub/config.py` is still right for what's in the release. Check the notes as they'll appear: `python3 scripts/release_notes.py notes X.Y.Z`.
2. Open a pull request from `dev` into `main` titled "WyzeSense2MQTT X.Y.Z", with the new changelog section as its description.
3. The owner squash-merges it.
4. CI on `main` publishes the images, creates the tag `vX.Y.Z` and the GitHub release (the changelog section plus install notes, with the hub and remote packages attached), bumps the WS2M Hub app, and resets `dev` to `main` if `dev` has no newer work. If it does, reset by hand once that work is released: `git checkout dev && git reset --hard origin/main && git push --force-with-lease`.

Versions follow [Semantic Versioning](https://semver.org/).

Repository settings (owner, one time):

- **Settings → Rules → Rulesets**: one for `main`, the default branch (no deletion, no force pushes, a pull request before merging, squash only). **Require status checks:** `CI passed` (the gate job in `build.yml`). **Require code scanning results:** CodeQL.
- **Settings → General → Pull Requests**: allow only squash merging, and **Automatically delete head branches**.
- **Settings → Branches → classic branch protection rule for `dev`**: nothing required, deletions not allowed (so auto-delete never removes `dev` after a release pull request), **force pushes allowed** (sync-dev resets `dev` with one). A classic rule, not a ruleset: a ruleset's "Restrict deletions" has been reported not to stop auto-delete when an admin can bypass it.
- **Settings → Secrets and variables → Actions**: `HA_APPS_PAT`, a token with contents write access to raetha/home-assistant-apps (to send it the version just published).
- **Code security**: CodeQL default setup.

### What CI does

`.github/workflows/build.yml`:

- **Every push and pull request:** `scripts/release_notes.py check` (fails without `[Unreleased]`; for pull requests into `main` and pushes to `main`, also without a dated section for the current version, so an undated release can't merge), ruff check and format, byte-compile, pytest.
- **`CI passed`:** a gate job that passes only when every check above did; it's the one status check the `main` ruleset requires. Every checkout uses `persist-credentials: false` except sync-dev's, the one job that pushes with git.
- **Push to `dev`:** builds both images for every platform and pushes the tags `dev`, `devel` (the test tag before the branch rename, kept for existing pulls) and `<version>-dev.<run>`; then sends `ws2m-dev` with that version to raetha/home-assistant-apps.
- **Push to `main` with an untagged version:** pushes the images as `X.Y.Z`, `X.Y`, `latest` and `vX.Y.Z` (the tag style of earlier releases), creates the tag and GitHub release, sends `ws2m-release` to the app repository.
- **Every push to `main`:** `sync-dev`.

Secrets are only used on pushes, never on pull requests.

### Test builds

Every push to `dev` publishes `ghcr.io/raetha/ws2m-hub` and `ghcr.io/raetha/ws2m-remote` as `dev` and `<version>-dev.<run>` (the version is the last release until the release pull request sets the next one). In Home Assistant, install **WS2M Hub (devel)** from raetha/home-assistant-apps: each test build shows up as an update to `<version>-dev.<run>`.

## The Home Assistant apps

raetha/home-assistant-apps holds the WS2M Hub app (releases) and WS2M Hub (devel) (test builds); both run the `ws2m-hub` image. Their `config.yaml` version is the image tag Supervisor pulls, set by that repository's sync workflow when this one dispatches. `hub/service.sh` turns the app's options into `WS2M_*` variables.

## Audits

Full reviews follow the checklist in [docs/audits.md](audits.md) and are recorded there.
