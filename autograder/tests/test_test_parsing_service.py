# Copyright © 2026 Rutgers, the State University of New Jersey. All rights reserved except as defined by the Rutgers Non-Commercial License, included with this software.
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from django.test import SimpleTestCase, TestCase

from autograder.services.TestParsingService import TestParsingService
from core.services.ai_service import AIService


class TestParsingServiceAlignmentTests(SimpleTestCase):
    def _parse(self, script: str, language: str):
        category = SimpleNamespace(testScript=script)
        return TestParsingService.parse_script(category, language=language)

    def test_ai_examples_parse_for_supported_languages(self):
        expected_by_language = {
            "python": ["Test Name", "Test Partial", "Test Explanation"],
            "java": ["Test Name", "Test Partial", "Test Explanation"],
            "javascript": ["Test Name", "Test Partial", "Test Explanation"],
            "node": ["Test Name", "Test Partial", "Test Explanation"],
            "cpp": ["TestName", "TestPartial", "TestExplanation"],
            "c": ["TestName", "TestPartial", "TestExplanation"],
            "r": ["Test Name", "Test Partial", "Test Explanation"],
            "php": ["Test Name", "Test Partial", "Test Explanation"],
            "ruby": ["Test Name", "Test Partial", "Test Explanation"],
        }

        for language, expected_names in expected_by_language.items():
            script = AIService.LANGUAGE_EXAMPLES[language]
            parsed = self._parse(script, language)

            self.assertGreaterEqual(
                len(parsed),
                3,
                msg=f"Expected at least 3 parsed tests for language '{language}', got {len(parsed)}",
            )

            parsed_names = [test["name"] for test in parsed]
            self.assertEqual(
                parsed_names[:3],
                expected_names,
                msg=f"Parsed names mismatch for language '{language}'",
            )

            for test in parsed[:3]:
                self.assertIn("points", test)
                self.assertGreater(test["points"], 0)

    def test_java_autodetection_accepts_non_void_test_methods(self):
        category = SimpleNamespace(testScript=AIService.LANGUAGE_EXAMPLES["java"])
        parsed = TestParsingService.parse_script(category)

        self.assertGreaterEqual(len(parsed), 1)
        self.assertEqual(parsed[0]["name"], "Test Name")

    def test_update_test_cases_uses_assignment_environment_language(self):
        # update_test_cases parses each of the category's files via parse_script,
        # passing the assignment's environment language through.
        category = SimpleNamespace(
            testScript='@Test(name="X", points=1) public void x() {}',
            testFiles=SimpleNamespace(all=lambda: []),
            assignment=SimpleNamespace(environment=SimpleNamespace(language="java-17")),
            testCases=SimpleNamespace(all=lambda: []),
            pk=123,
        )

        with patch.object(TestParsingService, "parse_script", return_value=[]) as parse_mock, \
             patch("autograder.services.TestParsingService.TestCategory.objects.filter") as filter_mock:
            filter_mock.return_value.update = MagicMock()

            TestParsingService.update_test_cases(category)

        # parse_script is called once (for the single legacy-script source) with the
        # environment language threaded through.
        self.assertEqual(parse_mock.call_count, 1)
        self.assertEqual(parse_mock.call_args.kwargs.get("language"), "java-17")

    def test_python_positional_points_are_parsed(self):
        script = '''
@test("Partial Credit", 5)
def test_partial():
    return 2.5
'''
        parsed = self._parse(script, "python")

        self.assertEqual(len(parsed), 1)
        self.assertEqual(parsed[0]["name"], "Partial Credit")
        self.assertEqual(parsed[0]["functionName"], "test_partial")
        self.assertEqual(parsed[0]["points"], 5)

    def test_python_hidden_and_objectives_kwargs(self):
        script = '''
@test(name="Reverses", points=2, hidden=True, objectives=["recursion", "lists"])
def test_reverse():
    return 1
'''
        parsed = self._parse(script, "python")

        self.assertEqual(len(parsed), 1)
        self.assertIs(parsed[0]["hidden"], True)
        self.assertEqual(parsed[0]["objectives"], ["recursion", "lists"])

    def test_python_objectives_drops_empty_strings(self):
        # Empty string elements should be filtered, not silently auto-create blank LearningObjectives.
        script = '''
@test(name="x", points=1, objectives=["recursion", "", "lists"])
def test_x():
    return 1
'''
        parsed = self._parse(script, "python")

        self.assertEqual(parsed[0]["objectives"], ["recursion", "lists"])

    def test_codepost_directive_objectives_with_spaces(self):
        # `@codepost objectives = a, b, c` should yield three objectives, not one.
        script = '''
// @codepost hidden objectives = recursion, edge-cases, lists
test("Reverses", 2, "desc", function(){ return 1; }, 30);
'''
        parsed = self._parse(script, "node")

        self.assertEqual(len(parsed), 1)
        self.assertIs(parsed[0].get("hidden"), True)
        self.assertEqual(parsed[0].get("objectives"), ["recursion", "edge-cases", "lists"])

    def test_codepost_directive_objectives_terminates_at_trailing_keyword(self):
        # `objectives=a,b hidden` should yield exactly [a, b]; "hidden" must not be folded
        # into the final objective value.
        script = '''
// @codepost objectives=a,b hidden
test("Reverses", 2, "desc", function(){ return 1; }, 30);
'''
        parsed = self._parse(script, "node")

        self.assertEqual(len(parsed), 1)
        self.assertEqual(parsed[0].get("objectives"), ["a", "b"])
        self.assertIs(parsed[0].get("hidden"), True)


