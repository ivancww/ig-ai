"""Safe, non-secret identity metadata for one deployed application bundle."""

from __future__ import annotations

import importlib.metadata
import json
import os
from pathlib import Path
from typing import Any

MANIFEST_NAME = "RELEASE-MANIFEST.json"


def _package_version(name: str) -> str | None:
    try:
        return importlib.metadata.version(name)
    except importlib.metadata.PackageNotFoundError:
        return None


def _manifest_candidates() -> list[Path]:
    candidates: list[Path] = []
    configured = os.environ.get("IGAI_BUILD_MANIFEST", "").strip()
    if configured:
        candidates.append(Path(configured))
    candidates.extend(
        (
            Path.cwd() / MANIFEST_NAME,
            Path(__file__).resolve().parents[2] / MANIFEST_NAME,
            Path(__file__).resolve().parent / MANIFEST_NAME,
            Path(__import__("sys").prefix) / "share" / "ig-ai" / MANIFEST_NAME,
        )
    )
    return candidates


def load_build_identity() -> dict[str, Any]:
    """Return only deployment metadata; never expose credentials or tokens."""
    base: dict[str, Any] = {
        "status": "UNVERIFIED",
        "source_sha": None,
        "package_version": _package_version("ig-ai"),
        "lightstreamer_version": _package_version("lightstreamer-client-lib"),
        "web_files": {},
    }
    seen: set[Path] = set()
    for path in _manifest_candidates():
        resolved = path.resolve()
        if resolved in seen or not resolved.is_file():
            continue
        seen.add(resolved)
        try:
            payload = json.loads(resolved.read_text(encoding="utf-8"))
            if not isinstance(payload, dict):
                continue
            web_files = payload.get("web_files")
            if not isinstance(web_files, dict) or not isinstance(payload.get("source_sha"), str):
                continue
            base.update(
                {
                    "status": "VERIFIED",
                    "manifest_version": payload.get("manifest_version"),
                    "source_sha": payload["source_sha"],
                    "package_version": payload.get("package_version"),
                    "lightstreamer_version": payload.get("lightstreamer_version"),
                    "web_files": {
                        str(name): str(digest) for name, digest in web_files.items()
                    },
                    "cache_version": payload.get("cache_version"),
                }
            )
            return base
        except (OSError, TypeError, ValueError, json.JSONDecodeError):
            continue
    return base
