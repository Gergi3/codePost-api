# Copyright © 2026 Rutgers, the State University of New Jersey. All rights reserved except as defined by the Rutgers Non-Commercial License, included with this software.
"""Phase 3c: people & grading ops tools through /mcp."""
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
        return CourseFactory(name="cs801", period="f2026", organization__name="TestOrg")


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
    raw = resp.data["key"]
    api_client.credentials(HTTP_AUTHORIZATION=f"CourseKey {raw}")
    api_client.post(MCP_URL, {"jsonrpc": "2.0", "id": 0, "method": "ping"},
                    format="json")
    return raw


@pytest.fixture
def write_key(api_client, course, admin):
    return _mint(api_client, course, admin, "write", "w-key")


@pytest.fixture
def admin_key(api_client, course, admin):
    return _mint(api_client, course, admin, "admin", "a-key")


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


class TestRoster:

    def test_add_names_new_accounts_in_dry_run(self, api_client, write_key, course):
        result = call(api_client, write_key, "codepost_update_roster",
                      {"add": {"students": ["brandnew@student.edu"]}})
        assert result["isError"] is False
        plan = result["structuredContent"]["data"]["plan"]
        assert plan["newAccountsCreated"] == ["brandnew@student.edu"]

    def test_add_applies(self, api_client, write_key, course):
        result = call(api_client, write_key, "codepost_update_roster",
                      {"add": {"students": ["newkid@student.edu"]},
                       "dryRun": False})
        assert result["isError"] is False, result["content"][0]["text"]
        assert course.students.filter(email="newkid@student.edu").exists()

    def test_remove_needs_admin_scope(self, api_client, write_key, course):
        target = course.students.first()
        result = call(api_client, write_key, "codepost_update_roster",
                      {"remove": {"students": [target.email]}})
        assert result["isError"] is True
        assert error_of(result)["code"] == "INSUFFICIENT_KEY_SCOPE"

    def test_remove_with_admin_key_and_token(self, api_client, admin_key, course):
        target = course.students.first()
        first = call(api_client, admin_key, "codepost_update_roster",
                     {"remove": {"students": [target.email]}})
        assert error_of(first)["code"] == "CONFIRMATION_REQUIRED"
        token = error_of(first)["context"]["confirmToken"]

        second = call(api_client, admin_key, "codepost_update_roster",
                      {"remove": {"students": [target.email]},
                       "dryRun": False, "confirmToken": token})
        assert second["isError"] is False, second["content"][0]["text"]
        assert not course.students.filter(pk=target.pk).exists()
        # Deactivated, not deleted
        assert course.inactive_students.filter(pk=target.pk).exists()

    def test_cannot_remove_last_admin(self, api_client, admin_key, course, admin):
        # The factory seeds several admins; removing ALL of them is the case
        # that must be refused.
        all_admins = [a.email for a in course.courseAdmins.all()]
        result = call(api_client, admin_key, "codepost_update_roster",
                      {"remove": {"courseAdmins": all_admins}})
        assert result["isError"] is True
        assert "no courseAdmin" in error_of(result)["message"]


class TestSections:

    def test_create_and_set_members_warns_about_moves(
            self, api_client, write_key, course):
        from core.models import Section
        s1_result = call(api_client, write_key, "codepost_manage_sections",
                         {"op": "create", "name": "Tue 10AM", "dryRun": False})
        assert s1_result["isError"] is False
        s1 = s1_result["structuredContent"]["data"]["section"]

        student = course.students.first()
        call(api_client, write_key, "codepost_manage_sections",
             {"op": "setMembers", "sectionId": s1["id"],
              "students": [student.email], "dryRun": False})

        s2_result = call(api_client, write_key, "codepost_manage_sections",
                         {"op": "create", "name": "Thu 2PM", "dryRun": False})
        s2 = s2_result["structuredContent"]["data"]["section"]

        preview = call(api_client, write_key, "codepost_manage_sections",
                       {"op": "setMembers", "sectionId": s2["id"],
                        "students": [student.email]})
        warnings = preview["structuredContent"].get("warnings") or []
        assert any("MOVE" in w for w in warnings)

        call(api_client, write_key, "codepost_manage_sections",
             {"op": "setMembers", "sectionId": s2["id"],
              "students": [student.email], "dryRun": False})
        assert not Section.objects.get(pk=s1["id"]).students.filter(
            pk=student.pk).exists()
        assert Section.objects.get(pk=s2["id"]).students.filter(
            pk=student.pk).exists()


