# Copyright © 2026 Rutgers, the State University of New Jersey. All rights reserved except as defined by the Rutgers Non-Commercial License, included with this software.
"""Student pseudonymization at the MCP boundary.

The property under test: no roster student email, NetID (email local part)
or username ever reaches an MCP client — not in a tool result, an error
payload, a Tier-2 plan, or a Tier-3 elicitation dialog — while every tool
that takes a student still works when handed the alias instead.
"""
import json
import re

import factory
import pytest
from django.db.models.signals import post_save
from rest_framework import status
from rest_framework.test import APIClient

MCP_URL = "/mcp"
V = "2025-06-18"

# Distinctive identities: a Rutgers-style NetID, a mixed-case address whose
# Django username differs from its local part, a plus-tagged address.
NETID = "zq7712"
STUDENTS = [
    (f"{NETID}@rutgers.edu", NETID),
    ("Mixed.Case@Uni.Example", "mixedcase_user"),
    ("plus+tag@uni.example", "plus+tag@uni.example"),
]
DROPPED = ("dropped99@uni.example", "dropped99")
INACTIVE = ("gone42@uni.example", "gone42")


@pytest.fixture
def course(db):
    from core.tests.factories import CourseFactory
    with factory.django.mute_signals(post_save):
        return CourseFactory(name="cs701", period="f2026", organization__name="TestOrg")


@pytest.fixture
def admin(course):
    return course.courseAdmins.first()


@pytest.fixture
def api_client():
    return APIClient()


def _mint(api_client, course, admin, scope, name):
    api_client.force_authenticate(user=admin)
    resp = api_client.post(f"/courses/{course.id}/apiKeys/",
                           {"name": name, "scope": scope}, format="json")
    assert resp.status_code == status.HTTP_201_CREATED, resp.data
    api_client.force_authenticate(user=None)
    return resp.data["key"]


@pytest.fixture
def admin_key(api_client, course, admin):
    return _mint(api_client, course, admin, "admin", "priv-admin")


@pytest.fixture
def write_key(api_client, course, admin):
    return _mint(api_client, course, admin, "write", "priv-write")


def _user(email, username):
    from django.contrib.auth.models import User
    from core.models import Profile
    with factory.django.mute_signals(post_save):
        u = User.objects.create(username=username, email=email)
        Profile.objects.get_or_create(user=u)
    return u


@pytest.fixture
def people(course):
    """Adds the distinctive students; returns {email: user}."""
    out = {}
    for email, username in STUDENTS:
        u = _user(email, username)
        course.students.add(u)
        out[email] = u
    inactive = _user(*INACTIVE)
    course.inactive_students.add(inactive)
    out[INACTIVE[0]] = inactive
    out[DROPPED[0]] = _user(*DROPPED)       # on a submission only (below)
    return out


@pytest.fixture
def assignment(course, people):
    """Published, feedback released; one submission per distinctive student,
    one of them finalized with an open regrade whose text names classmates."""
    from django.utils import timezone
    from core.models import Submission
    from core.tests.factories import AssignmentFactory
    with factory.django.mute_signals(post_save):
        a = AssignmentFactory(course=course, name="HW1", points=100,
                              state="published", feedbackStatus="released")
        first = a.submissions.first()
        first.students.set([people[STUDENTS[0][0]]])
        first.grader = course.graders.first()
        first.isFinalized = True
        first.grade = 88
        first.questionIsOpen = True
        first.questionIsRegrade = True
        first.questionDate = timezone.now()
        first.questionText = (
            f"Please compare with {STUDENTS[1][0]}'s grade; my file was "
            f"{NETID}_hw1.py and I worked with {NETID}.")
        first.save()
        for email, _ in STUDENTS[1:]:
            s = Submission.objects.create(assignment=a)
            s.students.set([people[email]])
        dropped = Submission.objects.create(assignment=a)
        dropped.students.set([people[DROPPED[0]]])
    return a


@pytest.fixture
def audit_rows(course, assignment, people):
    from core.services.audit import record_audit_event
    student = people[STUDENTS[0][0]]
    record_audit_event(course, "regrade_request", user=student,
                       assignment=assignment,
                       submission=assignment.submissions.first(),
                       meta={"questionText": f"from {NETID}", "isRegrade": True,
                             "student": student.email})
    record_audit_event(course, "feedback_view", user=people[STUDENTS[1][0]],
                       assignment=assignment)


