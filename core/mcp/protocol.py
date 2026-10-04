# Copyright © 2026 Rutgers, the State University of New Jersey. All rights reserved except as defined by the Rutgers Non-Commercial License, included with this software.
"""MCP JSON-RPC 2.0 handling — stateless Streamable HTTP.

Deliberately hand-rolled rather than pulled from an SDK.  Django's ASGI stack
cannot give a mounted sub-app lifespan events (``ProtocolTypeRouter`` raises on
any scope type it doesn't map, so uvicorn disables lifespan entirely), which is
exactly what the SDK's session manager needs.  A plain DRF view has no such
dependency, and it lets the real ``CourseAPIKeyAuthentication`` and throttle
classes run unmodified.

Stateless is the default: no ``Mcp-Session-Id`` is issued unless the client's
``initialize`` declares the ``elicitation`` capability, in which case a
session-lite id (see ``core/mcp/sessions.py``) lets later Tier-3 tool calls
stream an in-chat approval dialog. That state is in-process, which is exactly
why production serves ``/mcp`` from the single-process ``codepost-mcp``
container — never scale it to multiple workers.
"""
from __future__ import annotations

import json
from typing import Any

from core.agent import registry
from core.agent.errors import ToolError
from core.mcp.schema import to_mcp_tool

SERVER_NAME = 'codepost'
SERVER_TITLE = 'codePost'

# Revisions we can speak, newest first. Echo the client's if we know it,
# otherwise answer with our latest and let the client decide to disconnect.
SUPPORTED_PROTOCOL_VERSIONS = ('2025-06-18', '2025-03-26')
LATEST_PROTOCOL_VERSION = SUPPORTED_PROTOCOL_VERSIONS[0]
# Per spec: a client that sends no MCP-Protocol-Version header predates the
# header, so assume the revision that introduced it.
ASSUMED_PROTOCOL_VERSION = '2025-03-26'

# JSON-RPC 2.0 error codes
PARSE_ERROR = -32700
INVALID_REQUEST = -32600
METHOD_NOT_FOUND = -32601
INVALID_PARAMS = -32602
INTERNAL_ERROR = -32603


class JSONRPCError(Exception):
    def __init__(self, code: int, message: str, data: Any = None):
        super().__init__(message)
        self.code = code
        self.message = message
        self.data = data


def _result(request_id: Any, result: dict) -> dict:
    return {'jsonrpc': '2.0', 'id': request_id, 'result': result}


def _error(request_id: Any, code: int, message: str, data: Any = None) -> dict:
    err: dict[str, Any] = {'code': code, 'message': message}
    if data is not None:
        err['data'] = data
    return {'jsonrpc': '2.0', 'id': request_id, 'error': err}


def handle(body: Any, conn) -> dict | None:
    """Dispatch one JSON-RPC message.

    Returns the response object, or ``None`` when the message was a
    notification — the caller must then answer 202 with an empty body, which
    the spec requires for notifications and responses alike.
    """
    # Batching was removed in the 2025-06-18 revision.
    if isinstance(body, list):
        raise JSONRPCError(INVALID_REQUEST,
                           'JSON-RPC batching is not supported by this server.')
    if not isinstance(body, dict):
        raise JSONRPCError(INVALID_REQUEST, 'Request body must be a JSON-RPC object.')
    if body.get('jsonrpc') != '2.0':
        raise JSONRPCError(INVALID_REQUEST, "Missing or invalid 'jsonrpc' version.")

    # A JSON-RPC *response* (id + result/error, no method) is a client
    # answering an elicitation we sent on an SSE stream. Route it to the
    # waiting call; per spec the HTTP answer is 202 either way.
    if 'method' not in body and ('result' in body or 'error' in body) \
            and body.get('id') is not None:
        from core.mcp import elicitation
        elicitation.deliver(body['id'], body)
        return None

    method = body.get('method')
    request_id = body.get('id')
    params = body.get('params') or {}

    # No "id" means a notification (or a response to us, which we never solicit).
    if request_id is None:
        return None

    if not isinstance(method, str):
        return _error(request_id, INVALID_REQUEST, "Missing 'method'.")

    try:
        if method == 'initialize':
            return _result(request_id, _initialize(params, conn))
        if method == 'ping':
            return _result(request_id, {})
        if method == 'tools/list':
            return _result(request_id, _tools_list(conn))
        if method == 'tools/call':
            return _result(request_id, _tools_call(params, conn))
        # Advertised nowhere, but clients probe them; an empty list beats an error.
        if method in ('prompts/list', 'resources/list', 'resources/templates/list'):
            key = method.split('/')[0]
            return _result(request_id, {key: []})
    except JSONRPCError as exc:
        return _error(request_id, exc.code, exc.message, exc.data)

    return _error(request_id, METHOD_NOT_FOUND, f'Unknown method: {method}')


