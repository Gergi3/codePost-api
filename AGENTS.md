# codePost API — Agent Guidelines

## Architecture

Django 6 + DRF REST API serving the codePost platform. The `core/` app holds all domain models, views, serializers, and permissions. `autograder/` handles sandboxed code execution via Docker + Celery. `webhooks/` delivers async event hooks. `log/` stores audit events.

- **Models**: Single monolithic `core/models.py` — all inherit from `BaseModel` (auto `created`/`modified` timestamps, field change tracking).
- **Views**: One ViewSet per file in `core/views/`, inheriting `ListProtectedViewSet` or `SuperUserListProtectedViewSet`. Sub-resources use `@action`.
- **Serializers**: One per file in `core/serializers/`, inheriting `ModelSerializerWithPOSTCheck` (supports `createForPOSTCheck()` for pre-permission validation).
- **Permissions**: `core/permissions/` — `TemplatePermission` base validates POST data via serializer before `has_object_permission`. Helpers: `isStudent()`, `isGrader()`, `isCourseAdmin()`, `isCourseMember()`, `isStaffOfSub()`, etc.
- **URL routing**: Central `DefaultRouter` in `codepost/urls.py`. Autograder has a separate router.

## Code Style

- Every file must start with: `# Copyright © 2026 Rutgers, the State University of New Jersey. All rights reserved except as defined by the Rutgers Non-Commercial License, included with this software.`
- camelCase for serializer field names and API responses. NOTE: `djangorestframework-camel-case` is **not** installed globally — only a few views in `core/views/system.py` use its renderer. Fields are camelCase because the serializers name them that way, and snake_case leaks through in places (`inactive_students`, `ai_system_prompt`, query params `event_type`/`date_from`). snake_case for Python internals.
- Type checking: Pyright `basic` mode. Migrations, `.venv`, and `node_modules` are excluded.
- Use `@extend_schema` / `@extend_schema_field` from `drf_spectacular` for all new endpoints.
- No auto-formatter is enforced — match the style of surrounding code.

## Build and Test

```bash
# Setup
poetry install
python manage.py migrate

# Run dev server (SQLite, eager Celery)
./start_dev.sh              # uvicorn --reload on :8000
./start_dev.sh --local      # force local shell mode (no Redis)
./start_dev.sh --env        # load .env for MySQL/Redis config

# Tests
pytest                      # all tests
pytest core/tests/          # core app tests only
pytest autograder/tests/    # autograder tests only

# Generate OpenAPI schema
.venv/bin/python manage.py spectacular --file schema.yaml

# Regenerate TypeScript client for codePost-ui (run from this repo)
./scripts/generate_ts_client.sh

# Re-sync the in-app user docs (markdown) from codePost-ui into docs/user/
# — they back the agent's codepost_search_docs tool
./scripts/sync_user_docs.sh
```

## Project Conventions

- **Adding a new resource**: Create files in `core/views/<resource>.py`, `core/serializers/<resource>.py`, `core/permissions/` (if needed), register ViewSet in `codepost/urls.py` router, add migration.
- **Permissions pattern**: New permissions inherit `TemplatePermission`. Override `has_permission` and `has_object_permission`. Use helper functions from `core/permissions/helpers.py`.
- **Factories for tests**: Define in `core/tests/factories.py` using `factory.django.DjangoModelFactory`. Mute `post_save` signals with `@factory.django.mute_signals` — critical because signals trigger Celery tasks and extra DB work.
- **Test style**: Extend `rest_framework.test.APITestCase`, use `self.client.force_authenticate(user=...)`, hit actual API endpoints, assert status codes + response data.
- **Celery tasks**: Define in `tasks.py` within each app. Dev mode runs with `CELERY_TASK_ALWAYS_EAGER=TRUE`. Import tasks inside signal handlers to avoid circular imports.
- **Services layer**: Complex business logic goes in `core/services/` or `autograder/services/`, not in views.
- **Emails**: Inherit from `CodepostEmail` in `core/emails.py`. Emails are silently skipped when `TESTING=True`. Respect org-level `send_welcome_email` flag.

## Important Gotchas