@pytest.fixture
def quiz(course, people):
    from core.models import Quiz, QuizAttempt
    q = Quiz.objects.create(course=course, title="Q1", isPublished=True)
    QuizAttempt.objects.create(quiz=q, student=people[STUDENTS[0][0]],
                               attemptNumber=1, status="submitted",
                               needsManualGrading=True)
    return q


@pytest.fixture
def accommodation(course, people):
    from core.models import QuizAccommodation
    return QuizAccommodation.objects.create(
        course=course, student=people[STUDENTS[2][0]], timeMultiplier=1.5)


def call(api_client, key, name, arguments=None, *, initialize=False):
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


def alias(course, email):
    from core.agent.privacy import alias_for
    return alias_for(course.id, email)


ALL_EMAILS = [e for e, _ in STUDENTS] + [DROPPED[0], INACTIVE[0]]
ALL_TOKENS = [NETID, "mixed.case", "mixedcase_user", "plus+tag",
              DROPPED[1], INACTIVE[1]]


def assert_no_student_identifier(result):
    """Every surface a client reads: the text block and structuredContent."""
    blobs = [result["content"][0]["text"].lower()]
    if "structuredContent" in result:
        blobs.append(json.dumps(result["structuredContent"], default=str).lower())
    for blob in blobs:
        for email in ALL_EMAILS:
            assert email.lower() not in blob, (email, blob)
        for token in ALL_TOKENS:
            pattern = r"(?<![a-z0-9])" + re.escape(token.lower()) + r"(?![a-z0-9])"
            assert not re.search(pattern, blob), (token, blob)


# ---------------------------------------------------------------------------
# The unit behind everything
# ---------------------------------------------------------------------------

class TestStudentAliasMap:

    def test_alias_is_stable_per_course_and_case_insensitive(self):
        from core.agent.privacy import alias_for, is_alias
        a = alias_for(7, "Mixed.Case@Uni.Example")
        assert a == alias_for(7, " mixed.case@uni.example ")
        assert a != alias_for(8, "mixed.case@uni.example")
        assert is_alias(a) and is_alias(a.upper())
        assert not is_alias("student-xyz") and not is_alias("zq7712@rutgers.edu")

    def test_scrub_walks_keys_nested_text_and_identifiers(self):
        from core.agent.privacy import StudentAliasMap, alias_for
        m = StudentAliasMap(7, [("zq7712@rutgers.edu", "zq7712"),
                                ("john@x.edu", "jsmith")])
        zq = alias_for(7, "zq7712@rutgers.edu")
        john = alias_for(7, "john@x.edu")
        out = m.scrub({
            "zq7712@rutgers.edu": 3,                       # dict key
            "rows": [{"student": "ZQ7712@rutgers.edu"},  # whole-string, cased
                     {"note": "see zq7712_hw1.py and JSMITH; johnson stays"}],
            "n": 5, "flag": None,
            "already": f"{zq} is fine",
            "outsider": "nobody@else.edu wrote about johnny",
        })
        assert out[zq] == 3
        assert out["rows"][0]["student"] == zq
        assert out["rows"][1]["note"] == f"see {zq}_hw1.py and {john}; johnson stays"
        assert out["n"] == 5 and out["flag"] is None
        assert out["already"] == f"{zq} is fine"
        assert out["outsider"] == "nobody@else.edu wrote about johnny"

    def test_unalias(self):
        from core.agent.privacy import StudentAliasMap, alias_for
        m = StudentAliasMap(7, [("zq7712@rutgers.edu", "zq7712")])
        assert m.unalias(alias_for(7, "zq7712@rutgers.edu")) == "zq7712@rutgers.edu"
        assert m.unalias("typed@by.instructor") == "typed@by.instructor"
        assert m.unalias(alias_for(7, "unknown@x.edu")) is None

    def test_for_course_covers_active_inactive_and_dropped(self, course, people,
                                                          assignment):
        from core.agent.privacy import StudentAliasMap
        m = StudentAliasMap.for_course(course)
        for email in ALL_EMAILS:
            assert email.lower() in m.by_email
        assert NETID in m.by_token and "mixedcase_user" in m.by_token
        # Staff are never aliased.
        assert course.graders.first().email.lower() not in m.by_email


