# Copyright © 2026 Rutgers, the State University of New Jersey. All rights reserved except as defined by the Rutgers Non-Commercial License, included with this software.
"""Dataset mount paths: one shared folder, several spellings.

Every codePost-built image (autograder/testUtils/buildHelpers.py) has the shared dataset
folder at ``/shared`` and reaches it through symlinks from the codepost user's home
(``~/shared`` = ``/home/codepost/shared``), the working directory (``./shared`` =
``/work/shared``) and ``/srv/shared`` (the JupyterHub convention). The *relative* spellings
``~/shared/x``, ``./shared/x`` and ``shared/x`` are therefore one mount, stored as
``shared/x``. An *absolute* path is never rewritten: it binds exactly where typed, so an
instructor can still put a file at ``/srv/shared/x`` (or anywhere else) literally — which
also keeps working in images built before the ``/srv/shared`` link existed.
``container_mount_path`` turns a stored path into the absolute bind target.
"""
from __future__ import annotations

import os

SHARED_ROOT = '/shared'
WORK_DIR = '/work'            # WORKDIR of every generated image; where student code runs
HOME_DIR = '/home/codepost'   # ENV HOME of every generated image

# Relative spellings of the shared folder (absolute paths are left exactly as typed).
_SHARED_PREFIXES = (
    '~/shared/',
    './shared/',
    'shared/',
)


def normalize_mount_path(path: str | None) -> str:
    """Return the stored form of a mount path.

    The relative spellings of the shared folder become ``shared/<rest>``; a bare ``.``/``~``
    becomes ``./``/``~/`` (a directory); anything else — including every absolute path — is
    kept as typed, stripped.
    """
    p = (path or '').strip()
    if p in ('.', '~'):
        return p + '/'
    for prefix in _SHARED_PREFIXES:
        if p.startswith(prefix):
            return 'shared/' + p[len(prefix):]
        if p == prefix[:-1]:
            return 'shared/'
    return p


def container_mount_path(mount_path: str | None, name: str) -> str:
    """Absolute path a dataset binds to inside the container.

    ``shared/x`` (or ``~/shared/x``, ``./shared/x``) → ``/shared/x``; ``./x`` → ``/work/x``;
    ``~/x`` → ``/home/codepost/x``; an absolute path is used exactly as typed; any other
    relative path goes under ``/shared``. A trailing slash means "a folder" and ``name`` is
    appended.
    """
    p = normalize_mount_path(mount_path) or f'shared/{name}'
    if p.endswith('/'):
        p += name
    if p.startswith('./'):
        return os.path.normpath(WORK_DIR + p[1:])
    if p.startswith('~/'):
        return os.path.normpath(HOME_DIR + p[1:])
    if p.startswith('/'):
        return os.path.normpath(p)
    if p.startswith('shared/'):
        p = p[len('shared/'):]
    return os.path.normpath(os.path.join(SHARED_ROOT, p))
