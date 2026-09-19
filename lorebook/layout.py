"""Where lorebooks live on disk: the root, the manifests under it, and the artifacts.

The root is resolved in this order, first hit wins:

1. ``--root`` on the command line (``resolve_root(override)``)
2. the ``LOREBOOK_ROOT`` environment variable
3. ``LOREBOOK_ROOT`` in ``.env`` or ``.env.local`` in the working directory

No path is hardcoded here. With none of the three set, :func:`resolve_root` raises
:class:`RootNotConfigured` rather than falling back to a directory.

Two directory shapes are supported under the root, so one root can hold either::

    <root>/<name>/lorebook.yaml           # standalone lorebook
    <root>/<name>/lorebook/lorebook.yaml  # a story directory in novel-data

In both cases the directory holding ``lorebook.yaml`` is the lorebook directory:
``data/``, ``build/`` and ``export/`` sit beside the manifest, never above it.
"""

from __future__ import annotations

from pathlib import Path

from pydantic import field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

#: Manifest filename; its presence is what makes a directory a lorebook.
MANIFEST = "lorebook.yaml"

#: Subdirectory checked for a manifest when the story directory has none itself.
NESTED = "lorebook"

# Per-lorebook artifact locations, relative to the lorebook directory.
GRAPH_REL = Path("build/lorebook.ttl")
VIEW_REL = Path("build/lorebook-view.html")
EXPORT_REL = Path("export")


class RootNotConfigured(RuntimeError):
    """Raised when no root was given by flag, environment or ``.env``."""


class Settings(BaseSettings):
    """Settings read from the environment and from ``.env``.

    ``root`` defaults to ``None`` rather than being a required field. A required
    field would make ``Settings()`` a call with a missing argument as far as any
    type checker is concerned -- pydantic fills it from the environment, but the
    generated signature does not say so. ``None`` means "nothing configured it",
    which :func:`resolve_root` turns into :class:`RootNotConfigured`.
    """

    model_config = SettingsConfigDict(
        env_prefix="LOREBOOK_",
        # Layered, last one winning: `.env` is committed and holds defaults, `.env.local`
        # is not committed and holds per-machine paths. The root is per-machine, so it
        # belongs in `.env.local`.
        env_file=(".env", ".env.local"),
        env_file_encoding="utf-8",
        extra="ignore",
    )

    root: Path | None = None

    @field_validator("root")
    @classmethod
    def _expand(cls, value: Path | None) -> Path | None:
        """Expand ``~``, which neither pydantic nor the shell does inside a ``.env``."""
        return None if value is None else Path(value).expanduser()


def resolve_root(override: Path | None = None) -> Path:
    """The root holding the lorebooks, from ``override`` or the environment.

    Raises :class:`RootNotConfigured` when nothing supplies one.
    """
    if override is not None:
        return Path(override).expanduser()
    root = Settings().root
    if root is None:
        raise RootNotConfigured(
            "No lorebook root configured. Pass --root, set LOREBOOK_ROOT, "
            "or put LOREBOOK_ROOT=<path> in .env.local."
        )
    return root


def manifest_dir(entry: Path) -> Path | None:
    """The lorebook directory inside ``entry``, or ``None`` if there is no manifest.

    Accepts both shapes: the manifest directly in ``entry``, or in ``entry/lorebook/``.
    """
    for candidate in (entry, entry / NESTED):
        if (candidate / MANIFEST).is_file():
            return candidate
    return None


def discover(root: Path) -> dict[str, Path]:
    """Map name -> lorebook directory for every lorebook under ``root``, sorted by name.

    The name is the directory under the root, so a story keeps its slug whether its
    manifest sits in the story directory or one level down in ``lorebook/``.
    """
    if not root.is_dir():
        return {}
    found: dict[str, Path] = {}
    for entry in sorted(root.iterdir()):
        if not entry.is_dir():
            continue
        directory = manifest_dir(entry)
        if directory is not None:
            found[entry.name] = directory
    return found