# ---------------------------------------------------------------------------
# Every surface, swept
# ---------------------------------------------------------------------------

class TestSweep:

    @pytest.mark.parametrize("tool,args", [
        ("codepost_get_roster", {"view": "emails", "roles": [
            "students", "graders", "courseAdmins", "superGraders",
            "rubricEditors", "quizGraders", "inactiveStudents", "notActivated"]}),
        ("codepost_get_roster", {"view": "emails", "search": NETID}),
        ("codepost_get_gradebook", {"view": "summary"}),
        ("codepost_get_gradebook", {"view": "rows", "limit": 100}),
        ("codepost_get_audit_log", {}),
        ("codepost_get_audit_log", {"groupBy": "user"}),
        ("codepost_set_quiz_accommodation", {"op": "list"}),
        ("codepost_course_todo", {}),
    ])
    def test_course_level_tools(self, api_client, admin_key, course, people,
                                assignment, audit_rows, quiz, accommodation,
                                tool, args):
        result = call(api_client, admin_key, tool, args)
        assert result["isError"] is False, result["content"][0]["text"]
        assert_no_student_identifier(result)

    @pytest.mark.parametrize("status_,extra", [
        ("all", {"fields": ["id", "students", "grader", "isFinalized", "grade",
                            "dateUploaded", "isLate", "queueOrderKey"]}),
        ("missing", {}),
        ("regradeRequested", {}),
    ])
    def test_list_submissions(self, api_client, admin_key, course, assignment,
                              status_, extra):
        result = call(api_client, admin_key, "codepost_list_submissions",
                      {"assignmentId": assignment.id, "status": status_, **extra})
        assert result["isError"] is False, result["content"][0]["text"]
        assert_no_student_identifier(result)
        rows = result["structuredContent"]["data"]["rows"]
        assert rows, "fixture should produce rows"
        if status_ == "regradeRequested":
            # Free text is kept, but the classmates named in it are aliased.
            text = rows[0]["questionText"]
            assert alias(course, STUDENTS[1][0]) in text
            assert f"{alias(course, STUDENTS[0][0])}_hw1.py" in text

    def test_quiz_results(self, api_client, admin_key, course, quiz):
        result = call(api_client, admin_key, "codepost_get_quiz_status",
                      {"view": "results", "quizId": quiz.id})
        assert result["isError"] is False, result["content"][0]["text"]
        rows = result["structuredContent"]["data"]["results"]
        assert [r["student"] for r in rows] == [alias(course, STUDENTS[0][0])]
        assert_no_student_identifier(result)

    def test_get_submission_with_history(self, api_client, admin_key, course,
                                         assignment, people):
        from core.models import SubmissionHistory
        sub = assignment.submissions.first()
        SubmissionHistory.objects.get_or_create(submission=sub,
                                                student=people[STUDENTS[0][0]])
        result = call(api_client, admin_key, "codepost_get_submission",
                      {"submissionId": sub.id, "include": ["history", "tests"]})
        assert result["isError"] is False, result["content"][0]["text"]
        assert_no_student_identifier(result)
        data = result["structuredContent"]["data"]
        assert data["submission"]["students"] == [alias(course, STUDENTS[0][0])]

    def test_manage_regrades_list(self, api_client, admin_key, assignment):
        result = call(api_client, admin_key, "codepost_manage_regrades",
                      {"op": "list", "assignmentId": assignment.id})
        assert result["isError"] is False
        assert_no_student_identifier(result)

    def test_manage_sections_preview_and_apply(self, api_client, admin_key,
                                               course, people):
        section = course.sections.first()
        for dry in (True, False):
            result = call(api_client, admin_key, "codepost_manage_sections",
                          {"op": "setMembers", "sectionId": section.id,
                           "students": [STUDENTS[0][0]], "dryRun": dry})
            assert result["isError"] is False, result["content"][0]["text"]
            assert_no_student_identifier(result)
        assert section.students.filter(email=STUDENTS[0][0]).exists()

    def test_update_roster_add_and_remove_preview(self, api_client, admin_key,
                                                  course, people):
        result = call(api_client, admin_key, "codepost_update_roster",
                      {"add": {"students": ["newkid@uni.example"]}, "dryRun": False})
        assert result["isError"] is False, result["content"][0]["text"]
        # Added in this very call, and already aliased on the way out.
        assert "newkid" not in result["content"][0]["text"].lower()
        assert course.students.filter(email="newkid@uni.example").exists()

        preview = call(api_client, admin_key, "codepost_update_roster",
                       {"remove": {"students": [STUDENTS[0][0]]}})
        err = error_of(preview)
        assert err["code"] == "CONFIRMATION_REQUIRED"
        assert err["context"]["plan"]["remove"]["students"] == \
            [alias(course, STUDENTS[0][0])]
        assert_no_student_identifier(preview)

    def test_notify_dashboard_plan(self, api_client, admin_key, course, assignment):
        result = call(api_client, admin_key,
                      "codepost_notify_students_feedback_ready",
                      {"assignmentId": assignment.id})
        err = error_of(result)
        assert err["code"] == "CONFIRMATION_REQUIRED"
        assert err["context"]["plan"]["sampleRecipients"] == \
            [alias(course, STUDENTS[0][0])]
        assert_no_student_identifier(result)

    def test_unknown_student_candidates_are_aliases(self, api_client, admin_key,
                                                    course, assignment):
        result = call(api_client, admin_key, "codepost_list_submissions",
                      {"assignmentId": assignment.id, "student": "zq"})
        err = error_of(result)
        assert err["code"] == "UNKNOWN_STUDENT"
        assert err["context"]["candidates"] == [alias(course, STUDENTS[0][0])]
        assert_no_student_identifier(result)

        bogus = call(api_client, admin_key, "codepost_list_submissions",
                     {"assignmentId": assignment.id,
                      "student": alias(course, "nobody@nowhere.edu")})
        assert error_of(bogus)["code"] == "UNKNOWN_STUDENT"

    def test_staff_emails_are_untouched(self, api_client, admin_key, course,
                                        people):
        result = call(api_client, admin_key, "codepost_get_roster",
                      {"view": "emails", "roles": ["graders", "courseAdmins"]})
        members = {m["email"] for m in result["structuredContent"]["data"]["members"]}
        assert course.graders.first().email in members
        assert course.courseAdmins.first().email in members

    def test_write_key_gets_same_treatment(self, api_client, write_key, course,
                                           people, assignment):
        result = call(api_client, write_key, "codepost_list_submissions",
                      {"assignmentId": assignment.id})
        assert result["isError"] is False
        assert_no_student_identifier(result)


