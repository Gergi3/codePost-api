# Copyright © 2026 Rutgers, the State University of New Jersey. All rights reserved except as defined by the Rutgers Non-Commercial License, included with this software.
"""The in-chat Tier-3 approval path: session-lite + SSE elicitation.

The property under test: on an elicitation-capable client, a Tier-3 tool call
streams an ``elicitation/create`` request (the client's native Approve/Decline
dialog), blocks until the human's answer arrives as a separate POST, and only
an ``accept`` lets the destructive operation run — in the same call.

These tests use ``transactional_db``: the streaming view runs the tool in a
worker thread, whose own DB connection cannot see uncommitted TestCase
fixtures.
"""
import json

import factory
import pytest
from django.db.models.signals import post_save
from rest_framework import status
from rest_framework.test import APIClient

MCP_URL = "/mcp"
V = "2025-06-18"


@pytest.fixture
def course(transactional_db):
    from core.tests.factories import CourseFactory
    with factory.django.mute_signals(post_save):
        return CourseFactory(name="cs902", period="f2026", organization__name="TestOrg")


@pytest.fixture
def admin(course):
    return course.courseAdmins.first()


@pytest.fixture
def api_client():
    return APIClient()


@pytest.fixture
def admin_key(api_client, course, admin):
    api_client.force_authenticate(user=admin)
    resp = api_client.post(f"/courses/{course.id}/apiKeys/",
                           {"name": "elicit-key", "scope": "admin"}, format="json")
    assert resp.status_code == status.HTTP_201_CREATED, resp.data
    api_client.force_authenticate(user=None)
    return resp.data["key"]


@pytest.fixture
def empty_assignment(course):
    from core.models import Assignment
    with factory.django.mute_signals(post_save):
        return Assignment.objects.create(course=course, name="Disposable",
                                         points=10, state="draft")


def initialize(api_client, key, capabilities=None):
    api_client.credentials(HTTP_AUTHORIZATION=f"CourseKey {key}",
                           HTTP_MCP_PROTOCOL_VERSION=V)
    return api_client.post(MCP_URL, {
        "jsonrpc": "2.0", "id": 1, "method": "initialize",
        "params": {"protocolVersion": V,
                   "capabilities": capabilities if capabilities is not None else {},
                   "clientInfo": {"name": "test", "version": "0"}},
    }, format="json")


@pytest.fixture
def session_id(api_client, admin_key):
    resp = initialize(api_client, admin_key, {"elicitation": {}})
    assert resp.status_code == status.HTTP_200_OK
    return resp["Mcp-Session-Id"]


def start_delete(api_client, key, session, assignment_id):
    """POST the Tier-3 call; with an elicitation session this streams."""
    api_client.credentials(HTTP_AUTHORIZATION=f"CourseKey {key}",
                           HTTP_MCP_PROTOCOL_VERSION=V,
                           HTTP_MCP_SESSION_ID=session)
    return api_client.post(MCP_URL, {
        "jsonrpc": "2.0", "id": 7, "method": "tools/call",
        "params": {"name": "codepost_delete_resource",
                   "arguments": {"resourceType": "assignment",
                                 "resourceId": assignment_id}},
    }, format="json")


def parse_event(chunk) -> dict:
    text = chunk.decode() if isinstance(chunk, bytes) else chunk
    assert text.startswith("data: "), text
    return json.loads(text[len("data: "):].strip())


def answer(key, session, request_id, action):
    """The MCP client relaying the human's dialog answer — a separate POST."""
    client = APIClient()
    client.credentials(HTTP_AUTHORIZATION=f"CourseKey {key}",
                       HTTP_MCP_PROTOCOL_VERSION=V,
                       HTTP_MCP_SESSION_ID=session)
    resp = client.post(MCP_URL, {
        "jsonrpc": "2.0", "id": request_id,
        "result": {"action": action, "content": {}},
    }, format="json")
    assert resp.status_code == status.HTTP_202_ACCEPTED, resp.data
    return resp


class TestSessionLite:

    def test_elicitation_capability_gets_session(self, api_client, admin_key):
        resp = initialize(api_client, admin_key, {"elicitation": {}})
        assert resp.status_code == status.HTTP_200_OK
        assert resp["Mcp-Session-Id"]

    def test_plain_client_stays_stateless(self, api_client, admin_key):
        resp = initialize(api_client, admin_key, {})
        assert resp.status_code == status.HTTP_200_OK
        assert not resp.has_header("Mcp-Session-Id")

    def test_unmatched_response_post_gets_202(self, api_client, admin_key):
        api_client.credentials(HTTP_AUTHORIZATION=f"CourseKey {admin_key}",
                               HTTP_MCP_PROTOCOL_VERSION=V)
        resp = api_client.post(MCP_URL, {
            "jsonrpc": "2.0", "id": "elicit-nonexistent", "result": {}},
            format="json")
        assert resp.status_code == status.HTTP_202_ACCEPTED

    def test_non_tier3_call_stays_plain_json(self, api_client, admin_key,
                                             session_id, course):
        api_client.credentials(HTTP_AUTHORIZATION=f"CourseKey {admin_key}",
                               HTTP_MCP_PROTOCOL_VERSION=V,
                               HTTP_MCP_SESSION_ID=session_id)
        resp = api_client.post(MCP_URL, {
            "jsonrpc": "2.0", "id": 2, "method": "tools/call",
            "params": {"name": "codepost_get_course_overview", "arguments": {}},
        }, format="json")
        assert not resp.streaming
        assert resp["Content-Type"].startswith("application/json")


