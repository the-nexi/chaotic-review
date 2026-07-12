"""Configuration and value objects shared by chaotic-review components."""

from __future__ import annotations

import configparser
from dataclasses import dataclass
from pathlib import Path


DEFAULT_CONFIG = Path("/etc/chaotic-review.conf")
DEFAULT_STATE = Path("/var/lib/chaotic-review")
DEFAULT_PROJECT = "54867625"
DEFAULT_API = "https://gitlab.com/api/v4"
REPO = "chaotic-aur"
VERSION = "0.2.0"


class ReviewError(RuntimeError):
    """A safe, user-facing review failure."""


@dataclass(frozen=True)
class Config:
    review_user: str = "auto"
    state_dir: Path = DEFAULT_STATE
    gitlab_project: str = DEFAULT_PROJECT
    gitlab_api: str = DEFAULT_API
    pacman_config: Path = Path("/etc/pacman.conf")


@dataclass(frozen=True)
class SyncPackage:
    repo: str
    base: str
    name: str
    version: str
    filename: str
    sha256: str


def load_config(path: Path = DEFAULT_CONFIG) -> Config:
    parser = configparser.ConfigParser()
    if path.exists():
        parser.read(path)
    section = parser["chaotic-review"] if parser.has_section("chaotic-review") else {}
    return Config(
        review_user=section.get("review_user", "auto"),
        state_dir=Path(section.get("state_dir", str(DEFAULT_STATE))),
        gitlab_project=section.get("gitlab_project", DEFAULT_PROJECT),
        gitlab_api=section.get("gitlab_api", DEFAULT_API).rstrip("/"),
        pacman_config=Path(section.get("pacman_config", "/etc/pacman.conf")),
    )