# ---------------------------------------------------------------------------
# Aliases work as input
# ---------------------------------------------------------------------------

class TestRoundTrip:

    def test_alias_is_stable_and_accepted_everywhere(self, api_client, admin_key,
                                                     course, people, assignment,
                                                     audit_rows):
        from core.models import QuizAccommodation
        email = STUDENTS[0][0]
        first = call(api_client, admin_key, "codepost_get_roster",
                     {"view": "emails", "search": NETID})
        members = first["structuredContent"]["data"]["members"]
        assert members == [{"email": alias(course, email), "role": "students"}]
        second = call(api_client, admin_key, "codepost_get_roster",
                      {"view": "emails", "search": NETID})
        assert second["structuredContent"]["data"]["members"] == members
        a = members[0]["email"]

        for ident in (a, email, email.upper()):
            rows = call(api_client, admin_key, "codepost_list_submissions",
                        {"assignmentId": assignment.id, "student": ident}
                        )["structuredContent"]["data"]["rows"]
            assert [r["students"] for r in rows] == [[a]]

        gb = call(api_client, admin_key, "codepost_get_gradebook", {"student": a})
        rows = gb["structuredContent"]["data"]["rows"]
        assert len(rows) == 1 and rows[0]["student"] == a

        log = call(api_client, admin_key, "codepost_get_audit_log", {"student": a})
        events = log["structuredContent"]["data"]["events"]
        assert events and all(e["userEmail"] == a for e in events)

        acc = call(api_client, admin_key, "codepost_set_quiz_accommodation",
                   {"op": "set", "student": a, "timeMultiplier": 2})
        assert acc["structuredContent"]["data"]["student"] == a
        assert QuizAccommodation.objects.filter(
            course=course, student=people[email]).exists()

    def test_alias_in_section_and_roster_writes(self, api_client, admin_key,
                                                course, people):
        email = STUDENTS[1][0]
        a = alias(course, email)
        section = course.sections.first()
        res = call(api_client, admin_key, "codepost_manage_sections",
                   {"op": "setMembers", "sectionId": section.id,
                    "students": [a], "dryRun": False})
        assert res["isError"] is False, res["content"][0]["text"]
        assert section.students.filter(email=email).exists()

        preview = error_of(call(api_client, admin_key, "codepost_update_roster",
                                {"remove": {"students": [a]}}))
        token = preview["context"]["confirmToken"]
        # The confirm call sends the alias too; the token still matches.
        applied = call(api_client, admin_key, "codepost_update_roster",
                       {"remove": {"students": [a]}, "dryRun": False,
                        "confirmToken": token})
        assert applied["isError"] is False, applied["content"][0]["text"]
        assert not course.students.filter(email=email).exists()
        assert course.inactive_students.filter(email=email).exists()

    def test_list_submissions_cursor_never_carries_an_email(
            self, api_client, admin_key, course, assignment):
        from core.agent import shaping
        result = call(api_client, admin_key, "codepost_list_submissions",
                      {"assignmentId": assignment.id, "student": STUDENTS[0][0],
                       "limit": 1})
        meta = result["structuredContent"]["meta"]
        if meta.get("cursor"):
            payload = shaping.decode_cursor(meta["cursor"])
            assert payload["student"] == alias(course, STUDENTS[0][0])


