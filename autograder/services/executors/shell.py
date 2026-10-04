# Copyright © 2026 Rutgers, the State University of New Jersey. All rights reserved except as defined by the Rutgers Non-Commercial License, included with this software.
"""
Shell session helpers for environment debugging.

Provides a lightweight ShellExecutor and helpers to build shell contexts and
start shell containers that expose an attached socket.
"""

from __future__ import annotations

from typing import Any, Dict, Optional, Tuple, cast

from django.utils import timezone

from autograder.services.executors import get_executor_class
from autograder.services.executors.base import ExecutionResult, Executor, FileLike
from core.models import Environment


class _ShellFile:
    """Minimal file-like object for building shell session context."""

    def __init__(self, assignment):
        self.id = -1
        self.name = "shell"
        self.extension = ".sh"
        self.data = ""
        self.path = None
        self._assignment = assignment

    def get_file_info(self):
        return None, self._assignment, self._assignment.course


class ShellExecutor(Executor):
    """Lightweight executor for shell sessions (no code execution)."""

    LANGUAGE = "shell"
    EXECUTABLE_EXTENSIONS = []
    TEMPLATE = ""
    DOCKER_IMAGE = "bash:latest"

    def _detect_imports(self, code: str):
        return []

    def execute(self) -> ExecutionResult:
        return ExecutionResult.error("ShellExecutor does not execute code")

    @classmethod
    def is_executable(cls, file_name=None, extension=None, code=None) -> bool:
        return False

    @classmethod
    def open_shell_session(
        cls,
        env: Environment,
        include_datasets: bool,
        include_assignment_files: bool,
        timeout_seconds: int,
        labels: Optional[Dict[str, str]] = None,
        tmpfs_size: str = "size=512m,mode=1777",
        run_pre_script: bool = False,
        dataset_ids: Optional[list[int]] = None,
    ) -> Tuple[ShellExecutor, Dict[str, Dict[str, str]], Dict[str, str], str, Any, Any]:
        client = Executor._get_docker_client()
        if not client:
            raise RuntimeError("Docker is not available")

        if not Executor._ensure_image(env.image_name):
            raise RuntimeError("Docker image is not available")

        executor, volumes, docker_env, temp_staging_dir = build_shell_context(
            env=env,
            include_datasets=include_datasets,
            include_assignment_files=include_assignment_files,
            dataset_ids=dataset_ids,
        )

        default_labels = {
            "codepost.type": "shell_session",
            "codepost.staging_dir": temp_staging_dir or "",
            "codepost.created_at": timezone.now().isoformat(),
            "codepost.timeout_seconds": str(timeout_seconds),
            "codepost.env_id": str(env.id),
        }
        merged_labels = {**default_labels, **(labels or {})}

        # If run_pre_script is requested and a pre-script exists, execute it before the interactive shell
        if run_pre_script and executor.pre_script:
            shell_command = "sh ./.pre_script.sh; exec bash || sh -i"
        else:
            shell_command = "bash || sh -i"

        container = client.containers.create(
            image=env.image_name,
            command=["sh", "-c", shell_command],
            volumes=volumes,
            working_dir="/work",
            network_disabled=not env.allowNetworkAccess,
            mem_limit=Executor.MAX_MEMORY,
            mem_swappiness=0,
            memswap_limit=Executor.MAX_MEMORY_SWAP,
            cpu_quota=Executor.CPU_QUOTA,
            cpu_period=Executor.CPU_PERIOD,
            pids_limit=Executor.MAX_PIDS,
            security_opt=["no-new-privileges"],
            cap_drop=["ALL"],
            tmpfs={"/tmp": tmpfs_size},
            environment=docker_env,
            tty=True,
            stdin_open=True,
            labels=merged_labels,
        )

        # Inject files before starting so the pre-script exists when the command runs
        executor.add_additional_files(container)
        executor.add_pre_script(container)

        # Attach socket before starting so all output (including pre-script) is captured
        sock = container.attach_socket(params={"stdin": 1, "stdout": 1, "stderr": 1, "stream": 1})
        sock._sock.setblocking(True)  # type: ignore[attr-defined]  # Docker socket internal

        container.start()

        return executor, volumes, docker_env, temp_staging_dir, container, sock


def open_shell_session(
    env: Environment,
    include_datasets: bool,
    include_assignment_files: bool,
    timeout_seconds: int,
    labels: Optional[Dict[str, str]] = None,
    tmpfs_size: str = "size=512m,mode=1777",
    run_pre_script: bool = False,
    dataset_ids: Optional[list[int]] = None,
) -> Tuple[ShellExecutor, Dict[str, Dict[str, str]], Dict[str, str], str, Any, Any]:
    """
    Start a shell session container and return (executor, volumes, docker_env, staging_dir, container, socket).
    """
    return ShellExecutor.open_shell_session(
        env=env,
        include_datasets=include_datasets,
        include_assignment_files=include_assignment_files,
        timeout_seconds=timeout_seconds,
        labels=labels,
        tmpfs_size=tmpfs_size,
        run_pre_script=run_pre_script,
        dataset_ids=dataset_ids,
    )


def select_shell_datasets(assignment, include_datasets: bool, dataset_ids: Optional[list[int]] = None):
    """Which datasets a shell session mounts.

    ``dataset_ids=None`` (no selection sent) mounts the same shared set a normal run gets:
    active, not a per-student variant, not a test resource. Variants all share one mount path,
    so mounting the whole pool would collide; test resources belong to a test category's runs.
    An explicit list mounts exactly those ids — any variant or test resource the instructor
    picks — restricted to this assignment's active datasets (unknown ids are dropped).
    """
    if not include_datasets or assignment is None:
        return []
    qs = assignment.dataSets.filter(is_active=True)
    if dataset_ids is None:
        qs = qs.filter(is_student_variant=False, is_test_resource=False)
    else:
        qs = qs.filter(id__in=dataset_ids)
    return list(qs)


def build_shell_context(
    env: Environment,
    include_datasets: bool,
    include_assignment_files: bool,
    dataset_ids: Optional[list[int]] = None,
) -> Tuple[ShellExecutor, Dict[str, Dict[str, str]], Dict[str, str], str]:
    """
    Build executor context for a shell session.
    Returns (executor, volumes, docker_env, temp_staging_dir).
    """
    assignment = env.assignment
    datasets = select_shell_datasets(assignment, include_datasets, dataset_ids)

    shell_file = _ShellFile(assignment)
    executor = ShellExecutor(cast(FileLike, shell_file), datasets=datasets, image_name=env.image_name)

    # Use language executor to inherit cache volumes
    executor_cls = get_executor_class(env.language)
    if executor_cls and hasattr(executor_cls, "INIT_DOCKER_VOLUME"):
        executor.INIT_DOCKER_VOLUME = executor_cls.INIT_DOCKER_VOLUME.copy()

    # Optionally remove assignment files
    if not include_assignment_files:
        executor.additional_files = {}

    temp_staging_dir = executor._create_staging_directory() if datasets else ""
    volumes = executor._get_volume_mounts(temp_staging_dir if datasets else "")
    docker_env = executor._get_docker_environment()

    # Merge environment vars configured for this environment
    if env.env_vars:
        docker_env.update(env.env_vars)

    return executor, volumes, docker_env, temp_staging_dir
