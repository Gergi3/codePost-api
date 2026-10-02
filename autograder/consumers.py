# Copyright © 2026 Rutgers, the State University of New Jersey. All rights reserved except as defined by the Rutgers Non-Commercial License, included with this software.
import asyncio
import json
import logging
import random
import time
import uuid
from datetime import timedelta
from typing import Optional
from urllib.parse import parse_qs

from asgiref.sync import async_to_sync
from channels.db import database_sync_to_async
from channels.generic.websocket import AsyncWebsocketConsumer
from django.contrib.auth.models import AnonymousUser
from django.utils import timezone
from django.conf import settings
import aiohttp
import redis.asyncio as redis_async
from rest_framework.authentication import TokenAuthentication
from rest_framework_simplejwt.authentication import JWTAuthentication
from rest_framework_simplejwt.tokens import SlidingToken, AccessToken
from rest_framework_simplejwt.exceptions import TokenError
import jwt

from autograder.services.shell_session_utils import (
    cleanup_staging_dir as _cleanup_staging_dir,
    format_mounts as _format_mounts,
    normalize_timeout as _normalize_timeout,
)
from autograder.services.executors.shell import build_shell_context as _build_shell_context
from core.models import Environment
from core.permissions.helpers import isCourseStaff
from autograder.services.executors.base import Executor

logger = logging.getLogger(__name__)

DEFAULT_SHELL_TIMEOUT_SECONDS = 300
METRICS_ACTIVE_KEY = "shell:metrics:active"
METRICS_IN_KEY = "shell:metrics:in"
METRICS_OUT_KEY = "shell:metrics:out"
METRICS_SESSIONS_KEY = "shell:metrics:sessions"
METRICS_LAST_ACTIVITY_PREFIX = "shell:metrics:last_activity:"


