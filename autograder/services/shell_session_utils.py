# Copyright © 2026 Rutgers, the State University of New Jersey. All rights reserved except as defined by the Rutgers Non-Commercial License, included with this software.
"""Shared helpers for shell session handling."""

from __future__ import annotations

import logging
import os
import shutil
from typing import Dict, Optional

logger = logging.getLogger(__name__)

SHELL_TIMEOUT_SECONDS = 300


def normalize_timeout(timeout_seconds: Optional[int]) -> int:
    if timeout_seconds is None:
        return SHELL_TIMEOUT_SECONDS
    try:
        timeout_int = int(timeout_seconds)
    except Exception:
        return SHELL_TIMEOUT_SECONDS
    return max(60, min(timeout_int, SHELL_TIMEOUT_SECONDS))


def format_mounts(volumes: Dict[str, Dict[str, str]]) -> list[dict]:
    """Format volume mounts with type metadata for frontend display."""
    mounts = []
    for host_path, mount in volumes.items():
        container_path = mount.get("bind", "")
        mount_type = "other"
        # Datasets land under /shared/ by default, or in /work/ via a `./` / `~/` mount path.
        if "/shared/" in container_path or "/dataset" in container_path.lower() or container_path.startswith("/work/"):
            mount_type = "dataset"
        mounts.append(
            {
                "host": host_path,
                "container": container_path,
                "mode": mount.get("mode", "ro"),
                "type": mount_type,
            }
        )
    return mounts


def _is_allowed_staging_path(path: str) -> bool:
    if not path:
        return False
    path = os.path.abspath(path)
    worker_root = os.environ.get("WORKER_STAGING_ROOT")
    if worker_root and path.startswith(os.path.abspath(worker_root)):
        return True
    if path.startswith("/tmp"):
        return True
    if path.startswith("/var/tmp"):
        return True
    return False


def cleanup_staging_dir(staging_dir: str) -> None:
    if not staging_dir:
        return
    if not _is_allowed_staging_path(staging_dir):
        logger.warning(
            "Skipping cleanup of staging dir outside allowed roots: %s",
            staging_dir,
        )
        return
    try:
        shutil.rmtree(staging_dir, ignore_errors=True)
    except Exception as e:
        logger.warning("Failed to clean staging dir %s: %s", staging_dir, e)