class TestElicitationFlow:

    def test_accept_executes_in_one_call(self, api_client, admin_key, session_id,
                                         course, empty_assignment):
        from core.models import Assignment, CourseAuditEvent

        resp = start_delete(api_client, admin_key, session_id,
                            empty_assignment.id)
        assert resp.streaming
        assert resp["Content-Type"].startswith("text/event-stream")

        stream = iter(resp.streaming_content)
        ask = parse_event(next(stream))
        assert ask["method"] == "elicitation/create"
        assert "Disposable" in ask["params"]["message"]
        assert "PERMANENTLY" in ask["params"]["message"]

        answer(admin_key, session_id, ask["id"], "accept")

        final = parse_event(list(stream)[-1])
        assert final["id"] == 7
        assert final["result"]["isError"] is False, final
        assert not Assignment.objects.filter(pk=empty_assignment.id).exists()
        # No dashboard row was ever involved; the decision is audited.
        event = CourseAuditEvent.objects.filter(
            course=course, event_type="agent_action_approved").first()
        assert event is not None and event.meta["origin"] == "elicitation"

    def test_decline_refuses_and_preserves(self, api_client, admin_key,
                                           session_id, course, empty_assignment):
        from core.models import Assignment, PendingAgentAction

        resp = start_delete(api_client, admin_key, session_id,
                            empty_assignment.id)
        stream = iter(resp.streaming_content)
        ask = parse_event(next(stream))
        answer(admin_key, session_id, ask["id"], "decline")

        final = parse_event(list(stream)[-1])
        assert final["result"]["isError"] is True
        err = json.loads(final["result"]["content"][0]["text"])["error"]
        assert err["code"] == "CONFIRMATION_DENIED"
        assert err["retryable"] is False
        assert Assignment.objects.filter(pk=empty_assignment.id).exists()
        assert not PendingAgentAction.objects.filter(course=course).exists()

    def test_timeout_reports_and_preserves(self, settings, api_client, admin_key,
                                           session_id, empty_assignment):
        from core.models import Assignment

        settings.MCP_ELICIT_TIMEOUT_SECONDS = 0.3
        resp = start_delete(api_client, admin_key, session_id,
                            empty_assignment.id)
        events = [parse_event(c) for c in resp.streaming_content]
        assert events[0]["method"] == "elicitation/create"
        final = events[-1]
        assert final["result"]["isError"] is True
        err = json.loads(final["result"]["content"][0]["text"])["error"]
        assert err["code"] == "CONFIRMATION_REQUIRED"
        assert "timed out" in err["message"]
        assert Assignment.objects.filter(pk=empty_assignment.id).exists()


class TestElicitationPrivacy:
    """The approval dialog crosses the MCP client, so it gets the same
    student scrub as a tool result."""

    @pytest.fixture
    def released_assignment(self, course):
        from django.contrib.auth.models import User
        from core.models import Assignment, Profile, Submission
        with factory.django.mute_signals(post_save):
            student = User.objects.create(username="zq7712",
                                          email="zq7712@rutgers.edu")
            Profile.objects.get_or_create(user=student)
            course.students.add(student)
            a = Assignment.objects.create(course=course, name="Released",
                                          points=10, state="published",
                                          feedbackStatus="released")
            sub = Submission.objects.create(assignment=a, isFinalized=True,
                                            grader=course.graders.first())
            sub.students.set([student])
        return a

    def test_dialog_and_result_carry_aliases_not_emails(
            self, api_client, admin_key, session_id, course, released_assignment):
        from core.agent.privacy import alias_for

        api_client.credentials(HTTP_AUTHORIZATION=f"CourseKey {admin_key}",
                               HTTP_MCP_PROTOCOL_VERSION=V,
                               HTTP_MCP_SESSION_ID=session_id)
        resp = api_client.post(MCP_URL, {
            "jsonrpc": "2.0", "id": 9, "method": "tools/call",
            "params": {"name": "codepost_notify_students_feedback_ready",
                       "arguments": {"assignmentId": released_assignment.id}},
        }, format="json")
        assert resp.streaming
        stream = iter(resp.streaming_content)
        ask = parse_event(next(stream))
        assert ask["method"] == "elicitation/create"
        message = ask["params"]["message"]
        assert alias_for(course.id, "zq7712@rutgers.edu") in message
        assert "zq7712" not in message.lower()

        answer(admin_key, session_id, ask["id"], "decline")
        final = parse_event(list(stream)[-1])
        assert "zq7712" not in json.dumps(final).lower()
