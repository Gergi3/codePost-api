# Copyright © 2026 Rutgers, the State University of New Jersey. All rights reserved except as defined by the Rutgers Non-Commercial License, included with this software.
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase
from rest_framework.test import APIRequestFactory

from core.models import (
    Assignment,
    AssignmentDataSet,
    AssignmentFile,
    Course,
    LearningObjective,
    Organization,
    RubricCategory,
    RubricComment,
    User,
    TestCase as AssignmentTestCase,
    TestCategory,
    TestCategoryResource,
)
from core.serializers.course import CourseSerializer
from core.utils import copy_assignment


class AssignmentCloneTests(TestCase):
    def setUp(self):
        self.org = Organization.objects.create(name="Clone Org", shortname="cloneorg")
        self.source_course = Course.objects.create(name="Source Course", period="F2026", organization=self.org)
        self.destination_course = Course.objects.create(name="Destination Course", period="S2027", organization=self.org)

        self.assignment = Assignment.objects.create(
            name="HW1",
            course=self.source_course,
            points=100,
            state='published',
        )

        self.template_file = AssignmentFile.objects.create(
            assignment=self.assignment,
            name="helpers.py",
            extension=".py",
            data="def helper():\n    return 42\n",
            path="tests",
            required=False,
            description="Helper file",
        )

        self.dataset = AssignmentDataSet.objects.create(
            assignment=self.assignment,
            name="cases.csv",
            description="input cases",
            mount_path="shared/cases.csv",
            file=SimpleUploadedFile("cases.csv", b"x,y\n1,2\n"),
        )

        self.category = TestCategory.objects.create(
            assignment=self.assignment,
            name="Script Tests",
            testScript='@test(name="sum", points=5)\ndef test_sum():\n    assert 1 + 1 == 2\n',
            targetFileName="solution.py",
            sortKey=2,
            maxPoints=5,
        )

        # The testScript on the category already triggers a parser sync that creates
        # the test_sum TestCase (className=''). Update that row rather than creating a
        # colliding duplicate (which the unique constraint now rejects).
        AssignmentTestCase.objects.update_or_create(
            testCategory=self.category,
            className="",
            functionName="test_sum",
            defaults=dict(
                description="sum test",
                type="script",
                pointsPass=5,
                pointsFail=0,
                testCode="assert 1 + 1 == 2",
                timeout=15,
            ),
        )

        TestCategoryResource.objects.create(
            category=self.category,
            file=self.template_file,
            target_path="resources/helpers.py",
        )
        TestCategoryResource.objects.create(
            category=self.category,
            dataset=self.dataset,
            target_path="resources/cases.csv",
        )

    def test_copy_assignment_copies_test_script_and_resources(self):
        copied = copy_assignment(self.assignment, self.destination_course)

        self.assertIsNotNone(copied)
        copied_id = copied.id  # type: ignore[union-attr]
        copied = Assignment.objects.get(id=copied_id)
        self.assertEqual(copied.course.id, self.destination_course.id)

        copied_category = copied.testCategories.get(name="Script Tests")
        self.assertEqual(copied_category.testScript, self.category.testScript)
        self.assertEqual(copied_category.targetFileName, "solution.py")

        copied_resources = TestCategoryResource.objects.filter(category=copied_category).order_by("target_path")
        self.assertEqual(copied_resources.count(), 2)

        copied_file_resource = copied_resources.get(target_path="resources/helpers.py")
        self.assertIsNotNone(copied_file_resource.file)
        copied_file = copied_file_resource.file
        if copied_file is None:
            self.fail("Expected copied file resource to reference a file")
        self.assertEqual(copied_file.assignment.id, copied.id)
        self.assertEqual(copied_file.name, self.template_file.name)

        copied_dataset_resource = copied_resources.get(target_path="resources/cases.csv")
        self.assertIsNotNone(copied_dataset_resource.dataset)
        copied_dataset = copied_dataset_resource.dataset
        if copied_dataset is None:
            self.fail("Expected copied dataset resource to reference a dataset")
        self.assertEqual(copied_dataset.assignment.id, copied.id)
        self.assertEqual(copied_dataset.name, self.dataset.name)

    def test_copy_assignment_copies_rubric_flags_and_remaps_rubric_items(self):
        category = RubricCategory.objects.create(
            assignment=self.assignment,
            name="Style",
            atMostOnce=True,
        )
        comment = RubricComment.objects.create(
            category=category,
            text="Missing docstring",
            pointDelta=2,
        )
        AssignmentTestCase.objects.create(
            testCategory=self.category,
            description="docstring test",
            type="script",
            hidden=True,
            rubricItem=comment,
        )

        copied = copy_assignment(self.assignment, self.destination_course)
        self.assertIsNotNone(copied)

        copied_category = copied.rubricCategories.get(name="Style")  # type: ignore[union-attr]
        self.assertTrue(copied_category.atMostOnce)

        copied_test = AssignmentTestCase.objects.get(
            testCategory__assignment=copied, description="docstring test")
        self.assertTrue(copied_test.hidden)
        # The rubric item must be the CLONED comment, not a link back into the source course.
        self.assertIsNotNone(copied_test.rubricItem)
        self.assertNotEqual(copied_test.rubricItem.id, comment.id)
        self.assertEqual(copied_test.rubricItem.category.assignment_id, copied.id)  # type: ignore[union-attr]
        self.assertEqual(copied_test.rubricItem.text, "Missing docstring")

    def test_copy_assignment_nullifies_stale_rubric_item(self):
        # A rubricItem pointing at ANOTHER assignment's rubric (stale data) must become
        # None on the clone rather than staying linked outside the cloned assignment.
        other_assignment = Assignment.objects.create(
            name="HW2", course=self.source_course, points=50)
        other_category = RubricCategory.objects.create(assignment=other_assignment, name="Other")
        other_comment = RubricComment.objects.create(
            category=other_category, text="Elsewhere", pointDelta=1)
        AssignmentTestCase.objects.create(
            testCategory=self.category,
            description="stale rubric test",
            type="script",
            rubricItem=other_comment,
        )

        copied = copy_assignment(self.assignment, self.destination_course)
        self.assertIsNotNone(copied)

        copied_test = AssignmentTestCase.objects.get(
            testCategory__assignment=copied, description="stale rubric test")
        self.assertIsNone(copied_test.rubricItem)

    def test_copy_assignment_copies_learning_objectives(self):
        objective = LearningObjective.objects.create(
            assignment=self.assignment,
            shortId="recursion",
            name="Recursion",
            description="Uses recursion correctly",
            visibilityMode="on_pass",
            aggregationMode="any",
        )
        test_case = self.category.testCases.get(description="sum test")
        test_case.learningObjectives.add(objective)

        copied = copy_assignment(self.assignment, self.destination_course)
        self.assertIsNotNone(copied)

        copied_objective = copied.learningObjectives.get()  # type: ignore[union-attr]
        self.assertNotEqual(copied_objective.id, objective.id)
        self.assertEqual(copied_objective.shortId, "recursion")
        self.assertEqual(copied_objective.name, "Recursion")
        self.assertEqual(copied_objective.visibilityMode, "on_pass")
        self.assertEqual(copied_objective.aggregationMode, "any")

        copied_test = AssignmentTestCase.objects.get(
            testCategory__assignment=copied, description="sum test")
        self.assertEqual(list(copied_test.learningObjectives.all()), [copied_objective])