def _initialize(params: dict, conn) -> dict:
    requested = params.get('protocolVersion')
    version = (requested if requested in SUPPORTED_PROTOCOL_VERSIONS
               else LATEST_PROTOCOL_VERSION)

    # Session-lite: only a client that can show elicitation dialogs gets a
    # session (used by Tier-3 confirmations to stream an approval request).
    # Everyone else stays fully stateless, exactly as before.
    client_caps = params.get('capabilities') or {}
    if isinstance(client_caps, dict) and 'elicitation' in client_caps:
        from core.mcp import sessions
        conn.new_session_id = sessions.create(client_caps)

    if conn.pinned:
        instructions = (
            'Tools for managing one codePost course. The course is fixed by the '
            'API key, so no tool takes a course id. Call '
            'codepost_get_course_overview first — it is the only way to resolve '
            'assignment names to ids, and it reports what this key may do.'
        )
    else:
        instructions = (
            'Tools for managing codePost courses with your personal instructor '
            'credential. Call codepost_list_courses first, ask the user which '
            'course they mean if it is ambiguous, and pass that courseId to every '
            'other tool. Then codepost_get_course_overview resolves assignment '
            'names to ids within the chosen course.'
        )
    instructions += (
        ' Students are identified by stable pseudonymous aliases such as '
        'student-3f9a1c2d40 — never by email. Use the alias anywhere a tool '
        'takes a student; staff (graders, admins) are shown by email. Never '
        'put an alias in text a student will read. The instructor can resolve '
        'an alias under Course Settings → Student Aliases.'
    )
    return {
        'protocolVersion': version,
        # listChanged is false: the tool set is fixed for a credential's scope,
        # so there is nothing to notify about.
        'capabilities': {'tools': {'listChanged': False}},
        'serverInfo': {'name': SERVER_NAME, 'title': SERVER_TITLE,
                       'version': _server_version()},
        'instructions': instructions,
    }


def _tools_list(conn) -> dict:
    registry.load_tools()
    if conn.pinned:
        specs = registry.visible_tools(conn.pinned_context())
        return {'tools': [to_mcp_tool(spec) for spec in specs]}
    # Unpinned (personal token): scope filter only — there is no course yet, so
    # the capability filter can't run; per-call enforcement covers it. Every
    # course-bound schema gains a required courseId here, so each credential
    # type sees a coherent, non-conditional surface.
    specs = registry.visible_tools_unscoped(conn.scope)
    return {'tools': [to_mcp_tool(spec, inject_course_id=spec.course_bound)
                      for spec in specs]}


