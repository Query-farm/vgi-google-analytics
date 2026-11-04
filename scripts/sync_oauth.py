"""Copy or check the shared Google OAuth implementation in another worker checkout."""

from __future__ import annotations

import argparse
import sys
import tomllib
from pathlib import Path

SOURCE = Path(__file__).resolve().parent.parent
PACKAGE = "vgi_google_analytics"
MODULES = ("oauth.py", "oauth_model.py", "oauth_callback.py", "oauth_page.py")
PACKAGES = {
    "vgi-google-analytics": PACKAGE,
    "vgi-google-search-console": "vgi_google_search_console",
}


def main() -> int:
    """Synchronize explicit shared files, leaving connector configuration untouched."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--target", required=True, type=Path, help="Worker repository checkout")
    parser.add_argument("--check", action="store_true", help="Report drift without changing files")
    args = parser.parse_args()
    target = args.target.resolve()
    with (target / "pyproject.toml").open("rb") as stream:
        name = tomllib.load(stream)["project"]["name"]
    if name not in PACKAGES:
        parser.error(f"Unsupported worker: {name}")
    package = PACKAGES[name]
    assets = SOURCE / PACKAGE / "oauth_assets"
    shared = [Path(module) for module in MODULES]
    shared.extend(path.relative_to(assets.parent) for path in sorted(assets.rglob("*")) if path.is_file())
    pairs = [(SOURCE / PACKAGE / path, target / package / path) for path in shared]
    pairs.append((SOURCE / "tests/test_oauth_policy.py", target / "tests/test_oauth_policy.py"))
    drift = []
    for source, destination in pairs:
        content = source.read_bytes()
        if destination.is_file() and destination.read_bytes() == content:
            continue
        drift.append(str(destination.relative_to(target)))
        if not args.check:
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_bytes(content)
    # Removed assets must not silently survive in a bundled copy.
    expected_assets = {path.relative_to("oauth_assets") for path in shared if path.parts[0] == "oauth_assets"}
    extra = [
        path
        for path in sorted((target / package / "oauth_assets").rglob("*"))
        if path.is_file() and path.relative_to(target / package / "oauth_assets") not in expected_assets
    ]
    for path in extra:
        print(f"Remove obsolete shared asset: {path.relative_to(target)}", file=sys.stderr)
    if drift:
        action = "OAuth drift" if args.check else "Updated"
        for path in drift:
            print(f"{action}: {path}")
    elif not extra:
        print("Shared OAuth implementation matches the canonical source.")
    return int(bool(extra or (args.check and drift)))


if __name__ == "__main__":
    raise SystemExit(main())
