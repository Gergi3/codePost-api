# Copyright © 2026 Rutgers, the State University of New Jersey. All rights reserved except as defined by the Rutgers Non-Commercial License, included with this software.
"""
Confirms the role-based visibility contract for hidden tests:

- Course admins (instructors) see hidden tests in the assignment test-case listing
  and full details in submissionTestResults.
- Graders see hidden tests in the assignment test-case listing and full details in
  submissionTestResults.
- Students never see individual hidden tests. In submissionTestResults they receive
  one synthetic per-category 'Hidden tests' summary that reports the pass count and
  point impact without leaking names, descriptions, or logs.
"""
from decimal import Decimal

from django.contrib.auth.models import User
from rest_framework.test import APITestCase

from core.models import (
    Assignment,
    Course,
    Organization,
    Submission,
    SubmissionTest,
    TestCase as AutograderTestCase,
    TestCategory,
    TestCategoryFile,
)


class HiddenTestVisibilityTests(APITestCase):
    @classmethod
    def setUpTestData(cls):
        cls.org = Organization.objects.create(name="Org", shortname="org")

        cls.instructor = User.objects.create_user("inst@org.edu", "inst@org.edu", "pw")
        cls.instructor.profile.organization = cls.org
        cls.instructor.save()

        cls.grader = User.objects.create_user("grader@org.edu", "grader@org.edu", "pw")
        cls.grader.profile.organization = cls.org
        cls.grader.save()

        cls.student = User.objects.create_user("student@org.edu", "student@org.edu", "pw")
        cls.student.profile.organization = cls.org
        cls.student.save()

        cls.course = Course.objects.create(name="cs101", period="s2026", organization=cls.org)
        cls.course.courseAdmins.add(cls.instructor)
        cls.course.graders.add(cls.grader)
        cls.course.students.add(cls.student)

        cls.assignment = Assignment.objects.create(
            course=cls.course,
            name="A1",
            points=20,
            state='published',
        )

        cls.category = TestCategory.objects.create(
            name="Cat",
            assignment=cls.assignment,
            testScript="SECRET_TEST_SOURCE: def test_visible(): assert solve() == 42",
        )

        # A multi-file test harness lives on the category too; its content must never
        # reach a student.
        TestCategoryFile.objects.create(
            category=cls.category,
            name="Helper.java",
            content="SECRET_TEST_FILE_CONTENT class Helper {}",
            sortKey=0,
        )

        cls.visible_test = AutograderTestCase.objects.create(
            testCategory=cls.category,
            functionName="test_visible",
            description="Visible test",
            pointsPass=2,
            hidden=False,
            exposed=True,
        )
        cls.hidden_pass = AutograderTestCase.objects.create(
            testCategory=cls.category,
            functionName="test_hidden_pass",
            description="Secret hidden test (passes)",
            explanation="Internal explanation that students must not see",
            pointsPass=3,
            hidden=True,
            exposed=True,
        )
        cls.hidden_fail = AutograderTestCase.objects.create(
            testCategory=cls.category,
            functionName="test_hidden_fail",
            description="Another hidden test (fails)",
            explanation="More internal-only text",
            pointsPass=2,
            hidden=True,
            exposed=True,
        )

        cls.submission = Submission.objects.create(
            assignment=cls.assignment,
            isFinalized=True,
            grader=cls.grader,
        )
        cls.submission.students.add(cls.student)

        SubmissionTest.objects.create(
            submission=cls.submission,
            testCase=cls.visible_test,
            logs="visible logs",
            passed=True,
            score=Decimal("2"),
            maxScore=Decimal("2"),
        )
        SubmissionTest.objects.create(
            submission=cls.submission,
            testCase=cls.hidden_pass,
            logs="hidden pass logs",
            passed=True,
            score=Decimal("3"),
            maxScore=Decimal("3"),
        )
        SubmissionTest.objects.create(
            submission=cls.submission,
            testCase=cls.hidden_fail,
            logs="hidden fail logs",
            passed=False,
            score=Decimal("0"),
            maxScore=Decimal("2"),
        )

    # ------------------------------------------------------------------
    # /assignments/{id}/testCases/ — listing visibility
    # ------------------------------------------------------------------

    def _list_test_cases_as(self, user):
        self.client.force_authenticate(user=user)
        resp = self.client.get(f"/assignments/{self.assignment.id}/studentTests/")
        self.assertEqual(resp.status_code, 200, resp.content)
        return resp.json()["testCases"]

    def test_instructor_sees_hidden_tests_in_listing(self):
        cases = self._list_test_cases_as(self.instructor)
        ids = {c["id"] for c in cases}
        self.assertIn(self.visible_test.id, ids)
        self.assertIn(self.hidden_pass.id, ids)
        self.assertIn(self.hidden_fail.id, ids)

    def test_grader_sees_hidden_tests_in_listing(self):
        cases = self._list_test_cases_as(self.grader)
        ids = {c["id"] for c in cases}
        self.assertIn(self.hidden_pass.id, ids)
        self.assertIn(self.hidden_fail.id, ids)

    def test_student_does_not_see_hidden_tests_in_listing(self):
        cases = self._list_test_cases_as(self.student)
        ids = {c["id"] for c in cases}
        self.assertIn(self.visible_test.id, ids)
        self.assertNotIn(self.hidden_pass.id, ids)
        self.assertNotIn(self.hidden_fail.id, ids)
        # Belt-and-suspenders: the hidden description/explanation must never appear in
        # a student-facing payload, even smuggled through another field.
        payload = str(cases)
        self.assertNotIn("Secret hidden test", payload)
        self.assertNotIn("Internal explanation", payload)

    # ------------------------------------------------------------------
    # /assignments/{id}/studentTests/ — test-SOURCE visibility (categories)
    # ------------------------------------------------------------------

    def _studentTests_as(self, user):
        self.client.force_authenticate(user=user)
        resp = self.client.get(f"/assignments/{self.assignment.id}/studentTests/")
        self.assertEqual(resp.status_code, 200, resp.content)
        return resp.json()

    def test_staff_still_receive_full_category_source(self):
        # Instructors and graders must keep the full category payload (testScript +
        # testFiles) so the test editor and "see as student" tooling keep working.
        for user in (self.instructor, self.grader):
            body = self._studentTests_as(user)
            categories = body["testCategories"]
            self.assertTrue(categories)
            cat = categories[0]
            self.assertIn("testScript", cat)
            self.assertIn("SECRET_TEST_SOURCE", cat["testScript"])
            self.assertIn("testFiles", cat)
            self.assertIn("SECRET_TEST_FILE_CONTENT", str(cat["testFiles"]))

    def test_student_never_receives_category_test_source(self):
        # The core anti-cheat guarantee: a student must not be able to read the
        # instructor's test harness via studentTests, even for exposed categories.
        body = self._studentTests_as(self.student)
        categories = body["testCategories"]
        self.assertTrue(categories)  # category is returned (it has an exposed case)
        for cat in categories:
            self.assertNotIn("testScript", cat)
            self.assertNotIn("testFiles", cat)
            self.assertNotIn("resources", cat)
        # Belt-and-suspenders: the secret source must not appear anywhere in the payload.
        payload = str(body)
        self.assertNotIn("SECRET_TEST_SOURCE", payload)
        self.assertNotIn("SECRET_TEST_FILE_CONTENT", payload)

    # ------------------------------------------------------------------
    # /submissions/{id}/submissionTestResults/ — run visibility
    # ------------------------------------------------------------------

    def _test_results_as(self, user):
        self.client.force_authenticate(user=user)
        resp = self.client.get(f"/submissions/{self.submission.id}/testResults/")
        self.assertEqual(resp.status_code, 200, resp.content)
        return resp.json()

    def test_instructor_sees_full_hidden_test_results(self):
        body = self._test_results_as(self.instructor)
        rows = body["submissionTests"]
        # All three real SubmissionTest rows present; no synthetic summary row.
        self.assertEqual(len(rows), 3)
        self.assertTrue(all(r.get("hiddenSummary") is None for r in rows))
        logs = {r["logs"] for r in rows}
        self.assertIn("hidden pass logs", logs)
        self.assertIn("hidden fail logs", logs)

    def test_grader_sees_full_hidden_test_results(self):
        body = self._test_results_as(self.grader)
        rows = body["submissionTests"]
        self.assertEqual(len(rows), 3)
        logs = {r["logs"] for r in rows}
        self.assertIn("hidden pass logs", logs)
        self.assertIn("hidden fail logs", logs)

    def test_student_gets_consolidated_hidden_summary(self):
        body = self._test_results_as(self.student)
        rows = body["submissionTests"]

        # Exactly one visible-test row + one synthetic per-category hidden summary.
        real_rows = [r for r in rows if r.get("hiddenSummary") is None]
        synthetic_rows = [r for r in rows if r.get("hiddenSummary") is not None]
        self.assertEqual(len(real_rows), 1, real_rows)
        self.assertEqual(real_rows[0]["testCase"], self.visible_test.id)
        self.assertEqual(len(synthetic_rows), 1, synthetic_rows)

        summary = synthetic_rows[0]
        self.assertIsNone(summary["testCase"])
        self.assertEqual(summary["testCategory"], self.category.id)
        # 1 of 2 hidden tests passed; 3 of 5 points earned.
        self.assertEqual(summary["hiddenSummary"]["passedCount"], 1)
        self.assertEqual(summary["hiddenSummary"]["totalCount"], 2)
        self.assertEqual(float(summary["hiddenSummary"]["pointsEarned"]), 3.0)
        self.assertEqual(float(summary["hiddenSummary"]["pointsTotal"]), 5.0)
        # Top-level `passed` is False because not all hidden tests passed.
        self.assertFalse(summary["passed"])

        # The student must never see the underlying hidden-test logs/descriptions.
        payload = str(body)
        self.assertNotIn("hidden pass logs", payload)
        self.assertNotIn("hidden fail logs", payload)
        self.assertNotIn("Secret hidden test", payload)
        self.assertNotIn("Internal explanation", payload)

    def test_student_summary_omitted_when_no_hidden_tests_run(self):
        # New submission with only a visible test result.
        sub = Submission.objects.create(assignment=self.assignment, isFinalized=True)
        sub.students.add(self.student)
        SubmissionTest.objects.create(
            submission=sub,
            testCase=self.visible_test,
            logs="x",
            passed=True,
            score=Decimal("2"),
            maxScore=Decimal("2"),
        )
        self.client.force_authenticate(user=self.student)
        body = self.client.get(f"/submissions/{sub.id}/testResults/").json()
        rows = body["submissionTests"]
        self.assertEqual(len(rows), 1)
        self.assertIsNone(rows[0].get("hiddenSummary"))