def _tools_call(params: dict, conn) -> dict:
    registry.load_tools()
    name = params.get('name')
    arguments = params.get('arguments') or {}

    if not isinstance(name, str):
        raise JSONRPCError(INVALID_PARAMS, "tools/call requires a 'name'.")
    if not isinstance(arguments, dict):
        raise JSONRPCError(INVALID_PARAMS, "'arguments' must be an object.")

    spec = registry.get(name)
    # An unknown tool is a protocol error; so is one this credential may not
    # see, since revealing the difference would leak the tool's existence.
    if spec is None or not _may_call(spec, conn):
        raise JSONRPCError(METHOD_NOT_FOUND, f'Unknown tool: {name}')

    # courseId is an adapter-level argument, popped before schema validation —
    # it is injected into the advertised schema for unpinned connections but is
    # not part of any ToolSpec.input_schema.
    course_id = arguments.pop('courseId', None)

    try:
        _validate_arguments(spec, arguments)
    except JSONRPCError:
        raise
    except Exception as exc:                                  # pragma: no cover
        raise JSONRPCError(INVALID_PARAMS, str(exc))

    # Everything the model should be able to recover from — archived course,
    # missing capability, illegal transition — is a result with isError, never
    # a protocol error. Protocol errors are for malformed calls only.
    course_obj = None
    try:
        ctx = _resolve_context(spec, conn, course_id)
        course_obj = ctx.course
        if not spec.read_only:
            _enforce_write_gates(spec, ctx, conn)
            ctx.require_writable()      # archived preflight, for every write tool
        payload = spec.handler(ctx, **arguments)
    except ToolError as exc:
        _audit_write(spec, conn, arguments, course=course_obj, denied=exc)
        return _tool_result(_scrub(exc.to_payload(), course_obj), is_error=True)

    _audit_write(spec, conn, arguments, course=course_obj,
                 applied=not arguments.get('dryRun', _dry_default(spec)))
    return _tool_result(_scrub(payload, course_obj), is_error=False)


def _scrub(payload, course):
    """Pseudonymize student identities in everything that leaves the boundary.

    Built AFTER the handler ran, so roster changes made by this very call
    (update_roster) are already in the map. Errors raised before the course
    resolved carry no student data."""
    if course is None:                       # courseless tools (list_courses)
        return payload
    from core.agent.privacy import StudentAliasMap
    return StudentAliasMap.for_course(course).scrub(payload)


def _dry_default(spec) -> bool:
    prop = (spec.input_schema.get('properties') or {}).get('dryRun') or {}
    return bool(prop.get('default', False))


def _enforce_write_gates(spec, ctx, conn) -> None:
    """Belt over the visibility filter: refuse a write above the key's scope
    even if a stale client cached an older tool list."""
    if not registry.scope_permits(conn.scope, spec.min_scope):
        from core.agent.errors import insufficient_key_scope
        raise insufficient_key_scope(spec.name, spec.min_scope, conn.scope)


def _audit_write(spec, conn, arguments, *, course=None, applied: bool = False,
                 denied=None) -> None:
    """One audit row per agent write (or denied write), from the executor so no
    tool can forget it. Reads are never audited — they would flood the log."""
    if spec.read_only:
        return
    try:
        from core.models import Course
        from core.services.audit import record_audit_event

        if course is None and conn.pinned:
            course = Course.objects.filter(pk=conn.pinned_course_id).first()
        if course is None:
            return                       # denial happened before course resolution

        meta = {
            'tool': spec.name,
            'origin': 'mcp',
            'args': _redact(arguments),
            'applied': applied,
        }
        if denied is not None:
            meta['deniedCode'] = getattr(denied, 'code', 'UNKNOWN')
        record_audit_event(
            course=course,
            event_type='agent_write_denied' if denied is not None else 'agent_write',
            user=conn.user if getattr(conn.user, 'pk', None) else None,
            meta=meta)
    except Exception:                                          # pragma: no cover
        # Auditing must never turn a successful tool call into a failure.
        pass


