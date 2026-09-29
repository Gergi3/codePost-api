# Copyright © 2026 Rutgers, the State University of New Jersey. All rights reserved except as defined by the Rutgers Non-Commercial License, included with this software.
"""Per-request state shared by every tool in one MCP call."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from core.agent import errors
from core.agent.dispatch import Dispatcher
from core.agent.registry import SCOPE_ADMIN, SCOPE_READ, SCOPE_WRITE


@dataclass
class AgentContext:
    user: Any
    course: Any
    scope: str
    dispatch: Dispatcher
    # Set only for a Tier-3 call streaming over SSE from an elicitation-capable
    # client: a core.mcp.elicitation.Channel that can pop an Approve/Decline
    # dialog at the human mid-call. None everywhere else.
    elicit_channel: Any = None
    # Per-call caches. The roster is the only identity-resolution path under a
    # course key (/users/{email}/ can't be reached), and the assignment list
    # costs an N-way fan-out, so both are worth holding for the call's life.
    _roster: dict | None = field(default=None, repr=False)
    _assignments: dict[int, dict] | None = field(default=None, repr=False)
    _aliases: Any = field(default=None, repr=False)   # core.agent.privacy.StudentAliasMap

    def require_writable(self) -> None:
        """Pre-flight the archived check every write tool needs.

        Stricter than the API on purpose: `changeInviteCode`, `notifyStudents`
        and `resetAttempts` all bypass the serializer guard and would succeed
        on an archived course.
        """
        if self.course.archived:
            raise errors.course_archived(self.course)


class Connection:
    """One authenticated MCP request, before any course is chosen.

    Two flavours:
    - **pinned** — a course API key (or course-scoped JWT): ``pinned_course_id``
      is set, the course is fixed for every call, and tools carry no courseId.
    - **unpinned** — a personal instructor token: the course arrives per call as
      a ``courseId`` argument, validated against ``isCourseStaff`` each time.
    """

    def __init__(self, request):
        self.request = request
        self.user = request.user
        self.pinned_course_id = _get_scope_id(request)
        self.scope = _narrow(_resolve_scope(request), request)
        self._dispatch_meta = _dispatch_meta_for(request)

    @property
    def pinned(self) -> bool:
        return self.pinned_course_id is not None

    def context_for(self, course_id: int) -> AgentContext:
        """Build the per-call context, enforcing the boundary for this flavour."""
        from core.models import Course
        from core.permissions.helpers import isCourseStaff

        if self.pinned and int(course_id) != int(self.pinned_course_id):
            raise errors.not_in_scope(f'course {course_id}')

        try:
            course = Course.objects.select_related('organization').get(pk=course_id)
        except Course.DoesNotExist:
            raise errors.ToolError(
                'NOT_FOUND', f'Course {course_id} does not exist.',
                remedy='Call codepost_list_courses to see your courses.')

        # A pinned credential already proved course membership by authenticating;
        # an unpinned one must show the human behind the token staffs this course.
        if not self.pinned and not isCourseStaff(self.user, course):
            raise errors.ToolError(
                'NOT_IN_SCOPE',
                f'You are not a staff member of course {course_id}.',
                remedy='Call codepost_list_courses to see the courses you can manage.')

        return AgentContext(
            user=self.user,
            course=course,
            scope=self.scope,
            dispatch=Dispatcher(self._dispatch_meta, course_id=course_id),
            elicit_channel=getattr(self, 'elicit_channel', None),
        )

    def pinned_context(self) -> AgentContext:
        assert self.pinned
        return self.context_for(self.pinned_course_id)

    def courseless_context(self) -> AgentContext:
        """Context for the rare course_bound=False tool (codepost_list_courses)."""
        return AgentContext(
            user=self.user, course=None, scope=self.scope,
            dispatch=Dispatcher(self._dispatch_meta, course_id=None),
        )


def _dispatch_meta_for(request) -> dict:
    """The request META the in-process dispatcher replays into the viewsets.

    Normally the caller's own META (their Authorization header re-runs the real
    auth classes). OAuth Bearer tokens are the exception: they are opaque DOT
    tokens, and the internal viewsets' JWT auth class RAISES on a Bearer value
    it cannot decode — every dispatched call would 401. We deliberately do not
    add OAuth2 auth to the API-wide defaults (a scope-less OAuth token would
    then reach every REST endpoint directly), so instead the validated OAuth
    identity is exchanged at the edge for a short-lived internal JWT for the
    same user — from there the flow is identical to the personal-token path.
    """
    try:
        from oauth2_provider.models import AccessToken as OAuthAccessToken
    except ImportError:                                        # pragma: no cover
        return request.META

    if isinstance(getattr(request, 'auth', None), OAuthAccessToken):
        from core.views.auth import access_token_for_user
        internal = access_token_for_user(request.user)
        return {'HTTP_AUTHORIZATION': f'Bearer {internal}'}
    return request.META


def _get_scope_id(request):
    from core.permissions.course_scope import get_course_scope_id
    return get_course_scope_id(request)


def _resolve_scope(request) -> str:
    """The scope of the credential that authenticated this request.

    Course API keys carry an explicit scope, put there by
    ``CourseAPIKeyAuthentication``.  Anything else — a personal instructor
    token, a course-scoped JWT — is a real human's own credential, already
    bounded by their permissions on every dispatched call, so it gets full
    tool scope (tier guardrails still apply).
    """
    auth = getattr(request, 'auth', None)
    if isinstance(auth, dict) and 'scope' in auth:
        return auth['scope'] or SCOPE_READ

    # OAuth Bearer (django-oauth-toolkit): the instructor granted specific
    # scopes on the consent page — honour them. Highest granted wins; a token
    # with none of ours behaves as read. This branch must come before the
    # admin fallback or a read-scoped token would silently get admin.
    from oauth2_provider.models import AccessToken as OAuthAccessToken
    if isinstance(auth, OAuthAccessToken):
        granted = set((auth.scope or '').split())
        for name in (SCOPE_ADMIN, SCOPE_WRITE, SCOPE_READ):
            if name in granted:
                return name
        return SCOPE_READ

    return SCOPE_ADMIN


def _narrow(scope: str, request) -> str:
    """Apply the optional ``?scope=read|write`` self-limit from the connect URL.

    Narrowing only — a read key asking for ?scope=admin stays read.
    """
    from core.agent.registry import SCOPE_ORDER

    asked = request.query_params.get('scope') if hasattr(request, 'query_params') \
        else request.GET.get('scope')
    if asked in SCOPE_ORDER and SCOPE_ORDER[asked] < SCOPE_ORDER.get(scope, 0):
        return asked
    return scope