- **Assignment lifecycle**: `Assignment.state` (draft/visible/preview/published/closed/archived) is the source of truth — see `docs/assignment_lifecycle.md`. The legacy `isVisible`/`isReleased` columns are **gone** (the API returns them read-only, derived from state; writes 400). Work/visibility gates read `state`; grading reveals (rubric, full tests, finalized-submission views) read `feedbackReleased`. New assignments default to `draft`; `AssignmentFactory` pins `state='preview'`.
- **BaseModel extra query**: Every `save()` on an existing object fires an extra `SELECT` to compute changed fields (for `update_fields` optimization). Be aware of this cost in bulk operations.
- **List endpoints are blocked**: `ListProtectedViewSet.list()` returns 403 for non-superusers. Users access resources via parent object detail/actions (`GET /courses/{id}/` returns assignment **ids only** — there is no `courses/{id}/assignments/` action; clients hydrate per-id via `GET /assignments/{id}/`), not by listing directly.
- **Archived courses block edits**: `ModelSerializerWithPOSTCheck.validate()` raises `ValidationError` for any model attached to an archived `Course`. The `Course` model itself is exempt.
- **No global pagination**: Regular users access resources via parent actions (e.g., `assignments/{id}/submissions/`) which return bounded result sets. Only superuser list endpoints use `ListPagination` (50 per page). For new endpoints needing pagination, add `pagination_class = ListPagination` per-ViewSet.
- **Schema hooks are critical**: `codepost/schema_hooks.py` has `restore_underscore_operation_ids` which restores `tag_action` format in operationIds. Without it, the generated TS client method names break. Must stay in sync with any new tags.
- **`ENUM_NAME_OVERRIDES` match on value+label pairs**: an entry in `codepost/settings.py`'s `ENUM_NAME_OVERRIDES` only applies when its `(value, label)` tuples **exactly** match the model field's `choices` — labels included, not just values. Editing a choice's label (or adding a choice) without updating the matching override silently drops the override, so drf-spectacular auto-names the enum (e.g. `QuizAssignmentTriggerEnum` → `AssignmentTriggerEnum`) and the regenerated TS client breaks `tsc`. When you change a `choices` list, update its `ENUM_NAME_OVERRIDES` entry in lockstep, then regenerate and confirm the enum name is unchanged.
- **Signal side-effects**: `auto_execute_submission` includes a 1-second `time.sleep()`. `auto_detect_on_file_change` fires on `AssignmentFile` save/delete. Always mute signals in test factories.
- **`copy_assignment` uses `update_or_create`** for Environment because the `AssignmentFile` `post_save` signal may have already auto-created one via the `Autodetector`.

## MCP Agent Endpoint

`POST /mcp` — a stateless MCP (Model Context Protocol) Streamable HTTP endpoint so instructors can manage their course from Claude Code / Claude Desktop. Hand-rolled JSON-RPC in `core/mcp/` (no SDK: Django's Channels ASGI stack can't deliver lifespan events to a mounted sub-app, and stateless JSON mode needs none of it). Excluded from the OpenAPI schema.