def _redact(arguments: dict) -> dict:
    """Keep the audit row structural: names/ids/flags, not free prose.

    Long strings could carry student PII into a JSON blob that auditLogExport
    writes to CSV."""
    out = {}
    for key, value in arguments.items():
        if isinstance(value, str) and len(value) > 80:
            out[key] = value[:77] + '…'
        elif isinstance(value, list) and len(value) > 10:
            out[key] = f'[{len(value)} items]'
        elif isinstance(value, dict):
            out[key] = f'{{{len(value)} keys}}'
        else:
            out[key] = value
    return out


def _may_call(spec, conn) -> bool:
    """Call-time gate: key scope (and pinned-only tools) — deliberately NOT the
    per-course capability filter. Capabilities shape `tools/list` as a UX
    nicety, but at call time the archived preflight and the dispatched
    viewset's permissions produce far better errors than 'Unknown tool' —
    an agent told COURSE_ARCHIVED can explain; one told the tool doesn't
    exist cannot."""
    if spec.unscoped_only and conn.pinned:
        return False
    return registry.scope_permits(conn.scope, spec.min_scope)


def _resolve_context(spec, conn, course_id):
    """Turn the connection plus an optional courseId argument into a context.

    Pinned: the key's course, always; a stray courseId argument must agree.
    Unpinned: courseId is required for course-bound tools, and the caller must
    staff that course (checked in Connection.context_for).
    """
    if not spec.course_bound:
        return conn.courseless_context()
    if conn.pinned:
        if course_id is not None and int(course_id) != int(conn.pinned_course_id):
            raise ToolError(
                'NOT_IN_SCOPE',
                f'This API key is fixed to course {conn.pinned_course_id}; '
                f'it cannot act on course {course_id}.',
                remedy='Drop the courseId argument — the course is implied by the key.')
        return conn.pinned_context()
    if course_id is None:
        raise ToolError(
            'COURSE_REQUIRED',
            f'{spec.name} needs a courseId when connected with a personal token.',
            remedy='Call codepost_list_courses, confirm the course with the user, '
                   'then pass its id as courseId.',
            retryable=True)
    return conn.context_for(course_id)


def _tool_result(payload: dict, *, is_error: bool) -> dict:
    text = json.dumps(payload, indent=2, default=str)
    result: dict[str, Any] = {
        'content': [{'type': 'text', 'text': text}],
        'isError': is_error,
    }
    if not is_error:
        # Spec: a tool returning structured content SHOULD also serialise it
        # into a text block, for clients that don't read structuredContent.
        result['structuredContent'] = payload
    return result


def _validate_arguments(spec, arguments: dict) -> None:
    """Enforce the parts of the tool's JSON Schema that matter at this boundary.

    Deliberately not a full JSON Schema implementation — required keys, unknown
    keys, and enum membership are the three failures a model actually makes,
    and each is far more useful caught here with a precise message than as a
    TypeError from the handler.
    """
    schema = spec.input_schema or {}
    properties = schema.get('properties', {})
    required = schema.get('required', [])

    missing = [key for key in required if key not in arguments]
    if missing:
        raise JSONRPCError(INVALID_PARAMS,
                           f"{spec.name} is missing required argument(s): "
                           f"{', '.join(missing)}")

    if schema.get('additionalProperties') is False:
        unknown = [key for key in arguments if key not in properties]
        if unknown:
            raise JSONRPCError(
                INVALID_PARAMS,
                f"{spec.name} got unknown argument(s): {', '.join(unknown)}. "
                f"Valid arguments: {', '.join(sorted(properties)) or 'none'}")

    for key, value in arguments.items():
        prop = properties.get(key)
        if not isinstance(prop, dict):
            continue
        choices = prop.get('enum')
        if choices is not None and value not in choices:
            raise JSONRPCError(
                INVALID_PARAMS,
                f"{spec.name}.{key} must be one of {choices}; got {value!r}")


def _server_version() -> str:
    """The API version, which lives in SPECTACULAR_SETTINGS rather than at top level."""
    from django.conf import settings
    return getattr(settings, 'SPECTACULAR_SETTINGS', {}).get('VERSION', '0.0.0')