class JUnit5ParsingTests(SimpleTestCase):
    """Parsing of real (bare @Test) JUnit Jupiter test scripts."""

    def _parse(self, script: str):
        category = SimpleNamespace(testScript=script)
        return TestParsingService.parse_script(category, language="java-27")

    JUNIT_IMPORTS = (
        "import org.junit.jupiter.api.Test;\n"
        "import org.junit.jupiter.api.DisplayName;\n"
        "import org.junit.jupiter.api.BeforeEach;\n"
        "import static org.junit.jupiter.api.Assertions.*;\n"
    )

    def test_bare_test_methods_parse_with_method_name(self):
        script = self.JUNIT_IMPORTS + '''
public class CalcTest {
    @Test
    void testAdd() { assertEquals(3, 1 + 2); }

    @Test
    public void testSub() throws Exception { assertEquals(1, 2 - 1); }
}
'''
        parsed = self._parse(script)
        by = {t["functionName"]: t for t in parsed}
        self.assertEqual(set(by), {"testAdd", "testSub"})
        self.assertTrue(all(t["points"] == 1.0 for t in parsed))
        # name defaults to the method name when there is no @DisplayName
        self.assertEqual(by["testAdd"]["name"], "testAdd")

    def test_lifecycle_methods_are_not_tests(self):
        script = self.JUNIT_IMPORTS + '''
public class LifecycleTest {
    @BeforeEach
    void setUp() {}

    @AfterEach
    void tearDown() {}

    @Test
    void realTest() { assertTrue(true); }
}
'''
        parsed = self._parse(script)
        self.assertEqual([t["functionName"] for t in parsed], ["realTest"])

    def test_display_name_becomes_name(self):
        script = self.JUNIT_IMPORTS + '''
public class DnTest {
    @DisplayName("Adds two numbers")
    @Test
    void testAdd() { assertEquals(3, 1 + 2); }
}
'''
        parsed = self._parse(script)
        self.assertEqual(parsed[0]["functionName"], "testAdd")
        self.assertEqual(parsed[0]["name"], "Adds two numbers")

    def test_codepost_points_directive_above_stacked_annotations(self):
        # The directive sits ABOVE @DisplayName and @Test — the earlier bug stopped
        # scanning at the first annotation line and lost it.
        script = self.JUNIT_IMPORTS + '''
public class PointsTest {
    // @codepost points=2.5
    @DisplayName("Weighted")
    @Test
    void testWeighted() { assertTrue(true); }

    @Test
    void testDefault() { assertTrue(true); }
}
'''
        by = {t["functionName"]: t for t in self._parse(script)}
        self.assertEqual(by["testWeighted"]["points"], 2.5)
        self.assertEqual(by["testWeighted"]["name"], "Weighted")
        self.assertEqual(by["testDefault"]["points"], 1.0)

    def test_codepost_hidden_and_points_together(self):
        script = self.JUNIT_IMPORTS + '''
public class HiddenTest {
    // @codepost hidden points=3
    @Test
    void secret() { assertTrue(true); }
}
'''
        t = self._parse(script)[0]
        self.assertIs(t.get("hidden"), True)
        self.assertEqual(t["points"], 3.0)

    def test_junit_import_routes_to_junit5_not_custom(self):
        # A JUnit import must select the Jupiter parser even though the custom
        # parser also keys off "@Test".
        script = self.JUNIT_IMPORTS + '''
public class RouteTest {
    @Test
    void onlyBareTest() { assertTrue(true); }
}
'''
        parsed = self._parse(script)
        self.assertEqual(len(parsed), 1)
        self.assertEqual(parsed[0]["functionName"], "onlyBareTest")

    def test_legacy_custom_form_without_junit_import_still_parses(self):
        # No org.junit import -> custom @Test(name=,points=) parser.
        script = '''
@Test(name="Legacy", points=5)
public double legacyTest() { return 5.0; }
'''
        category = SimpleNamespace(testScript=script)
        parsed = TestParsingService.parse_script(category, language="java-27")
        self.assertEqual(len(parsed), 1)
        self.assertEqual(parsed[0]["name"], "Legacy")
        self.assertEqual(parsed[0]["points"], 5)


