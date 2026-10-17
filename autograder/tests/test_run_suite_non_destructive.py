# Copyright © 2026 Rutgers, the State University of New Jersey. All rights reserved except as defined by the Rutgers Non-Commercial License, included with this software.
"""
Regression net for the destructive-sync data-loss bug.

A student submitting a file that produces NO matching test results (an unrelated
.png, or a .java that doesn't contain the instructor's test class) used to cause
TestService.run_suite to DELETE every instructor-owned TestCase in the category —
cascading TestCategory.maxPoints to 0 (the student then saw "No tests available"
and the instructor saw "Tests not yet synced / Max pts: 0").

A student submission run passes allow_sync=False, so it must NEVER mutate the
instructor's TestCases. Instead it records failing SubmissionTests for the tests
that didn't run, leaving the instructor's test set and maxPoints intact.
"""
from decimal import Decimal
from unittest import mock

from django.contrib.auth.models import User
from django.test import TestCase

from core.models import (
    Assignment,
    Course,
    Organization,
    Submission,
    SubmissionFile,
    SubmissionTest,
    TestCase as AutograderTestCase,
    TestCategory,
)
from autograder.services.TestService import TestService


class RunSuiteNonDestructiveTests(TestCase):
    def setUp(self):
        self.org = Organization.objects.create(name="Org", shortname="org")
        self.student = User.objects.create_user("stu@org.edu", "stu@org.edu", "pw")
        self.student.profile.organization = self.org
        self.student.save()

        self.course = Course.objects.create(name="cs101", period="s2026", organization=self.org)
        self.course.students.add(self.student)

        self.assignment = Assignment.objects.create(
            course=self.course, name="Lab01", points=20, state="published",
        )

        # A script-backed category (run_suite's script branch requires testScript or
        # a testFile) with two instructor tests worth 4 points total.
        self.category = TestCategory.objects.create(
            name="Functional",
            assignment=self.assignment,
            testScript="// HashtagExtractorTest.java",
            maxPoints=Decimal("4"),
        )
        self.t1 = AutograderTestCase.objects.create(
            testCategory=self.category,
            functionName="testExtractsSingleTag",
            className="HashtagExtractorTest",
            description="extracts single tag",
            pointsPass=Decimal("2"),
            type="script",
        )
        self.t2 = AutograderTestCase.objects.create(
            testCategory=self.category,
            functionName="testExtractsMultipleTags",
            className="HashtagExtractorTest",
            description="extracts multiple tags",
            pointsPass=Decimal("2"),
            type="script",
        )

        self.submission = Submission.objects.create(assignment=self.assignment)
        self.submission.students.add(self.student)

    def _add_file(self, name):
        SubmissionFile.objects.create(submission=self.submission, name=name, extension="", data="x")

    def test_submission_with_no_matching_results_preserves_instructor_tests(self):
        """png / unrelated submission: executor exits clean but yields zero results.
        Instructor TestCases and maxPoints must survive; students get failing rows."""
        self._add_file("meme.png")

        # Executor "succeeds" (clean exit) but finds none of the instructor's tests.
        fake_exec = {"success": True, "tests": [], "stdout": "", "stderr": "", "output_data": {}}
        with mock.patch.object(TestService, "_run_ephemeral_execution", return_value=fake_exec):
            TestService.run_suite(self.submission.id)  # default allow_sync=False

        # The two instructor tests still exist.
        self.assertTrue(AutograderTestCase.objects.filter(pk=self.t1.pk).exists())
        self.assertTrue(AutograderTestCase.objects.filter(pk=self.t2.pk).exists())
        self.assertEqual(self.category.testCases.count(), 2)

        # maxPoints was not zeroed by a cascade of deletions.
        self.category.refresh_from_db()
        self.assertEqual(self.category.maxPoints, Decimal("4"))

        # The student sees each expected test recorded as a failing run.
        sts = SubmissionTest.objects.filter(submission=self.submission)
        self.assertEqual(sts.count(), 2)
        self.assertTrue(all(not st.passed for st in sts))

    def test_allow_sync_false_never_deletes_even_with_partial_results(self):
        """A submission that runs ONE test but not the other must not delete the
        missing one when allow_sync=False."""
        self._add_file("HashtagExtractor.java")

        fake_exec = {
            "success": True,
            "tests": [
                {
                    "name": "testExtractsSingleTag",
                    "className": "HashtagExtractorTest",
                    "passed": True,
                    "score": 2,
                    "max_score": 2,
                    "status": "passed",
                }
            ],
            "stdout": "",
            "stderr": "",
            "output_data": {},
        }
        with mock.patch.object(TestService, "_run_ephemeral_execution", return_value=fake_exec):
            TestService.run_suite(self.submission.id)  # allow_sync=False

        # Both instructor tests survive; the un-run one is reported, not deleted.
        self.assertEqual(self.category.testCases.count(), 2)
        self.category.refresh_from_db()
        self.assertEqual(self.category.maxPoints, Decimal("4"))

    def test_allow_sync_true_still_requires_results_before_deleting(self):
        """Even an explicit opt-in must not prune on an empty result set — an empty
        run is never evidence the instructor's tests are gone."""
        self._add_file("meme.png")

        fake_exec = {"success": True, "tests": [], "stdout": "", "stderr": "", "output_data": {}}
        with mock.patch.object(TestService, "_run_ephemeral_execution", return_value=fake_exec):
            TestService.run_suite(self.submission.id, allow_sync=True)

        self.assertEqual(self.category.testCases.count(), 2)
        self.category.refresh_from_db()
        self.assertEqual(self.category.maxPoints, Decimal("4"))