class CourseCloneTests(TestCase):
    def setUp(self):
        self.org = Organization.objects.create(name="Course Clone Org", shortname="courseclone")
        self.source_course = Course.objects.create(name="Source", period="F2026", organization=self.org)

        self.user = User.objects.create_user(username="admin@clone.org", email="admin@clone.org", password="pw")
        self.user.profile.organization = self.org
        self.user.profile.canCreateCourses = True
        self.user.profile.canModifyRosters = True
        self.user.profile.save()

        self.source_course.courseAdmins.add(self.user)
        self.source_course.graders.add(self.user)

        assignment = Assignment.objects.create(name="Lab 1", course=self.source_course, points=10)
        helper = AssignmentFile.objects.create(
            assignment=assignment,
            name="helper.py",
            extension=".py",
            data="print('helper')",
        )
        category = TestCategory.objects.create(
            assignment=assignment,
            name="Autograder",
            testScript='@test(name="t", points=1)\ndef t():\n    assert True\n',
        )
        TestCategoryResource.objects.create(category=category, file=helper, target_path="helper.py")

    def test_course_serializer_clone_transfers_assignment_test_assets(self):
        request = APIRequestFactory().post("/courses/", {})
        request.user = self.user
        request.auth = "a" * 40

        serializer = CourseSerializer(
            data={"name": "Cloned", "period": "S2027", "cloneFrom": self.source_course.id},
            context={"request": request},
        )
        self.assertTrue(serializer.is_valid(), serializer.errors)

        cloned_course = serializer.save()

        cloned_assignment = cloned_course.assignments.get(name="Lab 1")
        cloned_category = cloned_assignment.testCategories.get(name="Autograder")

        self.assertEqual(cloned_category.testScript, '@test(name="t", points=1)\ndef t():\n    assert True\n')
        self.assertEqual(cloned_category.resources.count(), 1)
        self.assertEqual(cloned_category.resources.first().target_path, "helper.py")
        self.assertEqual(cloned_category.resources.first().file.assignment_id, cloned_assignment.id)


class AssignmentCloneLifecycleResetTests(TestCase):
    """A clone must land fully inert: draft, upload disabled, no schedules — the
    pre-lifecycle behavior left allowStudentUpload on with no due date, so clones
    accepted (hidden) uploads forever."""

    def setUp(self):
        self.org = Organization.objects.create(name="Clone Org 2", shortname="cloneorg2")
        self.source_course = Course.objects.create(name="Src", period="F2026", organization=self.org)
        self.destination_course = Course.objects.create(name="Dst", period="S2027", organization=self.org)

    def test_clone_resets_lifecycle_and_upload_flags(self):
        from django.utils import timezone
        source = Assignment.objects.create(
            name="HW-flags",
            course=self.source_course,
            points=50,
            state='published',
            allowStudentUpload=True,
            allowStudentUploadWithPartners=True,
            uploadDueDate=timezone.now(),
            publishAt=timezone.now(),
            scheduledPublishRanAt=timezone.now(),
        )
        clone = copy_assignment(source, self.destination_course)
        self.assertIsNotNone(clone)
        clone.refresh_from_db()

        self.assertEqual(clone.state, 'draft')
        self.assertIsNone(clone.publishedAt)
        self.assertIsNone(clone.publishAt)
        self.assertIsNone(clone.scheduledPublishRanAt)
        self.assertFalse(clone.allowStudentUpload)
        self.assertFalse(clone.allowStudentUploadWithPartners)
        self.assertIsNone(clone.uploadDueDate)
        self.assertEqual(clone.state, 'draft')
