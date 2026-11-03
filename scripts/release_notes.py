#!/usr/bin/env python3
"""Changelog checks and release notes for WyzeSense2MQTT.

  python3 scripts/release_notes.py check            # CHANGELOG.md has [Unreleased] and the current version
  python3 scripts/release_notes.py plan [--github]  # version, and whether it's tagged yet
  python3 scripts/release_notes.py notes <version>  # release notes: that changelog section + install text
  python3 scripts/release_notes.py notes Unreleased # what's on dev but not released yet

The version is VERSION in hub/config.py (hub and remote release together, tagged
v<version>). CI runs `check` and `plan` on every push and pull request, and
`notes` for the GitHub release a push to main creates (.github/workflows/build.yml).
"""

import argparse
import os
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CHANGELOG = ROOT / "CHANGELOG.md"
CONFIG = ROOT / "hub" / "config.py"
GITHUB = "https://github.com/raetha/wyzesense2mqtt"

# "## [4.0.1] — 2026-07-29" (older sections) or "## [4.0.2] - 2026-11-10"; "## [Unreleased]"
HEADING = re.compile(r"^## \[(?P<name>[^\]]+)\](?:\s+[—-]\s+(?P<date>\d{4}-\d{2}-\d{2}))?\s*$")


def version() -> str:
    match = re.search(r'^VERSION\s*=\s*"([^"]+)"', CONFIG.read_text(), re.MULTILINE)
    if not match:
        sys.exit(f"no VERSION in {CONFIG.relative_to(ROOT)}")
    return match.group(1)


def sections() -> dict[str, tuple[str | None, str]]:
    """Changelog sections by name: (date, body). The link references at the end are not part of any body."""
    found: dict[str, tuple[str | None, str]] = {}
    name = date = None
    body: list[str] = []
    for line in CHANGELOG.read_text().splitlines():
        heading = HEADING.match(line)
        if heading or re.match(r"^\[[^\]]+\]:\s", line):
            if name is not None:
                found[name] = (date, "\n".join(body).strip())
            name, date, body = (heading["name"], heading["date"], []) if heading else (None, None, [])
            continue
        if name is not None:
            body.append(line)
    if name is not None:
        found[name] = (date, "\n".join(body).strip())
    return found


def tag_exists(tag: str) -> bool:
    out = subprocess.run(
        ["git", "ls-remote", "--tags", "origin", f"refs/tags/{tag}"],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    return bool(out.strip())


def check(release: bool) -> None:
    """On dev, VERSION is the expected next version, which has no section until the release
    pull request dates it; `release` (pull requests into and pushes to main) requires that dated
    section, so an undated release can't merge."""
    have = sections()
    current = version()
    problems = []
    if "Unreleased" not in have:
        problems.append("CHANGELOG.md has no '## [Unreleased]' section")
    if current in have and have[current][0] is None:
        problems.append(f"CHANGELOG.md's {current} section has no date ('## [{current}] - YYYY-MM-DD')")
    elif release and current not in have:
        problems.append(
            f"CHANGELOG.md has no dated section for {current} (VERSION in hub/config.py): "
            "date it in the release pull request"
        )
    if problems:
        sys.exit("\n".join(problems))
    status = f" and {current}" if current in have else f"; {current} is the expected next version"
    print(f"changelog OK: [Unreleased]{status}")


def plan(github: bool) -> None:
    current = version()
    # releases up to 4.0.2 are tagged v<version>
    release = not (tag_exists(current) or tag_exists(f"v{current}"))
    print(f"version {current}: " + ("not tagged yet, a push to main releases it" if release else "released"))
    if github and os.environ.get("GITHUB_OUTPUT"):
        with open(os.environ["GITHUB_OUTPUT"], "a") as out:
            out.write(f"version={current}\nrelease={'true' if release else 'false'}\n")


def notes(name: str) -> None:
    have = sections()
    if name not in have:
        sys.exit(f"CHANGELOG.md has no section [{name}]")
    body = have[name][1] or "No changes recorded."
    if name == "Unreleased":
        print(body)
        return
    minor = ".".join(name.split(".")[:2])
    print(f"""{body}

---

**Docker:** `ghcr.io/raetha/ws2m-hub:{name}` and `ghcr.io/raetha/ws2m-remote:{name}` \
(also tagged `{minor}` and `latest`).
**Home Assistant:** update the WS2M Hub app from the [app repository](https://github.com/raetha/home-assistant-apps).
**Without Docker:** the hub and remote packages are attached below; see \
[Linux Systemd]({GITHUB}#linux-systemd) in the README.""")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    chk = sub.add_parser("check")
    chk.add_argument("--release", action="store_true", help="also require a dated section for VERSION")
    p = sub.add_parser("plan")
    p.add_argument("--github", action="store_true", help="also write version/release to $GITHUB_OUTPUT")
    n = sub.add_parser("notes")
    n.add_argument("version", help="X.Y.Z, or Unreleased")
    args = ap.parse_args()
    if args.cmd == "check":
        check(args.release)
    elif args.cmd == "plan":
        plan(args.github)
    else:
        notes(args.version)


if __name__ == "__main__":
    main()
