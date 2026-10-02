# Copyright © 2026 Rutgers, the State University of New Jersey. All rights reserved except as defined by the Rutgers Non-Commercial License, included with this software.
import asyncio
import json
import logging
import os
import socket
import threading
import time
from dataclasses import dataclass
from datetime import timedelta
from typing import Any, Dict

import redis
import redis.asyncio as redis_async
from channels.db import database_sync_to_async
from django.utils import timezone

from autograder.services.shell_session_utils import (
    cleanup_staging_dir as _cleanup_staging_dir,
    format_mounts as _format_mounts,
    normalize_timeout as _normalize_timeout,
)

logger = logging.getLogger(__name__)

METRICS_ACTIVE_KEY = "shell:metrics:active"
METRICS_IN_KEY = "shell:metrics:in"
METRICS_OUT_KEY = "shell:metrics:out"
METRICS_SESSIONS_KEY = "shell:metrics:sessions"
METRICS_LAST_ACTIVITY_PREFIX = "shell:metrics:last_activity:"


@dataclass
class ShellSession:
    session_id: str
    env_id: int
    user_id: int
    container: Any
    socket: Any
    staging_dir: str
    expires_at: float
    read_thread: threading.Thread


def _setup_django():
    os.environ.setdefault("DJANGO_SETTINGS_MODULE", "codepost.settings")
    # Initialize Django for ORM access in the relay process.
    import django

    django.setup()


def _get_redis_url() -> str:
    return (
        os.environ.get("WORKER_SHELL_REDIS_URL")
        or os.environ.get("CELERY_BROKER_URL", "")
    )
    # Resolve Redis URL from environment.


def _get_worker_id() -> str:
    return os.environ.get("WORKER_SHELL_WORKER_ID") or socket.gethostname()
    # Use provided worker id or hostname.


def _out_channel(session_id: str) -> str:
    return f"shell:out:{session_id}"
    # Output channel for a shell session.


def _publish(redis_client: redis.Redis, channel: str, payload: dict) -> None:
    redis_client.publish(channel, json.dumps(payload))
    # Publish JSON on sync Redis client (thread context).


async def _apublish(redis_client: redis_async.Redis, channel: str, payload: dict) -> None:
    await redis_client.publish(channel, json.dumps(payload))
    # Publish JSON on async Redis client (async context).


def _update_last_activity(redis_client: redis.Redis, session_id: str) -> None:
    redis_client.set(f"{METRICS_LAST_ACTIVITY_PREFIX}{session_id}", str(time.time()))
    # Update last activity timestamp (sync client).


def _start_container(
    env,
    include_datasets: bool,
    include_assignment_files: bool,
    timeout_seconds: int,
    user_id: int | None = None,
    run_pre_script: bool = False,
    dataset_ids: list[int] | None = None,
):
    from autograder.services.executors.base import Executor

    labels = {"codepost.user_id": str(user_id or "")}
    return Executor.open_shell_session(
        env=env,
        include_datasets=include_datasets,
        include_assignment_files=include_assignment_files,
        timeout_seconds=timeout_seconds,
        labels=labels,
        run_pre_script=run_pre_script,
        dataset_ids=dataset_ids,
    )


def _start_session_sync(
    env_id: int,
    include_datasets: bool,
    include_assignment_files: bool,
    timeout_seconds: int,
    user_id: int | None = None,
    run_pre_script: bool = False,
    dataset_ids: list[int] | None = None,
):
    from core.models import Environment

    env = Environment.objects.get(pk=env_id)
    _, volumes, _, staging_dir, container, sock = _start_container(
        env,
        include_datasets,
        include_assignment_files,
        timeout_seconds,
        user_id=user_id,
        run_pre_script=run_pre_script,
        dataset_ids=dataset_ids,
    )
    # Sync wrapper to fetch environment and start container.
    return env.image_name, volumes, staging_dir, container, sock


