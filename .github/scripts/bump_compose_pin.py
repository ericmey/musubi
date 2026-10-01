"""Validate both public Core pins before writing either Compose file."""

from __future__ import annotations

import os
import re
from pathlib import Path


def _replace_one(text: str, pattern: str, replacement: str, label: str) -> str:
    updated, count = re.subn(pattern, replacement, text, flags=re.MULTILINE)
    if count != 1:
        raise SystemExit(f"{label}: expected one match, found {count}")
    return updated


def main() -> None:
    public = Path(os.environ["FILE"])
    quickstart = Path(os.environ["QUICKSTART"])
    image = os.environ["NEW_IMAGE"]
    tag = os.environ["TAG"]
    if (
        re.fullmatch(r"ghcr\.io/(?:ericmey|sourceblender)/musubi-core@sha256:[0-9a-f]{64}", image)
        is None
    ):
        raise SystemExit("NEW_IMAGE must be a Musubi Core digest pin")
    if re.fullmatch(r"v[0-9]+\.[0-9]+\.[0-9]+(?:-[0-9A-Za-z.-]+)?", tag) is None:
        raise SystemExit("TAG must be a version tag")

    public_text = public.read_text()
    quickstart_text = quickstart.read_text()
    new_public = _replace_one(
        public_text,
        r"^x-core-image: &core-image ghcr\.io/[a-z0-9-]+/musubi-core@sha256:[0-9a-f]{64}$",
        f"x-core-image: &core-image {image}",
        "public Compose Core pin",
    )
    new_public = _replace_one(
        new_public,
        r"^x-core-version: &core-version v[^\s]+$",
        f"x-core-version: &core-version {tag}",
        "public Compose version",
    )
    new_quickstart = _replace_one(
        quickstart_text,
        r"^(\s*image:\s*)ghcr\.io/[a-z0-9-]+/musubi-core@sha256:[0-9a-f]{64}[ \t]*$",
        rf"\g<1>{image}",
        "quickstart Core pin",
    )
    new_quickstart = _replace_one(
        new_quickstart,
        r"^(\s*#\s*)v[^\s,]+(, cosign-signed by publish-core-image\.yml)",
        rf"\g<1>{tag}\g<2>",
        "quickstart version comment",
    )
    public.write_text(new_public)
    quickstart.write_text(new_quickstart)


if __name__ == "__main__":
    main()