# ---------------------------------------------------------------------------
# The instructor's way back
# ---------------------------------------------------------------------------

class TestLookupEndpoint:

    def test_admin_resolves_alias_email_and_netid(self, api_client, course,
                                                  admin, people):
        email = STUDENTS[0][0]
        a = alias(course, email)
        api_client.force_authenticate(user=admin)
        for q in (a, a.upper(), email, NETID, NETID.upper()):
            resp = api_client.get(f"/courses/{course.id}/agentAliases/", {"q": q})
            assert resp.status_code == status.HTTP_200_OK, (q, resp.data)
            assert resp.data["matches"] == [
                {"alias": a, "email": email, "username": NETID, "active": True}]

        resp = api_client.get(f"/courses/{course.id}/agentAliases/",
                              {"q": "mixedcase_user"})
        assert resp.data["matches"][0]["email"] == STUDENTS[1][0]

        resp = api_client.get(f"/courses/{course.id}/agentAliases/",
                              {"q": INACTIVE[0]})
        assert resp.data["matches"][0]["active"] is False

        resp = api_client.get(f"/courses/{course.id}/agentAliases/",
                              {"q": alias(course, "nobody@nowhere.edu")})
        assert resp.data["matches"] == []

        resp = api_client.get(f"/courses/{course.id}/agentAliases/")
        assert resp.status_code == status.HTTP_400_BAD_REQUEST

    def test_course_key_and_grader_are_refused(self, api_client, course, admin,
                                               admin_key, people):
        a = alias(course, STUDENTS[0][0])
        api_client.credentials(HTTP_AUTHORIZATION=f"CourseKey {admin_key}")
        resp = api_client.get(f"/courses/{course.id}/agentAliases/", {"q": a})
        assert resp.status_code == status.HTTP_403_FORBIDDEN

        api_client.credentials()
        api_client.force_authenticate(user=course.graders.first())
        resp = api_client.get(f"/courses/{course.id}/agentAliases/", {"q": a})
        assert resp.status_code == status.HTTP_403_FORBIDDEN


# ---------------------------------------------------------------------------
# The server tells the model
# ---------------------------------------------------------------------------

class TestInstructions:

    def test_initialize_explains_aliases(self, api_client, admin_key):
        api_client.credentials(HTTP_AUTHORIZATION=f"CourseKey {admin_key}",
                               HTTP_MCP_PROTOCOL_VERSION=V)
        resp = api_client.post(MCP_URL, {
            "jsonrpc": "2.0", "id": 1, "method": "initialize",
            "params": {"protocolVersion": V, "capabilities": {},
                       "clientInfo": {"name": "t", "version": "0"}},
        }, format="json")
        assert "student-" in resp.data["result"]["instructions"]