class WorkerShellRelay:
    MAX_SESSION_SECONDS = 300

    def __init__(self, redis_url: str, worker_id: str):
        self.redis_url = redis_url
        self.worker_id = worker_id
        self.redis_async = redis_async.from_url(redis_url, decode_responses=True)
        self.redis_sync = redis.Redis.from_url(redis_url, decode_responses=True)
        self.sessions: Dict[str, ShellSession] = {}

    async def register_loop(self):
        while True:
            try:
                await self.redis_async.sadd("shell:workers", self.worker_id)  # type: ignore[misc]  # redis async stubs
                await self.redis_async.hset(  # type: ignore[misc]  # redis async stubs
                    f"shell:worker:{self.worker_id}",
                    mapping={"last_seen": str(time.time())},
                )
            except Exception as e:
                logger.error(f"Worker registry update failed: {e}")
            await asyncio.sleep(5)

    async def run(self):
        """
        Start the worker shell relay. This will subscribe to Redis channels for incoming shell session messages, and handle them accordingly. It also starts background tasks for worker registration and session cleanup.
        """
        pubsub = self.redis_async.pubsub()
        await pubsub.psubscribe(f"shell:in:{self.worker_id}:*")
        asyncio.create_task(self.register_loop())
        asyncio.create_task(self.cleanup_loop())

        async for message in pubsub.listen():
            if message.get("type") != "pmessage":
                continue

            channel = message.get("channel")
            data = message.get("data")
            try:
                payload = json.loads(data)
            except Exception:
                continue

            session_id = channel.split(":")[-1]
            await self._handle_message(session_id, payload)

    async def _handle_message(self, session_id: str, payload: dict):
        msg_type = payload.get("type")
        if msg_type == "start":
            await self._handle_start(session_id, payload)
        elif msg_type == "input":
            await self._handle_input(session_id, payload)
        elif msg_type == "close":
            await self._handle_close(session_id)

    async def _handle_start(self, session_id: str, payload: dict):
        if session_id in self.sessions:
            return

        env_id = int(payload.get("envId") or 0)
        user_id = int(payload.get("userId") or 0)
        if not env_id or not user_id:
            await _apublish(
                self.redis_async,
                _out_channel(session_id),
                {"type": "error", "message": "Missing envId/userId"},
            )
            return
        include_datasets = bool(payload.get("includeDatasets", True))
        include_assignment_files = bool(payload.get("includeAssignmentFiles", True))
        run_pre_script = bool(payload.get("runPreScript", False))
        raw_ids = payload.get("datasetIds")
        dataset_ids = [int(i) for i in raw_ids if str(i).isdigit()] if isinstance(raw_ids, list) else None
        timeout_seconds = _normalize_timeout(payload.get("timeoutSeconds"))
        if timeout_seconds > self.MAX_SESSION_SECONDS:
            timeout_seconds = self.MAX_SESSION_SECONDS

        try:
            image_name, volumes, staging_dir, container, sock = await database_sync_to_async(_start_session_sync)(
                env_id,
                include_datasets,
                include_assignment_files,
                timeout_seconds,
                user_id,
                run_pre_script,
                dataset_ids,
            )
        except Exception as e:
            logger.exception("Worker shell start failed")
            await _apublish(self.redis_async, _out_channel(session_id), {"type": "error", "message": str(e)})
            return

        expires_at = time.time() + timeout_seconds
        read_thread = threading.Thread(
            target=self._read_loop,
            args=(session_id, sock),
            daemon=True,
        )
        read_thread.start()

        self.sessions[session_id] = ShellSession(
            session_id=session_id,
            env_id=env_id,
            user_id=user_id,
            container=container,
            socket=sock,
            staging_dir=staging_dir,
            expires_at=expires_at,
            read_thread=read_thread,
        )

        try:
            await self.redis_async.sadd(METRICS_ACTIVE_KEY, session_id)  # type: ignore[misc]  # redis async stubs
            await self.redis_async.hset(  # type: ignore[misc]  # redis async stubs
                METRICS_SESSIONS_KEY,
                session_id,
                json.dumps(
                    {
                        "envId": env_id,
                        "userId": user_id,
                        "workerId": self.worker_id,
                        "startedAt": time.time(),
                        "expiresAt": expires_at,
                    }
                ),
            )
            await self.redis_async.set(f"{METRICS_LAST_ACTIVITY_PREFIX}{session_id}", str(time.time()))
        except Exception as e:
            logger.error(f"Shell metrics update failed: {e}")

        await _apublish(
            self.redis_async,
            _out_channel(session_id),
            {
                "type": "ready",
                "sessionId": session_id,
                "containerId": container.id,
                "image": image_name,
                "expiresAt": (timezone.now() + timedelta(seconds=timeout_seconds)).isoformat(),
                "workingDir": "/work",
                "mounts": _format_mounts(volumes),
            },
        )

    async def _handle_input(self, session_id: str, payload: dict):
        session = self.sessions.get(session_id)
        if not session:
            return
        data = payload.get("data", "")
        if not data:
            return
        try:
            session.socket._sock.sendall(data.encode("utf-8"))  # type: ignore[attr-defined]
            await self.redis_async.set(f"{METRICS_LAST_ACTIVITY_PREFIX}{session_id}", str(time.time()))
        except Exception as e:
            await _apublish(self.redis_async, _out_channel(session_id), {"type": "error", "message": str(e)})

    async def _handle_close(self, session_id: str):
        session = self.sessions.pop(session_id, None)
        if not session:
            return
        try:
            session.socket.close()  # type: ignore[call-arg]
        except Exception:
            pass
        try:
            session.container.stop(timeout=2)  # type: ignore[call-arg]
        except Exception:
            pass
        try:
            session.container.remove(force=True)  # type: ignore[call-arg]
        except Exception:
            pass
        if session.staging_dir:
            _cleanup_staging_dir(session.staging_dir)
        try:
            await self.redis_async.srem(METRICS_ACTIVE_KEY, session_id)  # type: ignore[misc]  # redis async stubs
            await self.redis_async.hdel(METRICS_SESSIONS_KEY, session_id)  # type: ignore[misc]  # redis async stubs
            await self.redis_async.delete(f"{METRICS_LAST_ACTIVITY_PREFIX}{session_id}")  # type: ignore[misc]  # redis async stubs
        except Exception as e:
            logger.error(f"Shell metrics cleanup failed: {e}")
        await _apublish(self.redis_async, _out_channel(session_id), {"type": "closed"})

    async def cleanup_loop(self):
        while True:
            try:
                now_ts = time.time()
                expired = [sid for sid, session in self.sessions.items() if session.expires_at <= now_ts]
                for session_id in expired:
                    await self._handle_close(session_id)
            except Exception as e:
                logger.error(f"Worker cleanup loop failed: {e}")
            await asyncio.sleep(5)

    def _read_loop(self, session_id: str, sock):
        try:
            while True:
                chunk = sock._sock.recv(4096)  # type: ignore[attr-defined]
                if not chunk:
                    break
                _publish(
                    self.redis_sync,
                    _out_channel(session_id),
                    {"type": "data", "data": chunk.decode("utf-8", errors="replace")},
                )
                _update_last_activity(self.redis_sync, session_id)
        except Exception as e:
            _publish(self.redis_sync, _out_channel(session_id), {"type": "error", "message": str(e)})
        finally:
            _publish(self.redis_sync, _out_channel(session_id), {"type": "closed"})


def main():
    _setup_django()
    redis_url = _get_redis_url()
    if not redis_url:
        raise RuntimeError("WORKER_SHELL_REDIS_URL or CELERY_BROKER_URL required")

    worker_id = _get_worker_id()
    relay = WorkerShellRelay(redis_url, worker_id)
    asyncio.run(relay.run())


if __name__ == "__main__":
    main()
