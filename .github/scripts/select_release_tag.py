"""Select a stable or numbered prerelease tag for a branch push."""

from argparse import ArgumentParser
import json
import re
from subprocess import check_output

from build_release import DOMAIN, ROOT


def select_tag(version: str, branch: str, tags: set[str], head_tags: set[str]) -> str:
    """Keep existing tags and reuse the selected tag when a run is retried."""
    if branch == "main":
        return "" if version in tags - head_tags else version

    pattern = re.compile(rf"{re.escape(version)}-dev\.([1-9][0-9]*)")
    previews = {int(match[1]): tag for tag in tags if (match := pattern.fullmatch(tag))}
    existing = max(
        (number for number, tag in previews.items() if tag in head_tags), default=0
    )
    return (
        previews[existing]
        if existing
        else f"{version}-dev.{max(previews, default=0) + 1}"
    )


if __name__ == "__main__":
    parser = ArgumentParser(description=__doc__)
    parser.add_argument("branch", help="Name of the branch that received a push")
    options = parser.parse_args()
    path = ROOT / "custom_components" / DOMAIN / "manifest.json"
    version = json.loads(path.read_text(encoding="utf-8"))["version"]
    tags = set(check_output(["git", "tag", "--list"], cwd=ROOT, text=True).splitlines())
    head_tags = set(
        check_output(
            ["git", "tag", "--points-at", "HEAD"], cwd=ROOT, text=True
        ).splitlines()
    )
    print(select_tag(version, options.branch, tags, head_tags))
