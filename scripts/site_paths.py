"""Shared locations for the single published site and private build exports."""
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
WEBSITE_DIR = REPO_ROOT / "website" / "deploy"
BUILD_EXPORT_DIR = REPO_ROOT / "website" / "build"
