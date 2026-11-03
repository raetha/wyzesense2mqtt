# Notes for Claude sessions

WyzeSense2MQTT (ws2m): a local bridge from WyzeSense USB dongles to Home Assistant over MQTT, run as Docker images (`ws2m-hub`, `ws2m-remote`) or as a Home Assistant app (raetha/home-assistant-apps). The canonical guide is `docs/development.md`; known work for later is in `docs/backlog.md`; the dongle protocol is `docs/protocol.md`. This file is the short version of what a new session needs.

## How we work

- Sessions push to this repository. No zip packages.
- **Branches** (`docs/development.md` → "Branches and releases"): never push to `main`; it gets one squash-merged pull request per release, which the owner merges. Claude runs `dev`: small, settled changes go straight to it, anything else through a `try/<topic>` branch and pull request that Claude squash-merges once CI is green. `dev` must stay releasable; a change that needs the owner's decision waits on its `try/` branch. Each push to `dev` publishes the test images (`dev`, `<version>-dev.<run>`), which the WS2M Hub (devel) app installs.
- Record each change users would notice under `## [Unreleased]` in `CHANGELOG.md`, written for people installing ws2m (no internal file names unless they need them). Fixes to something not yet released fold into its existing line. Released sections are frozen.
- **`dev` holds the expected next version:** with the first change after a release, set `VERSION` (`hub/config.py`) to what that change calls for under semver, and raise it when a later change calls for more; say what and why in a line. Never date the changelog or open the release pull request unless asked: that pull request (`dev` → `main`), which the owner squash-merges, dates the section. CI then tags `v<version>`, publishes the images and GitHub release, updates the app repository and resets `dev` to `main`.
- Propose the approach before implementing anything non-trivial; ask when a decision is ambiguous. Settled design decisions (global sensor registry, multi-dongle remote, health isolation) aren't revisited without the owner asking.
- Update `README.md` and the relevant `docs/*.md` in the same change as the feature.

## Validate

- `bash scripts/run_tests.sh`: ruff check, ruff format check, pytest (makes `.venv/` on first run from `hub/`, `remote/` requirements and `requirements-dev.txt`). Hardware tests need a dongle: `--hardware`.
- `python3 scripts/release_notes.py check` (also run by CI): the changelog has `[Unreleased]` and a dated section for the current version.

## Code

- Comments only say what the code can't: no narration or restating the obvious.
- `cfg` for config dicts, `cfg['key']` access; config keys in `hub/config.py`.
- Hub and remote share `shared/` (flat-copied into the images; added to `sys.path` locally). No `__init__.py` files.

## Hard-won facts

- Home Assistant's Supervisor pulls `<image>:<version>` from the app's `config.yaml`, so every app version must be an image tag that exists (releases `X.Y.Z`, test builds `X.Y.Z-dev.<run>`). Its `image:` can only hold `{arch}`, nothing else in braces.
- `hub/service.sh` maps each app option to its `WS2M_*` variable by the same name (`dongle` → `WS2M_DONGLE`). An option the app doesn't send becomes an empty variable, which overrides the default; an empty `dongle` means no local dongle (the apps' `usb_dongle` did exactly that in 4.0.0–4.0.1).
- `repository_dispatch` runs the receiving repository's default-branch (`main`) workflow.
- Sessions can't read Actions settings, secrets or security alerts, or rename branches, through the proxy: ask the owner.
