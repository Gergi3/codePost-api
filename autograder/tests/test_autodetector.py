# Copyright © 2026 Rutgers, the State University of New Jersey. All rights reserved except as defined by the Rutgers Non-Commercial License, included with this software.
from django.test import TestCase
from core.models import Submission, Assignment, Course, Environment, SubmissionFile, Organization
from autograder.services.autodetector import Autodetector

class AutodetectorTest(TestCase):
    def setUp(self):
        self.organization = Organization.objects.create(name="Test Org", shortname="TEST")
        self.course = Course.objects.create(
            name="Test Course",
            period="Fall 2025",
            organization=self.organization,
        )
        self.assignment = Assignment.objects.create(
            course=self.course,
            name="Test Assignment",
            points=100,
        )
        self.environment = Environment.objects.create(assignment=self.assignment, auto_detect=True, language="other")
        self.submission = Submission.objects.create(
            assignment=self.assignment,
            gradeFrozen=True,
        )

    def test_detect_python(self):
        SubmissionFile.objects.create(submission=self.submission, name="main.py", data="print('hello')", extension="py")
        
        updated = Autodetector.detect_and_update(self.submission)
        
        self.assertTrue(updated)
        self.environment.refresh_from_db()
        self.assertEqual(self.environment.language, "python-3.12")

    def test_detect_java(self):
        SubmissionFile.objects.create(submission=self.submission, name="Main.java", data="class Main {}", extension="java")
        
        updated = Autodetector.detect_and_update(self.submission)
        
        self.assertTrue(updated)
        self.environment.refresh_from_db()
        self.assertEqual(self.environment.language, "java-27")

    def test_no_update_if_auto_detect_false(self):
        self.environment.auto_detect = False
        self.environment.save()
        
        SubmissionFile.objects.create(submission=self.submission, name="main.py", data="print('hello')", extension="py")
        
        updated = Autodetector.detect_and_update(self.submission)
        
        self.assertFalse(updated)
        self.environment.refresh_from_db()
        self.assertEqual(self.environment.language, "other")
