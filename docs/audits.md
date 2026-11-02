# Audits

Reviews of the hub, the remote, the images and CI. Repeat before a release and whenever the owner asks for a full review; record each one below, newest first.

## Checklist for a full review

1. **Open pull requests**, Dependabot's especially (`gh api "repos/raetha/wyzesense2mqtt/pulls?state=open"`; sessions have the REST API, not GraphQL). For each: read the diff; check its CI passed (`gh api repos/raetha/wyzesense2mqtt/commits/<sha>/check-runs`); for an Action, check the pinned commit is the tag it claims (`git ls-remote https://github.com/<owner>/<action> refs/tags/<tag>`); for a Python package or base image, its release notes. Squash-merge good ones into `dev` (`gh api -X PUT repos/raetha/wyzesense2mqtt/pulls/<n>/merge -f merge_method=squash`), never into `main`. Record any held back, and why.
2. **Security alerts.** Sessions can't read code scanning, Dependabot or secret scanning alerts (403): ask the owner for their titles, and run the checks here: CodeQL (the bundle from `github/codeql-action` releases; `codeql database create --build-mode=none` over a copy of the tracked files only, then `database analyze` with `python-security-and-quality` and `actions-security-and-quality`), and `pip-audit -r hub/requirements.txt -r remote/requirements.txt`.
3. **Tests:** `bash scripts/run_tests.sh` (ruff, pytest); the hardware tests with a dongle if one is available; `docker build` of both Dockerfiles.
4. **The protocol and Home Assistant contract:** discovery payloads still match [docs/HA_MQTT_COMPLIANCE.md](HA_MQTT_COMPLIANCE.md); the apps' options still match what `hub/service.sh` reads.
5. **Security of the design:** what's exposed (the WebSocket listener, mDNS), what's validated (remote ids, tokens, replay sizes, MACs in topics), credential files' permissions, nothing secret in logs or in diagnostics people post publicly.
6. **Workflows:** top-level `contents: read`, write permissions only on the jobs that need them, actions pinned to commits, no `${{ }}` inside `run:` scripts, secrets only on pushes.
7. **Leftovers:** finished experiments, compatibility nobody needs, unused code, stale comments and docs, `docs/BACKLOG.md` items that are done.
8. **Record it** below; a changelog line for anything users notice.

## 2026-11-02: move to the shared repository workflow

Not a full review: what was found while moving to `dev`/`main` with releases from the changelog.

- **Home Assistant apps couldn't run 4.x.** The WS2M Hub app asked Supervisor for the image tag `4.0.1`, but releases were only tagged `v4.0.1`; the devel app's `image:` held a `{version}` placeholder Supervisor can't fill, and its versions (`4.0.1-devel.<date>`) were never image tags. Even with an image, `hub/service.sh` read a `dongle` option the apps call `usb_dongle`, so the hub got an empty dongle setting and started with no local dongle. Fixed: release images are also tagged `X.Y.Z` and `X.Y`, test builds `X.Y.Z-dev.<run>`, the apps were corrected (raetha/home-assistant-apps), and `service.sh` reads either option name.
- **CI** replaced by one `build.yml` (checks on every push and pull request, test images from `dev`, releases from `main` when the version isn't tagged, sync-dev); actions pinned to commits; Dependabot now covers pip and the base images, into `dev`. `pytest` left the hub image's requirements for `requirements-dev.txt` (pinned, with ruff and pytest-cov).
- Stale: `.coveragerc` omitted a path from before the hub/remote split; `docs/cli_tools.md` used the pre-4.0 image name.
- Not changed, worth a look: the `docker run … -m cli.dongle_tool` example in `docs/cli_tools.md` runs from `/` without a working directory, which may not find `cli/` in the image; the runtime requirements are still ranges (see `docs/BACKLOG.md`).
