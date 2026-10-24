# Copyright © 2026 Rutgers, the State University of New Jersey. All rights reserved except as defined by the Rutgers Non-Commercial License, included with this software.
"""Unit tests for the student-facing test-row sanitizer in core/views/submission.py.

Covers the boundary that decides what a STUDENT sees for an errored test:
- a real compile failure shows the generic banner + ONLY the student's own
  compiler lines (instructor test-file lines are stripped),
- an OOM/timeout/didn't-run row is NOT mislabeled "didn't compile" and gets a
  clear "did not run" message,
- a normal assertion/runtime row keeps its own per-test results.
"""
from django.test import SimpleTestCase

from core.views.submission import (
    _filter_log_to_student_files,
    _sanitize_student_test_row,
    STUDENT_COMPILE_FAILURE_MESSAGE,
)


class FilterLogToStudentFilesTests(SimpleTestCase):
    def test_keeps_student_file_lines_strips_test_file_lines(self):
        log = (
            "Exit Code: 1\n"
            "./src/bg/.../vehicle/Car.java:5: error: ';' expected\n"
            "    private static final int SURCHARGE = 15\n"
            "                                           ^\n"
            "./src/bg/.../sanction/TowAwayTest.java:5: error: cannot find symbol\n"
            "import bg...Jeep;\n"
        )
        out = _filter_log_to_student_files(log, {"Car.java"})
        self.assertIn("Car.java:5: error: ';' expected", out)
        self.assertIn("SURCHARGE = 15", out)   # continuation line kept
        self.assertNotIn("TowAwayTest.java", out)  # instructor test stripped
        self.assertNotIn("Jeep", out)

    def test_empty_when_no_student_files(self):
        self.assertEqual(_filter_log_to_student_files("Car.java:1: error: x", set()), "")

    def test_strips_trailing_runner_noise(self):
        log = (
            "./x/Car.java:5: error: ';' expected\n"
            "    int SURCHARGE = 15\n"
            "1 error\n"
            "Error: Could not find or load main class JUnitPlatformRunner\n"
            "Caused by: java.lang.ClassNotFoundException: JUnitPlatformRunner\n"
        )
        out = _filter_log_to_student_files(log, {"Car.java"})
        self.assertIn("Car.java:5", out)
        self.assertNotIn("JUnitPlatformRunner", out)
        self.assertNotIn("ClassNotFoundException", out)


class SanitizeStudentTestRowTests(SimpleTestCase):
    def test_real_compile_error_shows_banner_plus_own_lines(self):
        row = {
            'isError': True,
            'maxScore': 0.08,
            'logs': "./src/x/Car.java:5: error: ';' expected\n    int SURCHARGE = 15\n",
            'results': [],
        }
        out = _sanitize_student_test_row(row, {"Car.java"})
        self.assertEqual(out['results'][0]['status'], 'compile_error')
        self.assertIn(STUDENT_COMPILE_FAILURE_MESSAGE, out['results'][0]['error'])
        self.assertIn("Car.java:5", out['results'][0]['error'])
        self.assertEqual(out['logs'], '')

    def test_compile_error_referencing_only_test_files_hides_detail(self):
        row = {
            'isError': True,
            'maxScore': 0.08,
            'logs': "./x/TowAwayTest.java:5: error: cannot find symbol\nimport ...Jeep;\n",
            'results': [],
        }
        out = _sanitize_student_test_row(row, {"Car.java"})
        # Still flagged as compile error, but no instructor test detail leaks.
        self.assertEqual(out['results'][0]['status'], 'compile_error')
        self.assertNotIn("TowAwayTest", out['results'][0]['error'])
        self.assertEqual(out['results'][0]['error'], STUDENT_COMPILE_FAILURE_MESSAGE)

    def test_oom_errored_empty_is_not_compile_error(self):
        row = {
            'isError': True,
            'maxScore': 0.08,
            # The "name mismatch / Available Results: ..." text has NO compile markers.
            'logs': "Test did not execute or name mismatch.\nAvailable Results: a, b, c",
            'results': [],
        }
        out = _sanitize_student_test_row(row, {"Car.java"})
        self.assertEqual(out['results'][0]['status'], 'error')
        self.assertNotIn("compile", out['results'][0]['error'].lower())
        self.assertIn("did not run", out['results'][0]['error'].lower())
        self.assertEqual(out['logs'], '')  # raw log (lists test names) dropped

    def test_normal_assertion_row_keeps_its_results(self):
        row = {
            'isError': False,
            'maxScore': 0.08,
            'logs': "some grader-only execution log",
            'results': [{'name': 'testFoo', 'status': 'failed', 'passed': False,
                         'error': 'expected: <15> but was: <999>', 'score': 0, 'max_score': 0.08}],
        }
        out = _sanitize_student_test_row(row, {"Car.java"})
        self.assertEqual(out['results'][0]['status'], 'failed')
        self.assertIn("expected", out['results'][0]['error'])
        self.assertEqual(out['logs'], '')  # grader-only log still dropped