class EnvironmentShellConsumer(AsyncWebsocketConsumer):
    """
    WebSocket consumer for interactive shell sessions.

    Client sends raw command bytes; server forwards to container shell.
    Server streams shell output back to client as text frames.
    """

    container = None
    socket = None
    staging_dir: str = ""
    timeout_task: Optional[asyncio.Task] = None
    read_task: Optional[asyncio.Task] = None
    relay_task: Optional[asyncio.Task] = None
    relay_session: Optional[aiohttp.ClientSession] = None
    relay_ws: Optional[aiohttp.ClientWebSocketResponse] = None
    redis_client: Optional[redis_async.Redis] = None
    redis_pubsub: Optional[object] = None
    redis_task: Optional[asyncio.Task] = None
    session_id: Optional[str] = None
    worker_id: Optional[str] = None
    metrics_client: Optional[redis_async.Redis] = None

    async def connect(self):
        # Authenticate, authorize, and create a shell session (local or relay).
        env_id = self.scope.get("url_route", {}).get("kwargs", {}).get("environment_id")
        if not env_id:
            logger.warning("Shell WS denied: missing environment_id")
            await self.close(code=4000)
            return

        query = parse_qs((self.scope.get("query_string") or b"").decode())
        token_param = query.get("token", [None])[0]
        user = await self._authenticate_scope(token_param)
        if not user or isinstance(user, AnonymousUser):
            logger.warning("Shell WS denied: unauthenticated", extra={"env_id": env_id})
            await self.close(code=4001)
            return

        env = await self._get_environment(env_id)
        if not env:
            logger.warning(
                "Shell WS denied: environment not found",
                extra={"env_id": env_id, "user_id": getattr(user, "id", None)},
            )
            await self.close(code=4004)
            return

        if not await self._is_admin(user, env):
            logger.warning(
                "Shell WS denied: not staff",
                extra={"env_id": env_id, "user_id": getattr(user, "id", None), "debug": settings.DEBUG},
            )
            await self.close(code=4003)
            return

        if not env.image_name:
            logger.warning(
                "Shell WS denied: image missing",
                extra={"env_id": env_id, "user_id": getattr(user, "id", None)},
            )
            await self.close(code=4002)
            return

        include_datasets = self._parse_bool(query.get("includeDatasets", ["true"])[0])
        include_assignment_files = self._parse_bool(query.get("includeAssignmentFiles", ["true"])[0])
        run_pre_script = self._parse_bool(query.get("runPreScript", ["false"])[0])
        # Absent → None (mount the shared default); present → exactly these ids.
        dataset_ids = self._parse_id_list(query.get("datasetIds", [None])[0])
        needs_network = env.allowNetworkAccess
        timeout_seconds = _normalize_timeout(
            self._parse_int(query.get("timeoutSeconds", [str(DEFAULT_SHELL_TIMEOUT_SECONDS)])[0])
        )
        self.session_id = query.get("sessionId", [None])[0]
        if not self.session_id:
            self.session_id = uuid.uuid4().hex

        if self._should_relay_redis():
            await self._connect_relay_redis(
                env_id,
                user,
                include_datasets,
                include_assignment_files,
                timeout_seconds,
                needs_network,
                run_pre_script,
                dataset_ids,
            )
            return

        if self._should_relay():
            await self._connect_relay(
                env_id, user, include_datasets, include_assignment_files, timeout_seconds, run_pre_script, dataset_ids,
            )
            return

        labels = {
            "codepost.env_id": str(env.id),
            "codepost.assignment_id": str(env.assignment.id),
            "codepost.user_id": str(getattr(user, "id", "")),
            "codepost.type": "shell_session",
            "codepost.created_at": timezone.now().isoformat(),
            "codepost.timeout_seconds": str(timeout_seconds),
        }

        try:
            tmpfs_size = "size=2g,mode=1777" if needs_network else "size=512m,mode=1777"
            executor, volumes, _docker_env, temp_staging_dir, self.container, self.socket = (
                await self._open_shell_session_sync(
                    env, 
                    include_datasets, 
                    include_assignment_files, 
                    timeout_seconds, 
                    labels, 
                    tmpfs_size,
                    run_pre_script,
                    dataset_ids,
                )
            )
            self.staging_dir = temp_staging_dir
        except Exception as e:
            logger.error(f"Shell WS start failed: {e}")
            await self.close(code=5003)
            await self._cleanup()
            return

        await self.accept()
        await self.send_json({
            "type": "ready",
            "containerId": self.container.id,
            "image": env.image_name,
            "expiresAt": (timezone.now() + timedelta(seconds=timeout_seconds)).isoformat(),
            "workingDir": "/work",
            "mounts": _format_mounts(volumes),
        })

        # Set socket to non-blocking for asyncio
        if self.socket and hasattr(self.socket, "_sock"):
             self.socket._sock.setblocking(False)

        user_id = getattr(user, "id", None)
        if user_id is not None:
            await self._register_metrics_session(env_id, int(user_id), timeout_seconds, worker_id="local")

        self.read_task = asyncio.create_task(self._pump_container_output())
        self.timeout_task = asyncio.create_task(self._auto_close(timeout_seconds))

    async def disconnect(self, code):
        # Cleanup resources on websocket disconnect.
        await self._cleanup()

    async def receive(self, text_data=None, bytes_data=None):
        # Forward client input to the active shell session.
        if self.redis_client is not None and self.worker_id and self.session_id:
            await self._redis_send(text_data=text_data, bytes_data=bytes_data)
            return
        if self.relay_ws is not None:
            await self._relay_send(text_data=text_data, bytes_data=bytes_data)
            return
        if not self.socket:
            return

        data = bytes_data
        if text_data is not None:
            data = text_data.encode("utf-8")

        if not data:
            return

        try:
            loop = asyncio.get_running_loop()
            await loop.sock_sendall(self.socket._sock, data)
            await self._metrics_incr(METRICS_IN_KEY)
            await self._metrics_touch()
        except Exception as e:
            logger.error(f"Shell WS send failed: {e}")
            await self.send_json({"type": "error", "message": "Failed to write to shell"})

    async def _pump_container_output(self):
        # Stream container stdout/stderr back to the websocket.
        if not self.socket:
            return
        
        loop = asyncio.get_running_loop()
        sock = getattr(self.socket, "_sock", None)
        if sock is None:
            return

        try:
            while True:
                chunk = await loop.sock_recv(sock, 4096)
                if not chunk:
                    break
                await self.send(text_data=chunk.decode("utf-8", errors="replace"))
                await self._metrics_incr(METRICS_OUT_KEY)
                await self._metrics_touch()
        except Exception as e:
            logger.error(f"Shell WS read failed: {e}")

    async def _auto_close(self, timeout_seconds: int):
        # Close the session automatically after the timeout.
        await asyncio.sleep(timeout_seconds)
        await self.close(code=4000)

    async def _cleanup(self):
        # Tear down container, sockets, relay connections, and metrics.
        if self.redis_client is not None and self.worker_id and self.session_id:
            try:
                await self._redis_publish(
                    f"shell:in:{self.worker_id}:{self.session_id}",
                    {"type": "close"},
                )
            except Exception:
                pass
        if self.redis_task and not self.redis_task.done():
            self.redis_task.cancel()
        if self.redis_pubsub is not None:
            try:
                close_fn = getattr(self.redis_pubsub, "close", None)
                if close_fn:
                    await close_fn()
            except Exception:
                pass
            self.redis_pubsub = None
        redis_client_ref = self.redis_client
        if self.redis_client is not None:
            try:
                await self.redis_client.aclose()
            except Exception:
                pass
            self.redis_client = None
        if self.metrics_client is not None and self.metrics_client is not redis_client_ref:
            try:
                await self.metrics_client.aclose()
            except Exception:
                pass
            self.metrics_client = None
        if self.relay_task and not self.relay_task.done():
            self.relay_task.cancel()
        if self.relay_ws is not None:
            try:
                await self.relay_ws.close()
            except Exception:
                pass
            self.relay_ws = None
        if self.relay_session is not None:
            try:
                await self.relay_session.close()
            except Exception:
                pass
            self.relay_session = None
        if self.timeout_task and not self.timeout_task.done():
            self.timeout_task.cancel()
        if self.read_task and not self.read_task.done():
            self.read_task.cancel()

        if self.socket is not None:
            try:
                self.socket.close()
            except Exception:
                pass
            self.socket = None

        if self.container is not None:
            try:
                self.container.stop(timeout=2)
            except Exception:
                pass
            try:
                self.container.remove(force=True)
            except Exception:
                pass
            self.container = None

        if self.staging_dir:
            _cleanup_staging_dir(self.staging_dir)
            self.staging_dir = ""

        await self._unregister_metrics_session()

    async def _authenticate_scope(self, token_param: Optional[str] = None):
        # Resolve the user from auth header or token query param.
        headers = {k.lower(): v for k, v in (self.scope.get("headers") or [])}
        auth_header = headers.get(b"authorization", b"").decode("utf-8")

        if token_param and not auth_header:
            token_param = token_param.strip()
            if token_param:
                if "." in token_param:
                    auth_header = f"Bearer {token_param}"
                else:
                    auth_header = f"Token {token_param}"

        if auth_header.startswith("Bearer "):
            token = auth_header.split("Bearer ")[-1].strip()
            jwt_auth = JWTAuthentication()
            try:
                validated = jwt_auth.get_validated_token(token)  # type: ignore[arg-type]
                return jwt_auth.get_user(validated)
            except Exception as e:
                logger.debug(
                    "Shell WS JWT auth failed (str token)",
                    extra={"error": f"{type(e).__name__}: {e}"},
                )
            user = await self._validate_simplejwt_token(token)
            if user:
                return user
            try:
                validated = jwt_auth.get_validated_token(token.encode("utf-8"))
                return jwt_auth.get_user(validated)
            except Exception as e:
                logger.warning(
                    "Shell WS JWT auth failed",
                    extra={"error": f"{type(e).__name__}: {e}"},
                )
                user = await self._validate_simplejwt_token(token)
                if user:
                    return user
                if settings.DEBUG:
                    user = await self._debug_user_from_token(token)
                    if user:
                        logger.warning(
                            "Shell WS using DEBUG token fallback",
                            extra={"user_id": getattr(user, "id", None)},
                        )
                        return user
                return None

        if auth_header.startswith("Token "):
            token = auth_header.split("Token ")[-1].strip()
            token_auth = TokenAuthentication()
            try:
                user, _ = token_auth.authenticate_credentials(token)
                return user
            except Exception as e:
                logger.debug(
                    "Shell WS token auth failed (str token)",
                    extra={"error": f"{type(e).__name__}: {e}"},
                )
            try:
                user, _ = token_auth.authenticate_credentials(token.encode("utf-8"))  # type: ignore[arg-type]  # DRF accepts bytes at runtime
                return user
            except Exception as e:
                logger.warning(
                    "Shell WS token auth failed",
                    extra={"error": f"{type(e).__name__}: {e}"},
                )
                if settings.DEBUG:
                    user = await self._debug_user_from_token(token)
                    if user:
                        logger.warning(
                            "Shell WS using DEBUG token fallback",
                            extra={"user_id": getattr(user, "id", None)},
                        )
                        return user
                return None

        return None

    async def _validate_simplejwt_token(self, token: str):
        # Validate sliding/access tokens directly when JWTAuthentication fails.
        try:
            validated = SlidingToken(token)  # type: ignore[arg-type]
            user_id = validated.get("user_id")
            if user_id:
                return await self._get_user_by_id(int(user_id))
        except TokenError:
            pass
        except Exception as e:
            logger.debug("Shell WS sliding token failed", extra={"error": f"{type(e).__name__}: {e}"})

        try:
            validated = AccessToken(token)  # type: ignore[arg-type]
            user_id = validated.get("user_id")
            if user_id:
                return await self._get_user_by_id(int(user_id))
        except TokenError:
            pass
        except Exception as e:
            logger.debug("Shell WS access token failed", extra={"error": f"{type(e).__name__}: {e}"})

        return None

    @staticmethod
    @database_sync_to_async
    def _get_environment(env_id: int):
        try:
            return Environment.objects.select_related('assignment__course').get(pk=env_id)
        except Environment.DoesNotExist:
            return None

    @staticmethod
    @database_sync_to_async
    def _is_admin(user, env: Environment) -> bool:
        if getattr(settings, "DEBUG", False):
            return True
        return isCourseStaff(user, env.assignment.course)

    @staticmethod
    @database_sync_to_async
    def _get_user_by_id(user_id: int):
        from django.contrib.auth.models import User
        try:
            return User.objects.get(pk=user_id)
        except Exception:
            return None

    async def _debug_user_from_token(self, token: str):
        # DEBUG-only fallback to map JWT to user without signature verification.
        try:
            payload = jwt.decode(token, options={"verify_signature": False})
            user_id = payload.get("user_id")
            if user_id:
                return await self._get_user_by_id(int(user_id))
        except Exception as e:
            logger.debug(
                "Shell WS DEBUG token decode failed",
                extra={"error": f"{type(e).__name__}: {e}"},
            )
        return None

    @staticmethod
    @database_sync_to_async
    def _open_shell_session_sync(env, include_datasets, include_assignment_files, timeout_seconds, labels, tmpfs_size, run_pre_script=False, dataset_ids=None):
        return Executor.open_shell_session(
            env=env,
            include_datasets=include_datasets,
            include_assignment_files=include_assignment_files,
            timeout_seconds=timeout_seconds,
            labels=labels,
            tmpfs_size=tmpfs_size,
            run_pre_script=run_pre_script,
            dataset_ids=dataset_ids,
        )

    @staticmethod
    def _parse_bool(value: str) -> bool:
        # Parse truthy query params.
        return str(value).lower() in {"1", "true", "yes", "on"}

    @staticmethod
    def _parse_int(value: str) -> int:
        # Parse integer query params with a safe default.
        try:
            return int(value)
        except Exception:
            return DEFAULT_SHELL_TIMEOUT_SECONDS

    @staticmethod
    def _parse_id_list(value: Optional[str]) -> Optional[list[int]]:
        # "1,2,3" → [1, 2, 3]; absent → None (caller's default); "" → [] (explicitly none).
        # Non-numeric entries are dropped rather than failing the whole connection.
        if value is None:
            return None
        ids = []
        for part in str(value).split(","):
            part = part.strip()
            if part.isdigit():
                ids.append(int(part))
        return ids

    async def send_json(self, data):
        # Send JSON payload over websocket.
        await self.send(text_data=json.dumps(data))

    @staticmethod
    def _should_relay() -> bool:
        # Return True when legacy WS relay is configured.
        return bool(getattr(settings, "WORKER_SHELL_WS_URL", ""))

    @staticmethod
    def _should_relay_redis() -> bool:
        # Return True when Redis relay is configured and not forced local.
        if getattr(settings, "WORKER_SHELL_FORCE_LOCAL", False):
            return False
        return bool(getattr(settings, "WORKER_SHELL_REDIS_URL", ""))

    @staticmethod
    def _get_redis_url() -> str:
        # Resolve Redis URL for relay/metrics.
        return (
            getattr(settings, "WORKER_SHELL_REDIS_URL", "")
            or getattr(settings, "CELERY_BROKER_URL", "")
        )

    def _get_redis_client(self) -> Optional[redis_async.Redis]:
        # Create a Redis async client.
        url = self._get_redis_url()
        if not url:
            return None
        return redis_async.from_url(url, decode_responses=True)

    async def _get_metrics_client(self) -> Optional[redis_async.Redis]:
        # Reuse relay Redis client or create one for metrics only.
        if self.redis_client is not None:
            return self.redis_client
        if self.metrics_client is not None:
            return self.metrics_client
        url = self._get_redis_url()
        if not url:
            return None
        self.metrics_client = redis_async.from_url(url, decode_responses=True)
        return self.metrics_client

    async def _metrics_incr(self, key: str) -> None:
        # Increment a Redis counter for metrics.
        client = await self._get_metrics_client()
        if not client:
            return
        try:
            await client.incr(key)
        except Exception:
            pass

    async def _metrics_touch(self) -> None:
        # Update last-activity timestamp for the session.
        if not self.session_id:
            return
        client = await self._get_metrics_client()
        if not client:
            return
        try:
            await client.set(f"{METRICS_LAST_ACTIVITY_PREFIX}{self.session_id}", str(time.time()))
        except Exception:
            pass

    async def _register_metrics_session(self, env_id: int, user_id: int, timeout_seconds: int, worker_id: str):
        # Register an active session in Redis metrics.
        if not self.session_id:
            return
        client = await self._get_metrics_client()
        if not client:
            return
        try:
            await client.sadd(METRICS_ACTIVE_KEY, self.session_id)  # type: ignore[misc]  # redis async stubs
            await client.hset(  # type: ignore[misc]  # redis async stubs
                METRICS_SESSIONS_KEY,
                self.session_id,
                json.dumps(
                    {
                        "envId": env_id,
                        "userId": user_id,
                        "workerId": worker_id,
                        "startedAt": time.time(),
                        "expiresAt": time.time() + timeout_seconds,
                    }
                ),
            )
            await self._metrics_touch()
        except Exception:
            pass

    async def _unregister_metrics_session(self):
        # Remove session from Redis metrics.
        if not self.session_id:
            return
        client = await self._get_metrics_client()
        if not client:
            return
        try:
            await client.srem(METRICS_ACTIVE_KEY, self.session_id)  # type: ignore[misc]  # redis async stubs
            await client.hdel(METRICS_SESSIONS_KEY, self.session_id)  # type: ignore[misc]  # redis async stubs
            await client.delete(f"{METRICS_LAST_ACTIVITY_PREFIX}{self.session_id}")  # type: ignore[misc]  # redis async stubs
        except Exception:
            pass

    @staticmethod
    def _session_key(session_id: str) -> str:
        # Build Redis key for a session mapping.
        return f"shell:session:{session_id}"

    async def _load_session(self, session_id: str) -> Optional[dict]:
        # Load session metadata from Redis.
        if not self.redis_client:
            return None
        data = await self.redis_client.hgetall(self._session_key(session_id))  # type: ignore[misc]  # redis async stubs
        return data or None

    async def _save_session(self, session_id: str, mapping: dict, ttl_seconds: int) -> None:
        # Persist session metadata in Redis.
        if not self.redis_client:
            return
        key = self._session_key(session_id)
        await self.redis_client.hset(key, mapping=mapping)  # type: ignore[misc]  # redis async stubs
        await self.redis_client.expire(key, ttl_seconds)  # type: ignore[misc]  # redis async stubs

    async def _select_worker(self) -> Optional[str]:
        # Pick an active worker based on recent heartbeats.
        if not self.redis_client:
            return None
        worker_ids = await self.redis_client.smembers("shell:workers")  # type: ignore[misc]  # redis async stubs
        if not worker_ids:
            return None
        now_ts = time.time()
        candidates = []
        for worker_id in worker_ids:
            data = await self.redis_client.hgetall(f"shell:worker:{worker_id}")  # type: ignore[misc]  # redis async stubs
            try:
                last_seen = float(data.get("last_seen", "0"))
            except Exception:
                last_seen = 0.0
            if now_ts - last_seen <= 20:
                candidates.append((last_seen, worker_id))
        if not candidates:
            return None
        candidates.sort(key=lambda x: x[0])
        oldest_seen = candidates[0][0]
        oldest_workers = [wid for last, wid in candidates if last == oldest_seen]
        return random.choice(oldest_workers)

    async def _connect_relay_redis(
        self,
        env_id: int,
        user,
        include_datasets: bool,
        include_assignment_files: bool,
        timeout_seconds: int,
        needs_network: bool,
        run_pre_script: bool = False,
        dataset_ids: Optional[list[int]] = None,
    ):
        # Set up Redis pub/sub relay to a worker session.
        self.redis_client = self._get_redis_client()
        if not self.redis_client:
            await self.close(code=5006)
            return

        session_id = self.session_id or uuid.uuid4().hex
        self.session_id = session_id

        session = await self._load_session(session_id)
        if session and session.get("worker_id"):
            self.worker_id = session.get("worker_id")
        else:
            worker_id = await self._select_worker()
            if not worker_id:
                await self.close(code=5007)
                return
            self.worker_id = worker_id
            await self._save_session(
                session_id,
                {
                    "env_id": str(env_id),
                    "user_id": str(user.id),
                    "worker_id": worker_id,
                    "created_at": str(time.time()),
                },
                ttl_seconds=timeout_seconds + 120,
            )
            await self._redis_publish(
                f"shell:in:{worker_id}:{session_id}",
                {
                    "type": "start",
                    "sessionId": session_id,
                    "envId": env_id,
                    "userId": user.id,
                    "includeDatasets": include_datasets,
                    "includeAssignmentFiles": include_assignment_files,
                    "timeoutSeconds": timeout_seconds,
                    "networkAccess": needs_network,
                    "runPreScript": run_pre_script,
                    "datasetIds": dataset_ids,
                },
            )

        self.redis_pubsub = self.redis_client.pubsub()
        await self.redis_pubsub.subscribe(f"shell:out:{session_id}")
        await self.accept()
        user_id = getattr(user, "id", None)
        if user_id is not None:
            await self._register_metrics_session(env_id, int(user_id), timeout_seconds, worker_id=self.worker_id or "redis")
        self.redis_task = asyncio.create_task(self._redis_pump())

    async def _redis_publish(self, channel: str, payload: dict) -> None:
        # Publish a JSON payload to Redis.
        if not self.redis_client:
            return
        await self.redis_client.publish(channel, json.dumps(payload))

    async def _redis_pump(self):
        # Consume Redis output channel and forward to client websocket.
        if not self.redis_pubsub:
            return
        async for message in self.redis_pubsub.listen():  # type: ignore[union-attr]
            if message.get("type") != "message":
                continue
            data = message.get("data")
            try:
                payload = json.loads(data)
            except Exception:
                continue

            msg_type = payload.get("type")
            if msg_type == "ready":
                await self.send_json(payload)
            elif msg_type == "data":
                await self.send(text_data=payload.get("data", ""))
                await self._metrics_incr(METRICS_OUT_KEY)
                await self._metrics_touch()
            elif msg_type == "error":
                await self.send_json(payload)
            elif msg_type == "closed":
                await self.close(code=4000)
                break

    async def _redis_send(self, text_data=None, bytes_data=None):
        # Forward websocket input to the Redis relay channel.
        if not self.worker_id or not self.session_id:
            return
        data = bytes_data
        if text_data is not None:
            data = text_data.encode("utf-8")
        if not data:
            return
        await self._redis_publish(
            f"shell:in:{self.worker_id}:{self.session_id}",
            {"type": "input", "data": data.decode("utf-8", errors="replace")},
        )
        await self._metrics_incr(METRICS_IN_KEY)
        await self._metrics_touch()

    async def _connect_relay(
        self,
        env_id: int,
        user,
        include_datasets: bool,
        include_assignment_files: bool,
        timeout_seconds: int,
        run_pre_script: bool = False,
        dataset_ids: Optional[list[int]] = None,
    ):
        # Connect to legacy worker WS relay.
        worker_url = getattr(settings, "WORKER_SHELL_WS_URL", "")
        shared_secret = getattr(settings, "WORKER_SHELL_SHARED_SECRET", "")
        if not worker_url or not shared_secret:
            await self.close(code=5004)
            return

        qs = {
            "userId": str(user.id),
            "includeDatasets": "true" if include_datasets else "false",
            "includeAssignmentFiles": "true" if include_assignment_files else "false",
            "timeoutSeconds": str(timeout_seconds),
            "runPreScript": "true" if run_pre_script else "false",
        }
        if dataset_ids is not None:
            qs["datasetIds"] = ",".join(str(i) for i in dataset_ids)
        query = "&".join([f"{k}={v}" for k, v in qs.items()])
        ws_url = f"{worker_url.rstrip('/')}/ws/internal/autograder/environments/{env_id}/shell/?{query}"

        headers = {"X-Codepost-Worker-Secret": shared_secret}
        self.relay_session = aiohttp.ClientSession()
        try:
            self.relay_ws = await self.relay_session.ws_connect(ws_url, headers=headers)
        except Exception as e:
            logger.error(f"Failed to connect relay WS: {e}")
            await self._cleanup()
            await self.close(code=5005)
            return

        await self.accept()
        self.relay_task = asyncio.create_task(self._relay_pump())

    async def _relay_pump(self):
        # Forward messages from legacy relay WS to the client.
        if self.relay_ws is None:
            return
        async for msg in self.relay_ws:
            if msg.type == aiohttp.WSMsgType.TEXT:
                await self.send(text_data=msg.data)
            elif msg.type == aiohttp.WSMsgType.BINARY:
                await self.send(bytes_data=msg.data)
            elif msg.type in (aiohttp.WSMsgType.CLOSED, aiohttp.WSMsgType.ERROR):
                break
        await self.close(code=4000)

    async def _relay_send(self, text_data=None, bytes_data=None):
        # Send input to legacy relay WS.
        if self.relay_ws is None:
            return
        if text_data is not None:
            await self.relay_ws.send_str(text_data)
        elif bytes_data is not None:
            await self.relay_ws.send_bytes(bytes_data)


