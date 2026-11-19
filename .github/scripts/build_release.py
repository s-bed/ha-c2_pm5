"""Build the Marketplace integration ZIP and its GitHub release notes."""

from argparse import ArgumentParser
import json
from pathlib import Path
import re
from zipfile import ZIP_DEFLATED, ZipFile

ROOT = Path(__file__).resolve().parents[2]
DOMAIN = "c2_pm5"
ARCHIVE_NAME = f"{DOMAIN}.zip"


def release_notes(version: str) -> str:
    """Read only the changelog section for this release."""
    changelog = (ROOT / "CHANGELOG.md").read_text(encoding="utf-8")
    for section in re.split(r"(?m)^## ", changelog)[1:]:
        heading, _, body = section.partition("\n")
        if heading.strip() == version and body.strip():
            return body.strip() + "\n"
    raise SystemExit(f"CHANGELOG.md needs a non-empty '## {version}' section.")


def build(output: Path, tag: str | None) -> None:
    """Package files at the root of the integration directory."""
    integration = ROOT / "custom_components" / DOMAIN
    manifest = json.loads((integration / "manifest.json").read_text(encoding="utf-8"))
    metadata = json.loads((ROOT / "hacs.json").read_text(encoding="utf-8"))
    version = manifest["version"]
    if manifest["domain"] != DOMAIN or not re.fullmatch(r"\d+\.\d+\.\d+", version):
        raise SystemExit("The manifest must have the PM5 domain and a stable x.y.z version.")
    if tag is not None and tag != version:
        raise SystemExit(f"Release tag {tag!r} must equal manifest version {version!r}.")
    if metadata.get("zip_release") is not True or metadata.get("filename") != ARCHIVE_NAME:
        raise SystemExit(f"hacs.json must enable ZIP releases and name {ARCHIVE_NAME}.")

    notes = release_notes(version)
    output.mkdir(parents=True, exist_ok=True)
    with ZipFile(output / ARCHIVE_NAME, "w", ZIP_DEFLATED) as archive:
        for source in sorted(integration.rglob("*")):
            relative = source.relative_to(integration)
            if (
                source.is_file()
                and "__pycache__" not in relative.parts
                and source.suffix not in {".pyc", ".pyo"}
            ):
                archive.write(source, relative.as_posix())
    (output / "release-notes.md").write_text(notes, encoding="utf-8")
    print(f"Built {ARCHIVE_NAME} and release notes for {version}.")


if __name__ == "__main__":
    parser = ArgumentParser(description=__doc__)
    parser.add_argument("output", type=Path)
    parser.add_argument("--tag", help="Require the Git tag to match the manifest version")
    options = parser.parse_args()
    build(options.output, options.tag)
