# Copyright © 2026 Rutgers, the State University of New Jersey. All rights reserved except as defined by the Rutgers Non-Commercial License, included with this software.
"""Phase 3d: attention & accommodation tools, driven through /mcp."""
import json

import factory
import pytest
from django.db.models.signals import post_save
from rest_framework import status
from rest_framework.test import APIClient

MCP_URL = "/mcp"
V = "2025-06-18"


@pytest.fixture
def course(db):
    from core.tests.factories import CourseFactory
    with factory.django.mute_signals(post_save):
        return CourseFactory(name="cs601", period="f2026", organization__name="TestOrg")


@pytest.fixture
def admin(course):
    return course.courseAdmins.first()


@pytest.fixture
def api_client():
    return APIClient()


@pytest.fixture
def key(api_client, course, admin):
    api_client.force_authenticate(user=admin)
    resp = api_client.post(f"/courses/{course.id}/apiKeys/",
                           {"name": "attn-key", "scope": "write"}, format="json")
    assert resp.status_code == status.HTTP_201_CREATED, resp.data
    api_client.force_authenticate(user=None)
    return resp.data["key"]


def call(api_client, key, name, arguments=None):
    api_client.credentials(HTTP_AUTHORIZATION=f"CourseKey {key}",
                           HTTP_MCP_PROTOCOL_VERSION=V)
    resp = api_client.post(MCP_URL, {
        "jsonrpc": "2.0", "id": 1, "method": "tools/call",
        "params": {"name": name, "arguments": arguments or {}},
    }, format="json")
    assert resp.status_code == status.HTTP_200_OK, resp.data
    return resp.data["result"]


def error_of(result):
    return json.loads(result["content"][0]["text"])["error"]


@pytest.fixture
def regrade_submission(course):
    """A finalized submission with an open regrade request."""
    from django.utils import timezone
    from core.tests.factories import AssignmentFactory
    with factory.django.mute_signals(post_save):
        a = AssignmentFactory(course=course, state="published")
    sub = a.submissions.first()
    # The factory submission has no students; the submission serializer
    # rejects any PATCH on a student-less submission ("students list cannot
    # be empty"), so attach one like every real submission has.
    sub.students.set([course.students.first()])
    # Finalized implies a grader (the serializer enforces it on every PATCH),
    # so mirror real data.
    sub.grader = course.graders.first()
    sub.isFinalized = True
    sub.questionIsOpen = True
    sub.questionIsRegrade = True
    sub.questionText = "I think problem 2 was graded too harshly."
    sub.questionDate = timezone.now()
    with factory.django.mute_signals(post_save):
        sub.save()
    return sub


class TestCourseTodo:

    def test_quiet_course_says_so(self, api_client, key, course):
        result = call(api_client, key, "codepost_course_todo")
        assert result["isError"] is False
        data = result["structuredContent"]["data"]
        assert data["gradingDebt"] == [] or data["gradingDebt"]
        # All five sections always present
        for section in ("deadlines", "gradingDebt", "openRegrades",
                        "quizzesNeedingGrading", "brokenAutograderBuilds"):
            assert section in data

    def test_surfaces_grading_debt_and_regrades(self, api_client, key, course,
                                                regrade_submission):
        result = call(api_client, key, "codepost_course_todo")
        data = result["structuredContent"]["data"]
        open_totals = [r["open"] for r in data["openRegrades"]]
        assert sum(open_totals) >= 1
        # Every item carries a next-step hint
        assert all("hint" in r for r in data["openRegrades"])


class TestManageRegrades:

    def test_list_shows_question_text(self, api_client, key, regrade_submission):
        result = call(api_client, key, "codepost_manage_regrades",
                      {"op": "list",
                       "assignmentId": regrade_submission.assignment_id})
        rows = result["structuredContent"]["data"]["openRequests"]
        assert rows[0]["id"] == regrade_submission.id
        assert "harshly" in rows[0]["questionText"]

    def test_respond_draft_keeps_request_open_and_warns(
            self, api_client, key, regrade_submission):
        from core.models import Submission
        result = call(api_client, key, "codepost_manage_regrades",
                      {"op": "respond", "submissionId": regrade_submission.id,
                       "response": "Looking into it.", "close": False})
        assert result["isError"] is False, result["content"][0]["text"]
        assert result["structuredContent"]["data"]["studentCanSeeReply"] is False
        assert any("DRAFT" in w for w in result["structuredContent"]["warnings"])
        row = Submission.objects.get(pk=regrade_submission.id)
        assert row.questionIsOpen is True
        assert row.questionResponse == "Looking into it."

    def test_respond_close_publishes(self, api_client, key, regrade_submission):
        from core.models import Submission
        result = call(api_client, key, "codepost_manage_regrades",
                      {"op": "respond", "submissionId": regrade_submission.id,
                       "response": "Regraded: +2 points on problem 2.",
                       "close": True})
        assert result["isError"] is False
        assert result["structuredContent"]["data"]["studentCanSeeReply"] is True
        row = Submission.objects.get(pk=regrade_submission.id)
        assert row.questionIsOpen is False

    def test_list_submissions_regrade_status_filter(
            self, api_client, key, regrade_submission):
        result = call(api_client, key, "codepost_list_submissions",
                      {"assignmentId": regrade_submission.assignment_id,
                       "status": "regradeRequested"})
        rows = result["structuredContent"]["data"]["rows"]
        assert rows and rows[0]["id"] == regrade_submission.id
        assert "questionText" in rows[0]


class TestQuizAccommodations:

    def test_set_and_list(self, api_client, key, course):
        from core.agent.privacy import alias_for
        from core.models import QuizAccommodation
        student = course.students.first()
        alias = alias_for(course.id, student.email)
        result = call(api_client, key, "codepost_set_quiz_accommodation",
                      {"op": "set", "student": student.email,
                       "timeMultiplier": 1.5, "sebExempt": True})
        assert result["isError"] is False
        data = result["structuredContent"]["data"]
        assert data["revoked"] is False
        # The model never sees the address, only the course-stable alias.
        assert data["accommodation"]["student"] == alias

        row = QuizAccommodation.objects.get(course=course, student=student)
        assert float(row.timeMultiplier) == 1.5
        assert row.sebExempt is True

        listing = call(api_client, key, "codepost_set_quiz_accommodation",
                       {"op": "list"})
        rows = listing["structuredContent"]["data"]["accommodations"]
        assert any(r["student"] == alias for r in rows)

    def test_revoke_via_multiplier_one(self, api_client, key, course):
        from core.models import QuizAccommodation
        student = course.students.first()
        call(api_client, key, "codepost_set_quiz_accommodation",
             {"op": "set", "student": student.email, "timeMultiplier": 2})

        result = call(api_client, key, "codepost_set_quiz_accommodation",
                      {"op": "set", "student": student.email,
                       "timeMultiplier": 1, "sebExempt": False})
        assert result["structuredContent"]["data"]["revoked"] is True
        assert not QuizAccommodation.objects.filter(
            course=course, student=student).exists()

    def test_unknown_student_is_a_clean_error(self, api_client, key):
        result = call(api_client, key, "codepost_set_quiz_accommodation",
                      {"op": "set", "student": "ghost@nowhere.edu",
                       "timeMultiplier": 1.5})
        assert result["isError"] is True
        err = error_of(result)
        assert err["retryable"] is False or "student" in err["message"].lower()
