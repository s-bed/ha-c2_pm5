"""Build the Marketplace integration ZIP and its GitHub release notes."""

from argparse import ArgumentParser
import json
from pathlib import Path
import re
from zipfile import ZIP_DEFLATED, ZipFile

ROOT = Path(__file__).resolve().parents[2]
DOMAIN = "c2_pm5"
ARCHIVE_NAME = f"{DOMAIN}.zip"


def release_notes(version: str, base_version: str) -> str:
    """Read version-specific notes and use the target release notes for previews."""
    changelog = (ROOT / "CHANGELOG.md").read_text(encoding="utf-8")
    notes = {}
    for section in re.split(r"(?m)^## ", changelog)[1:]:
        heading, _, body = section.partition("\n")
        if body.strip():
            notes[heading.strip()] = body.strip() + "\n"
    if version in notes:
        return notes[version]
    if version != base_version and base_version in notes:
        return f"Development preview of **{base_version}**.\n\n{notes[base_version]}"
    raise SystemExit(f"CHANGELOG.md needs a non-empty '## {version}' section.")


def build(output: Path, tag: str | None) -> None:
    """Package files at the root of the integration directory."""
    integration = ROOT / "custom_components" / DOMAIN
    manifest = json.loads((integration / "manifest.json").read_text(encoding="utf-8"))
    metadata = json.loads((ROOT / "hacs.json").read_text(encoding="utf-8"))
    base_version = manifest["version"]
    if manifest["domain"] != DOMAIN or not re.fullmatch(r"\d+\.\d+\.\d+", base_version):
        raise SystemExit("The manifest must have the PM5 domain and a stable x.y.z version.")
    version = tag if tag is not None else base_version
    if version != base_version and not re.fullmatch(
        rf"{re.escape(base_version)}-dev\.[1-9][0-9]*", version
    ):
        raise SystemExit(f"Release tag must be {base_version} or {base_version}-dev.N (N >= 1).")
    if metadata.get("zip_release") is not True or metadata.get("filename") != ARCHIVE_NAME:
        raise SystemExit(f"hacs.json must enable ZIP releases and name {ARCHIVE_NAME}.")

    notes = release_notes(version, base_version)
    output.mkdir(parents=True, exist_ok=True)
    with ZipFile(output / ARCHIVE_NAME, "w", ZIP_DEFLATED) as archive:
        for source in sorted(integration.rglob("*")):
            relative = source.relative_to(integration)
            if not source.is_file() or "__pycache__" in relative.parts:
                continue
            if source.suffix in {".pyc", ".pyo"}:
                continue
            if relative.as_posix() == "manifest.json" and version != base_version:
                manifest["version"] = version
                archive.writestr("manifest.json", json.dumps(manifest, indent=2) + "\n")
            else:
                archive.write(source, relative.as_posix())
    (output / "release-notes.md").write_text(notes, encoding="utf-8")
    print(f"Built {ARCHIVE_NAME} and release notes for {version}.")


if __name__ == "__main__":
    parser = ArgumentParser(description=__doc__)
    parser.add_argument("output", type=Path)
    parser.add_argument("--tag", help="Stable or development tag for the manifest version")
    options = parser.parse_args()
    build(options.output, options.tag)