class StudentFailureDisplayTests(APITestCase):
    """How compile / runtime / assertion failures are shown to a STUDENT.

    - Compile (or couldn't-run) failures collapse to a single generic message; the raw
      compiler stderr is never sent to the student.
    - Runtime errors (status 'error') and assertion failures (status 'failed') keep their
      per-test message so students can debug their own code.
    - Raw execution `logs` are grader-only.
    """

    @classmethod
    def setUpTestData(cls):
        cls.org = Organization.objects.create(name="Org2", shortname="org2")
        cls.instructor = User.objects.create_user("i2@org.edu", "i2@org.edu", "pw")
        cls.instructor.profile.organization = cls.org
        cls.instructor.save()
        cls.student = User.objects.create_user("s2@org.edu", "s2@org.edu", "pw")
        cls.student.profile.organization = cls.org
        cls.student.save()

        cls.course = Course.objects.create(name="cs2", period="s2026", organization=cls.org)
        cls.course.courseAdmins.add(cls.instructor)
        cls.course.students.add(cls.student)

        cls.assignment = Assignment.objects.create(
            course=cls.course, name="A2", points=30, state='published',
        )
        cls.category = TestCategory.objects.create(name="Cat", assignment=cls.assignment)

        cls.compile_tc = AutograderTestCase.objects.create(
            testCategory=cls.category, functionName="t_compile",
            description="Compile", pointsPass=10, hidden=False, exposed=True,
        )
        cls.runtime_tc = AutograderTestCase.objects.create(
            testCategory=cls.category, functionName="t_runtime",
            description="Runtime", pointsPass=10, hidden=False, exposed=True,
        )
        cls.assert_tc = AutograderTestCase.objects.create(
            testCategory=cls.category, functionName="t_assert",
            description="Assert", pointsPass=10, hidden=False, exposed=True,
        )

        cls.submission = Submission.objects.create(
            assignment=cls.assignment, isFinalized=True,
        )
        cls.submission.students.add(cls.student)

        # Compile failure: errored, no per-test results, raw javac stderr in logs.
        cls.compile_st = SubmissionTest.objects.create(
            submission=cls.submission, testCase=cls.compile_tc,
            logs="Main.java:7: error: ';' expected\n    return x\n            ^\n1 error",
            passed=False, isError=True, score=Decimal("0"), maxScore=Decimal("10"),
            results=None,
        )
        # Runtime error: compiled, test threw a non-assertion exception.
        cls.runtime_st = SubmissionTest.objects.create(
            submission=cls.submission, testCase=cls.runtime_tc,
            logs="raw run logs that students must not see",
            passed=False, isError=True, score=Decimal("0"), maxScore=Decimal("10"),
            results=[{
                "name": "t_runtime", "passed": False, "score": 0, "maxScore": 10,
                "status": "error", "error": "java.lang.NullPointerException: boom",
            }],
        )
        # Assertion failure: compiled, assertion did not hold.
        cls.assert_st = SubmissionTest.objects.create(
            submission=cls.submission, testCase=cls.assert_tc,
            logs="raw assert logs",
            passed=False, isError=False, score=Decimal("0"), maxScore=Decimal("10"),
            results=[{
                "name": "t_assert", "passed": False, "score": 0, "maxScore": 10,
                "status": "failed", "error": "expected: <42> but was: <0>",
            }],
        )

    def _results_as(self, user):
        self.client.force_authenticate(user=user)
        resp = self.client.get(f"/submissions/{self.submission.id}/testResults/")
        self.assertEqual(resp.status_code, 200, resp.content)
        return {r["testCase"]: r for r in resp.json()["submissionTests"] if r.get("testCase")}

    def test_student_compile_failure_is_generic(self):
        rows = self._results_as(self.student)
        row = rows[self.compile_tc.id]
        self.assertEqual(row["logs"], "")  # raw stderr dropped
        self.assertTrue(row["results"])  # synthetic generic result present
        res = row["results"][0]
        self.assertEqual(res["status"], "compile_error")
        self.assertIn("didn't compile", res["error"])
        # The raw compiler text must be gone entirely.
        self.assertNotIn("';' expected", str(row))
        self.assertNotIn("Main.java:7", str(row))

    def test_student_runtime_error_shows_exception(self):
        rows = self._results_as(self.student)
        row = rows[self.runtime_tc.id]
        self.assertEqual(row["logs"], "")  # raw logs grader-only
        res = row["results"][0]
        self.assertEqual(res["status"], "error")
        self.assertIn("NullPointerException", res["error"])
        self.assertNotIn("raw run logs", str(row))

    def test_student_assertion_failure_shows_message(self):
        rows = self._results_as(self.student)
        row = rows[self.assert_tc.id]
        self.assertEqual(row["logs"], "")
        res = row["results"][0]
        self.assertEqual(res["status"], "failed")
        self.assertIn("expected", res["error"])
        self.assertNotIn("raw assert logs", str(row))

    def test_grader_keeps_full_logs_and_raw_compiler_output(self):
        rows = self._results_as(self.instructor)
        self.assertIn("';' expected", rows[self.compile_tc.id]["logs"])
        self.assertIn("raw run logs", rows[self.runtime_tc.id]["logs"])
        self.assertIn("raw assert logs", rows[self.assert_tc.id]["logs"])