class WorkerShellConsumer(AsyncWebsocketConsumer):
    """
    Internal-only consumer for worker-hosted containers.
    Protected by shared secret header and userId query param.
    """

    container = None
    socket = None
    staging_dir: str = ""
    timeout_task: Optional[asyncio.Task] = None
    read_task: Optional[asyncio.Task] = None

    async def connect(self):
        # Validate internal request and start a worker-hosted container.
        env_id = self.scope.get("url_route", {}).get("kwargs", {}).get("environment_id")
        if not env_id:
            await self.close(code=4000)
            return

        if not self._is_internal_request():
            await self.close(code=4003)
            return

        query = parse_qs((self.scope.get("query_string") or b"").decode())
        user_id = query.get("userId", [None])[0]
        if not user_id:
            await self.close(code=4001)
            return

        user = await self._get_user(user_id)
        env = await self._get_environment(env_id)
        if not env or not user:
            await self.close(code=4004)
            return

        if not await self._is_admin(user, env):
            await self.close(code=4005)
            return

        include_datasets = EnvironmentShellConsumer._parse_bool(query.get("includeDatasets", ["true"])[0])
        include_assignment_files = EnvironmentShellConsumer._parse_bool(query.get("includeAssignmentFiles", ["true"])[0])
        run_pre_script = EnvironmentShellConsumer._parse_bool(query.get("runPreScript", ["false"])[0])
        dataset_ids = EnvironmentShellConsumer._parse_id_list(query.get("datasetIds", [None])[0])
        timeout_seconds = _normalize_timeout(
            EnvironmentShellConsumer._parse_int(
                query.get("timeoutSeconds", [str(DEFAULT_SHELL_TIMEOUT_SECONDS)])[0]
            )
        )

        try:
            labels = {
                "codepost.env_id": str(env.id),
                "codepost.assignment_id": str(env.assignment.id),
                "codepost.user_id": str(getattr(user, "id", "")),
                "codepost.type": "shell_session",
                "codepost.created_at": timezone.now().isoformat(),
                "codepost.timeout_seconds": str(timeout_seconds),
            }
            executor, volumes, _docker_env, temp_staging_dir, self.container, self.socket = (
                Executor.open_shell_session(
                    env=env,
                    include_datasets=include_datasets,
                    include_assignment_files=include_assignment_files,
                    timeout_seconds=timeout_seconds,
                    labels=labels,
                    run_pre_script=run_pre_script,
                    dataset_ids=dataset_ids,
                )
            )
            self.staging_dir = temp_staging_dir
        except Exception as e:
            logger.error(f"Worker shell start failed: {e}")
            await self._cleanup()
            await self.close(code=5003, reason="Failed to start shell container")
            return

        await self.accept()
        await self.send(text_data=json.dumps({
            "type": "ready",
            "containerId": self.container.id,
            "image": env.image_name,
            "expiresAt": (timezone.now() + timedelta(seconds=timeout_seconds)).isoformat(),
            "workingDir": "/work",
            "mounts": _format_mounts(volumes),
        }))

        self.read_task = asyncio.create_task(self._pump_container_output())
        self.timeout_task = asyncio.create_task(self._auto_close(timeout_seconds))

    async def disconnect(self, code):
        # Cleanup on internal worker websocket disconnect.
        await self._cleanup()

    async def receive(self, text_data=None, bytes_data=None):
        # Forward input to the worker container PTY.
        if not self.socket:
            return

        data = bytes_data
        if text_data is not None:
            data = text_data.encode("utf-8")

        if not data:
            return

        try:
            self.socket._sock.sendall(data)
        except Exception as e:
            logger.error(f"Worker shell send failed: {e}")

    async def _pump_container_output(self):
        # Stream worker container output to websocket.
        if not self.socket:
            return
        loop = asyncio.get_running_loop()

        def _read_loop():
            sock = getattr(self.socket, "_sock", None)
            if sock is None:
                return
            try:
                while True:
                    chunk = sock.recv(4096)
                    if not chunk:
                        break
                    async_to_sync(self.send)(text_data=chunk.decode("utf-8", errors="replace"))
            except Exception as e:
                logger.error(f"Worker shell read failed: {e}")

        await loop.run_in_executor(None, _read_loop)

    async def _auto_close(self, timeout_seconds: int):
        # Auto-close worker shell after timeout.
        await asyncio.sleep(timeout_seconds)
        await self.close(code=4000)

    async def _cleanup(self):
        # Stop and remove worker container and cleanup staging.
        if self.timeout_task and not self.timeout_task.done():
            self.timeout_task.cancel()
        if self.read_task and not self.read_task.done():
            self.read_task.cancel()

        if self.socket is not None:
            try:
                self.socket.close()
            except Exception:
                pass
            self.socket = None

        if self.container is not None:
            try:
                self.container.stop(timeout=2)
            except Exception:
                pass
            try:
                self.container.remove(force=True)
            except Exception:
                pass
            self.container = None

        if self.staging_dir:
            _cleanup_staging_dir(self.staging_dir)
            self.staging_dir = ""

    def _is_internal_request(self) -> bool:
        # Validate shared secret for internal worker requests.
        headers = {k.lower(): v for k, v in (self.scope.get("headers") or [])}
        secret = headers.get(b"x-codepost-worker-secret", b"").decode("utf-8")
        expected = getattr(settings, "WORKER_SHELL_SHARED_SECRET", "")
        return bool(expected) and secret == expected

    @staticmethod
    @database_sync_to_async
    def _get_environment(env_id: int):
        # Load environment in async-safe way.
        try:
            return Environment.objects.get(pk=env_id)
        except Environment.DoesNotExist:
            return None

    @staticmethod
    @database_sync_to_async
    def _get_user(user_id: str):
        # Load user for internal worker requests.
        from django.contrib.auth.models import User
        try:
            return User.objects.get(pk=int(user_id))
        except Exception:
            return None

    @staticmethod
    @database_sync_to_async
    def _is_admin(user, env: Environment) -> bool:
        # Check staff permissions (DEBUG bypass).
        if getattr(settings, "DEBUG", False):
            return True
        return isCourseStaff(user, env.assignment.course)

    @staticmethod
    @database_sync_to_async
    def _build_shell_context(env: Environment, include_datasets: bool, include_assignment_files: bool):
        # Build worker container context (volumes/env).
        return _build_shell_context(
            env=env,
            include_datasets=include_datasets,
            include_assignment_files=include_assignment_files,
        )