- **Tool layer**: `core/agent/` — protocol-agnostic registry (`@tool` decorator, `ToolSpec`), in-process dispatch that replays the caller's `Authorization` header through the real viewsets (never the ORM — permission classes are the enforcement), response shaping with a hard size budget. Tool names are prefixed `codepost_`.
- **Credentials**: a course API key (`Authorization: CourseKey cpk_…`) connects *pinned* — the course is implied, no tool takes a courseId. A personal instructor token (`Authorization: Token …`, the SDK credential) connects *unpinned*: `tools/list` adds `codepost_list_courses`, injects a required `courseId` into every course-bound schema, and each call checks `isCourseStaff`. The optional `?scope=read|write` query param on the connect URL narrows any credential.
- **Key scopes**: `CourseAPIKey.scope` ∈ `read`/`write`/`admin` (default `read`). `tools/list` is filtered by scope, so a key never even sees tools above its tier. Enum name pinned as `CourseAPIKeyScopeEnum` in `ENUM_NAME_OVERRIDES`.
- **Capability gates on tools must be course-level** (`compute_course_capabilities` keys) — assignment/submission-level capabilities would silently hide the tool; the registry raises at import time if you try. Per-object permissions come from the dispatched viewset.
- **Deployment**: in production/dev-VM compose, `/mcp` is served by the dedicated single-process `codepost-mcp` container (same image, `uvicorn` on :8001, no init.sh — migrations belong to `codepost-api`), routed by the nginx `location /mcp` block (3600s read timeout, unbuffered). Single process on purpose: it keeps a future stateful MCP mode (SSE/elicitation) possible. Local dev needs none of this — `./start_dev.sh` serves `/mcp` like any route.
- **OAuth**: the API is also an OAuth 2.1 authorization server (django-oauth-toolkit 3.4.1, pinned) so Claude Desktop/claude.ai connectors reach `/mcp` natively — RFC 8414/9728 metadata at `/.well-known/…`, endpoints under `/o/` (trimmed mount: never include `oauth2_provider.urls` wholesale, it exposes self-serve app creation), DCR open+rate-limited, PKCE S256 only, scopes = the agent read/write/admin tiers. Consent needs a Django session: `/auth/agent-login/` (SSO or password) + the CAS callback's opt-in `next` param (`core/views/sso.py`) — with no `next`, SSO behaviour is byte-for-byte unchanged. The MCP view's Bearer auth must stay FIRST in `authentication_classes` (DRF takes the 401 challenge from the first class); the dispatcher exchanges opaque Bearer tokens for an internal JWT (`core/agent/context.py:_dispatch_meta_for`) because the internal viewsets' JWT auth raises on non-JWT Bearers. Tokens must be resource-bound to `{API_URL}/mcp` (unbound → 403). Fallback client: `manage.py seed_oauth_application`.
- **Tier-3 confirmations**: unrecoverable operations (deletes, attempt resets, mass email) require a real human decision. Primary path: **MCP elicitation** — a client that declared the `elicitation` capability at `initialize` gets a session-lite `Mcp-Session-Id` (in-process, `core/mcp/sessions.py` — another reason `/mcp` must stay single-process), and its Tier-3 `tools/call` answers as an SSE stream carrying an `elicitation/create` Approve/Decline dialog the model never sees; the human's answer arrives as a JSON-RPC response POST routed by `core/mcp/elicitation.py`. Fallback (no elicitation): a `PendingAgentAction` row with Approve/Deny buttons in Course Settings → Pending agent actions — those endpoints refuse course-scoped credentials, so the agent can never approve its own action; the refusal hands the instructor a `CLIENT_URL` deep link (`?section=pending-agent-actions`). Approvals are single-use (conditional-UPDATE claim), expire in 10 minutes, die when the operation's plan hash changes, and a denial blocks re-requests until expiry.
- **Student privacy (FERPA)**: every `tools/call` payload — success or error — is scrubbed in `core/mcp/protocol.py:_scrub`, and the Tier-3 elicitation dialog in `guardrails._confirm_via_elicitation`, via `core/agent/privacy.py`: each roster student's email, email local part (NetID) and Django username become a stable per-course HMAC alias `student-<10 hex>` keyed off `FIELD_ENCRYPTION_KEY`; staff emails pass through. Always on, every credential. Tools that take a student go through `_common.resolve_student` / `unalias`, which accept an alias or an email. `GET /courses/{id}/agentAliases/?q=` resolves aliases for signed-in admins only (course-scoped credentials get 403). Regression net: `core/tests/test_agent_privacy.py`.
- **Tests**: `core/tests/test_mcp_protocol.py` (wire-format conformance — we own the protocol, these are the regression net), `core/tests/test_agent_dispatch.py` (header allowlist + scope postcondition).

## Integration Points

- **Frontend (codePost-ui)**: Consumes this API. Schema changes require regenerating the TS client via `./scripts/generate_ts_client.sh` which outputs to `../codePost-ui/src/api-client/`.
- **Python SDK (codepost-python)**: Generated from the same `schema.yaml` via `./scripts/generate_sdk.sh`.
- **TypeScript SDK (codePost/)**: Separate SDK package also consuming this schema.
- **Database**: MySQL in production (`DB_HOSTNAME` env var), SQLite in dev/test (no env var).
- **Redis**: Celery broker, Channels layer, worker shell relay. Optional in dev (falls back to local mode).
- **S3**: File storage via boto3 when `AWS_STORAGE_BUCKET_NAME` is set.
- **Docker**: Autograder spawns sandboxed containers for code execution.

## Security

- Sensitive fields use `django-encrypted-model-fields` (`EncryptedCharField`).
- Auth: `TokenAuthentication`, `JWTAuthentication` (SimpleJWT sliding tokens), `BasicAuthentication`, `SessionAuthentication`.
- `FIELD_ENCRYPTION_KEY` env var required for encrypted fields.
- `/impersonate/` is a production endpoint — staff/superusers can impersonate any user; course admins can only impersonate students/graders in their courses. It issues a fresh JWT and logs the event.
- `dev-auth/login-as/` is DEBUG-only — not available in production.
- All permission checks must happen in the permission class, not in the view body.
- `DEFAULT_PERMISSION_CLASSES` is `[IsAuthenticated]` — all endpoints require auth unless explicitly overridden.