class TestGradingOps:

    @pytest.fixture
    def assignment_with_unclaimed(self, course):
        from core.models import Assignment, Submission
        with factory.django.mute_signals(post_save):
            a = Assignment.objects.create(course=course, name="HWD", points=10,
                                          state="published")
            for student in course.students.all():
                sub = Submission.objects.create(assignment=a)
                sub.students.set([student])
        return a

    def test_distribute_previews_shares(self, api_client, write_key, course,
                                        assignment_with_unclaimed):
        graders = [g.email for g in course.graders.all()[:2]]
        result = call(api_client, write_key, "codepost_update_submission_grading",
                      {"op": "distribute",
                       "assignmentId": assignment_with_unclaimed.id,
                       "graders": graders})
        assert result["isError"] is False, result["content"][0]["text"]
        plan = result["structuredContent"]["data"]["plan"]
        expected = course.students.count()
        assert plan["unclaimed"] == expected
        assert sum(plan["perGrader"].values()) == expected

    def test_distribute_applies(self, api_client, write_key, course,
                                assignment_with_unclaimed):
        from core.models import Submission
        graders = [g.email for g in course.graders.all()[:2]]
        result = call(api_client, write_key, "codepost_update_submission_grading",
                      {"op": "distribute",
                       "assignmentId": assignment_with_unclaimed.id,
                       "graders": graders, "dryRun": False})
        assert result["isError"] is False, result["content"][0]["text"]
        assert not Submission.objects.filter(
            assignment=assignment_with_unclaimed, grader=None).exists()

    def test_assign_dry_run_warns_on_unfinalize(self, api_client, write_key,
                                                assignment_with_unclaimed):
        sub_id = assignment_with_unclaimed.submissions.first().id
        result = call(api_client, write_key, "codepost_update_submission_grading",
                      {"op": "assign", "submissionIds": [sub_id],
                       "isFinalized": False})
        warnings = result["structuredContent"].get("warnings") or []
        assert any("per-student" in w for w in warnings)

    def test_unassign_releases_claimed_submissions(self, api_client, write_key,
                                                   course, assignment_with_unclaimed):
        from core.models import Submission
        grader = course.graders.first()
        Submission.objects.filter(assignment=assignment_with_unclaimed).update(grader=grader)
        ids = list(Submission.objects.filter(
            assignment=assignment_with_unclaimed).values_list("id", flat=True))

        result = call(api_client, write_key, "codepost_update_submission_grading",
                      {"op": "unassign", "submissionIds": ids, "dryRun": False})
        assert result["isError"] is False, result["content"][0]["text"]
        data = result["structuredContent"]["data"]
        assert sorted(data["applied"]) == sorted(ids)
        assert data["failures"] == []
        assert not Submission.objects.filter(
            assignment=assignment_with_unclaimed, grader__isnull=False).exists()

    def test_unassign_refuses_finalized_submissions(self, api_client, write_key,
                                                    course, assignment_with_unclaimed):
        from core.models import Submission
        grader = course.graders.first()
        finalized, claimed = Submission.objects.filter(
            assignment=assignment_with_unclaimed)[:2]
        Submission.objects.filter(id=finalized.id).update(
            grader=grader, isFinalized=True, grade=10)
        Submission.objects.filter(id=claimed.id).update(grader=grader)

        result = call(api_client, write_key, "codepost_update_submission_grading",
                      {"op": "unassign", "submissionIds": [finalized.id, claimed.id],
                       "dryRun": False})
        assert result["isError"] is False, result["content"][0]["text"]
        data = result["structuredContent"]["data"]
        assert data["applied"] == [claimed.id]
        assert [f["submissionId"] for f in data["failures"]] == [finalized.id]
        finalized.refresh_from_db()
        assert finalized.grader == grader


class TestEditRubric:

    def test_batch_create_category_and_comment(self, api_client, write_key, course):
        from core.models import Assignment, RubricCategory, RubricComment
        with factory.django.mute_signals(post_save):
            a = Assignment.objects.create(course=course, name="HWR", points=10,
                                          state="draft")

        result = call(api_client, write_key, "codepost_edit_rubric",
                      {"assignmentId": a.id,
                       "categories": [{"op": "create", "name": "Style",
                                       "pointLimit": 5}]})
        assert result["isError"] is False, result["content"][0]["text"]
        report = result["structuredContent"]["data"]["report"]
        category_id = report["categories"][0]["id"]

        result2 = call(api_client, write_key, "codepost_edit_rubric",
                       {"assignmentId": a.id,
                        "comments": [{"op": "create", "categoryId": category_id,
                                      "text": "Missing docstring",
                                      "pointDelta": -1}]})
        assert result2["isError"] is False, result2["content"][0]["text"]
        assert RubricComment.objects.filter(
            category__assignment=a, text="Missing docstring").exists()