class HumanizeTests(SimpleTestCase):
    def test_camel_and_pascal_split_preserve_casing(self):
        h = TestParsingService._humanize
        self.assertEqual(h("thisIsAPascalCaseTest"), "This Is A Pascal Case Test")
        self.assertEqual(h("testEmptyArray"), "Test Empty Array")

    def test_acronyms_preserved(self):
        self.assertEqual(TestParsingService._humanize("parseHTTPResponse"), "Parse HTTP Response")

    def test_snake_case_title_cased(self):
        self.assertEqual(TestParsingService._humanize("snake_case_name"), "Snake Case Name")


class JUnitSyncTests(TestCase):
    """DB-backed tests for update_test_cases: multi-file, DB-points preservation."""

    JUNIT = (
        "import org.junit.jupiter.api.Test;\n"
        "public class {cls} {{\n"
        "  @Test void {m1}() {{}}\n"
        "  @Test void {m2}() {{}}\n"
        "}}\n"
    )

    def _category(self):
        from core.tests.factories import TestCategoryFactory
        return TestCategoryFactory(name="Functional")

    def test_multi_file_aggregates_and_defaults(self):
        from core.models import TestCategoryFile
        cat = self._category()
        TestCategoryFile.objects.create(
            category=cat, name="FooTest.java",
            content=self.JUNIT.format(cls="FooTest", m1="thisIsAPascalCaseTest", m2="testEmptyArray"),
            sortKey=0)
        TestCategoryFile.objects.create(
            category=cat, name="BarTest.java",
            content=self.JUNIT.format(cls="BarTest", m1="barOnly", m2="alsoBar"), sortKey=1)

        by = {t.functionName: t for t in cat.testCases.all()}
        self.assertEqual(set(by), {"thisIsAPascalCaseTest", "testEmptyArray", "barOnly", "alsoBar"})
        self.assertTrue(all(t.pointsPass == Decimal("1.00") for t in by.values()))
        # humanized default description, casing preserved
        self.assertEqual(by["thisIsAPascalCaseTest"].description, "This Is A Pascal Case Test")
        cat.refresh_from_db()
        self.assertEqual(cat.maxPoints, Decimal("4.00"))

    def test_resync_preserves_db_points_and_description(self):
        from core.models import TestCategoryFile
        cat = self._category()
        f = TestCategoryFile.objects.create(
            category=cat, name="FooTest.java",
            content=self.JUNIT.format(cls="FooTest", m1="alpha", m2="beta"), sortKey=0)

        t = cat.testCases.get(functionName="beta")
        t.pointsPass = Decimal("5.00")
        t.description = "My Custom Name"
        t.hidden = True
        t.save()

        # Re-saving the file re-syncs; instructor-owned fields must survive.
        f.save()
        t.refresh_from_db()
        self.assertEqual(t.pointsPass, Decimal("5.00"))
        self.assertEqual(t.description, "My Custom Name")
        self.assertTrue(t.hidden)
        cat.refresh_from_db()
        self.assertEqual(cat.maxPoints, Decimal("6.00"))  # 5 + 1

    def test_stale_test_deleted_when_removed_from_file(self):
        from core.models import TestCategoryFile
        cat = self._category()
        f = TestCategoryFile.objects.create(
            category=cat, name="FooTest.java",
            content=self.JUNIT.format(cls="FooTest", m1="keep", m2="drop"), sortKey=0)
        self.assertEqual(cat.testCases.count(), 2)

        f.content = (
            "import org.junit.jupiter.api.Test;\n"
            "public class FooTest {\n  @Test void keep() {}\n}\n"
        )
        f.save()
        self.assertEqual(
            sorted(t.functionName for t in cat.testCases.all()), ["keep"])

    def test_legacy_testscript_fallback(self):
        """A category with no testFiles still parses the legacy testScript."""
        from core.models import TestCategory
        cat = self._category()
        # TestCategoryFactory mutes post_save, so set the script then sync manually.
        cat.testScript = self.JUNIT.format(cls="LegacyTest", m1="one", m2="two")
        cat.save(update_fields=["testScript"])
        TestParsingService.update_test_cases(cat)
        self.assertEqual(
            sorted(t.functionName for t in cat.testCases.all()), ["one", "two"])
