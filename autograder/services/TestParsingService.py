# Copyright © 2026 Rutgers, the State University of New Jersey. All rights reserved except as defined by the Rutgers Non-Commercial License, included with this software.

import ast
import re
import logging
from typing import List, Dict, Any, Optional
from core.models import TestCategory, TestCase, LearningObjective

logger = logging.getLogger(__name__)

# Regex for inline codepost directives in comments preceding a test definition.
# Supports: # @codepost hidden objectives=recursion,edge-cases
# Works with //, #, --, /* and ` * ` (block-comment continuation) comment styles.
_DIRECTIVE_PATTERN = re.compile(
    r'(?://|#|--|/\*|\*)\s*@codepost\b(.*?)(?:\*/)?$', re.MULTILINE
)

# A real JUnit (Jupiter) test script imports org.junit.*; the legacy custom harness
# (@Test(name=,points=)) never does. Used to pick the Java sub-parser.
_JUNIT_IMPORT_RE = re.compile(r'^\s*import\s+org\.junit', re.MULTILINE)


def _parse_directives(script: str, test_start_pos: int) -> Dict[str, Any]:
    """
    Scan the line(s) immediately before test_start_pos for @codepost directives.
    Returns parsed directives like {'hidden': True, 'objectives': ['recursion'], 'points': 2.0}.
    """
    result: Dict[str, Any] = {}
    # Get the text before the test definition, limited to 10 lines back
    preceding = script[:test_start_pos]
    # Take last 500 chars max for efficiency
    preceding = preceding[-500:]
    lines = preceding.rstrip().split('\n')
    # Check up to 10 lines before. The window is wide (and annotation lines are
    # treated as continuations below) because in JUnit a `// @codepost points=N`
    # comment usually sits above a stack of annotations (@DisplayName, @Test).
    for line in reversed(lines[-10:]):
        stripped = line.strip()
        m = _DIRECTIVE_PATTERN.search(stripped)
        if m:
            directive_text = m.group(1).strip()
            if 'hidden' in directive_text:
                result['hidden'] = True
            # Accept both `objectives=foo,bar` and the looser `objectives = foo, bar, baz`.
            # Each value runs up to the next comma or whitespace; the list is bounded by the
            # first whitespace that isn't between commas (e.g. trailing `hidden` after the list).
            obj_match = re.search(r'objectives\s*=\s*([^\s,]+(?:\s*,\s*[^\s,]+)*)', directive_text)
            if obj_match:
                result['objectives'] = [o.strip() for o in obj_match.group(1).split(',') if o.strip()]
            pts_match = re.search(r'\bpoints\s*=\s*(\d+(?:\.\d+)?)', directive_text)
            if pts_match:
                result['points'] = float(pts_match.group(1))
            timeout_match = re.search(r'\btimeout\s*=\s*(\d+(?:\.\d+)?)', directive_text)
            if timeout_match:
                result['timeout'] = int(float(timeout_match.group(1)))
        elif stripped and not stripped.startswith(('#', '//', '--', '/*', '*', '@')):
            # Non-comment, non-annotation, non-empty line — stop scanning.
            # Java annotation lines (@Test, @DisplayName, @Timeout, ...) are allowed
            # to sit between the directive comment and the method without stopping us.
            break
    return result

class TestParsingService:
    @staticmethod
    def parse_script(test_category: TestCategory, language: Optional[str] = None) -> List[Dict[str, Any]]:
        """
        Parse the test script to identify @test decorated functions or equivalents.
        Returns a list of test definitions {name, points, etc}.
        """
        script = test_category.testScript
        if not script:
            return []

        # Normalize language if provided
        normalized_language = (language or "").lower()

        # Use explicit language if provided, otherwise detect
        if normalized_language.startswith('python') or (not language and "def " in script and "@test" in script):
            return TestParsingService._parse_python(script)
        elif normalized_language == 'java' or normalized_language.startswith('java-') or (not language and "@Test" in script):
            return TestParsingService._parse_java(script)
        elif normalized_language == 'r' or normalized_language.startswith('r-') or (not language and "run_test(" in script):
            return TestParsingService._parse_r(script)
        elif any(token in normalized_language for token in ['node', 'javascript', 'js', 'typescript']):
            return TestParsingService._parse_node(script)
        elif normalized_language.startswith('php'):
            return TestParsingService._parse_php(script)
        elif normalized_language.startswith('ruby') or normalized_language.startswith('rb'):
            return TestParsingService._parse_ruby(script)
        elif normalized_language in ['c/c++', 'c++', 'cpp', 'c'] or 'c++' in normalized_language or 'cpp' in normalized_language:
            return TestParsingService._parse_cpp(script)
        
        return []

    @staticmethod
    def _parse_python(script: str) -> List[Dict[str, Any]]:
        tests = []
        try:
            tree = ast.parse(script)
            for node in ast.walk(tree):
                if isinstance(node, ast.FunctionDef):
                    for decorator in node.decorator_list:
                        if isinstance(decorator, ast.Call) and getattr(decorator.func, 'id', '') == 'test':
                            # Found @test(...)
                            test_info = {'functionName': node.name, 'name': node.name, 'points': 0}
                            
                            # Parse args/keywords
                            if decorator.args:
                                # First arg is usually name (title)
                                if isinstance(decorator.args[0], ast.Constant):
                                    test_info['name'] = decorator.args[0].value
                                # Second positional arg may be points (e.g. @test("Name", 5))
                                if len(decorator.args) > 1 and isinstance(decorator.args[1], ast.Constant) and isinstance(decorator.args[1].value, (int, float)):
                                    test_info['points'] = decorator.args[1].value
                            
                            for keyword in decorator.keywords:
                                if keyword.arg == 'points':
                                    if isinstance(keyword.value, ast.Constant) and isinstance(keyword.value.value, (int, float)):
                                        test_info['points'] = keyword.value.value
                                elif keyword.arg == 'timeout':
                                    if isinstance(keyword.value, ast.Constant) and isinstance(keyword.value.value, (int, float)):
                                        test_info['timeout'] = int(keyword.value.value)
                                elif keyword.arg == 'description':
                                    if isinstance(keyword.value, ast.Constant):
                                         test_info['description'] = keyword.value.value
                                elif keyword.arg == 'name':
                                    if isinstance(keyword.value, ast.Constant):
                                         test_info['name'] = keyword.value.value
                                elif keyword.arg == 'hidden':
                                    if isinstance(keyword.value, ast.Constant) and isinstance(keyword.value.value, bool):
                                        test_info['hidden'] = keyword.value.value
                                elif keyword.arg == 'objectives':
                                    if isinstance(keyword.value, ast.List):
                                        test_info['objectives'] = [
                                            elt.value.strip() for elt in keyword.value.elts
                                            if isinstance(elt, ast.Constant) and isinstance(elt.value, str) and elt.value.strip()
                                        ]
                            
                            tests.append(test_info)
        except Exception as e:
            logger.error(f"Failed to parse Python script: {e}")
        return tests

    @staticmethod
    def _parse_java(script: str) -> List[Dict[str, Any]]:
        """
        Dispatch Java test parsing. Real JUnit (Jupiter) scripts import org.junit.*
        and use bare @Test; the legacy codePost harness uses @Test(name=,points=)
        with no JUnit import. Prefer JUnit when the import is present, otherwise fall
        back to the custom-annotation parser.
        """
        if _JUNIT_IMPORT_RE.search(script):
            return TestParsingService._parse_java_junit5(script)
        return TestParsingService._parse_java_custom(script)

    @staticmethod
    def _parse_java_junit5(script: str) -> List[Dict[str, Any]]:
        """
        Parse a standard JUnit 5/6 (Jupiter) test file. Each method annotated with a
        bare @Test (or @ParameterizedTest/@RepeatedTest) is one test; functionName is
        the method name (matches MethodSource.getMethodName() at runtime). @DisplayName
        becomes the display name; `// @codepost points=N` sets points (default 1.0);
        lifecycle methods (@BeforeEach etc.) are NOT tests.

        Uses javalang for an accurate AST when available; falls back to a careful
        regex otherwise. Either way, directives are read from the raw script text so
        comment placement is honored.
        """
        tests = TestParsingService._parse_java_junit5_ast(script)
        if tests is None:
            tests = TestParsingService._parse_java_junit5_regex(script)
        return tests

    _JUNIT_TEST_ANNOTATIONS = {'Test', 'ParameterizedTest', 'RepeatedTest', 'TestFactory', 'TestTemplate'}
    _JUNIT_LIFECYCLE_ANNOTATIONS = {'BeforeEach', 'AfterEach', 'BeforeAll', 'AfterAll', 'Disabled'}

    @staticmethod
    def _parse_java_junit5_ast(script: str) -> Optional[List[Dict[str, Any]]]:
        """AST-based JUnit parse via javalang. Returns None if javalang is unavailable
        or the source cannot be parsed (caller then uses the regex fallback)."""
        try:
            import javalang  # type: ignore[import-untyped]
        except Exception:
            return None
        try:
            tree = javalang.parse.parse(script)
        except Exception:
            return None

        tests: List[Dict[str, Any]] = []
        seen = set()
        for _path, node in tree.filter(javalang.tree.MethodDeclaration):
            ann_names = {a.name for a in (node.annotations or [])}
            if not (ann_names & TestParsingService._JUNIT_TEST_ANNOTATIONS):
                continue
            if ann_names & TestParsingService._JUNIT_LIFECYCLE_ANNOTATIONS:
                continue
            fname = node.name
            if fname in seen:
                continue
            seen.add(fname)

            info: Dict[str, Any] = {'functionName': fname, 'name': fname, 'points': 1.0}

            # @DisplayName("...") -> display name
            for ann in (node.annotations or []):
                if ann.name == 'DisplayName' and ann.element is not None:
                    val = getattr(ann.element, 'value', None)
                    if isinstance(val, str):
                        info['name'] = val.strip('"')

            # Directives from the comment(s) above the method/annotations.
            pos = TestParsingService._find_method_pos(script, fname)
            if pos is not None:
                info.update(_parse_directives(script, pos))

            tests.append(info)
        return tests

    @staticmethod
    def _find_method_pos(script: str, method_name: str) -> Optional[int]:
        """Char offset at which to scan for directives for a method: the start of the
        contiguous annotation block directly above the method declaration. Backing up
        past the annotations (and the method's own line) is required so
        `_parse_directives` doesn't stop at the non-comment method/annotation lines
        before reaching a `// @codepost ...` comment placed above the block."""
        m = re.search(r'\b' + re.escape(method_name) + r'\s*\(', script)
        if not m:
            return None
        lines = script.splitlines(keepends=True)
        # Find the line index containing the match offset.
        offset = 0
        method_line = 0
        for i, ln in enumerate(lines):
            if offset + len(ln) > m.start():
                method_line = i
                break
            offset += len(ln)
        # Walk upward over contiguous annotation lines (@...).
        start_line = method_line
        j = method_line - 1
        while j >= 0 and lines[j].strip().startswith('@'):
            start_line = j
            j -= 1
        return sum(len(lines[k]) for k in range(start_line))

    @staticmethod
    def _parse_java_junit5_regex(script: str) -> List[Dict[str, Any]]:
        """Regex fallback for JUnit parsing when javalang is unavailable.
        Matches a @Test-family annotation (bare, or with args for @ParameterizedTest)
        followed, possibly across other annotations, by a method declaration."""
        tests: List[Dict[str, Any]] = []
        seen = set()
        # @Test (optionally @Test(...) is NOT the custom form here because this path
        # only runs for JUnit-import scripts) then any stacked annotations, then the
        # method signature: [modifiers] <ret> name( ... ) [throws ...]
        pattern = re.compile(
            r'@(?:Test|ParameterizedTest|RepeatedTest|TestFactory|TestTemplate)\b[^\n]*'
            r'(?:\s*@[A-Za-z_][\w.]*(?:\([^)]*\))?[^\n]*)*'   # other stacked annotations
            r'\s*(?:public\s+|protected\s+|private\s+|static\s+|final\s+)*'
            r'[\w<>\[\],.\s?]+?\s+'                            # return type
            r'([A-Za-z_$][\w$]*)\s*\(',                        # method name
            re.DOTALL,
        )
        # Guard: skip matches whose method is actually a lifecycle method. We detect
        # that by checking the annotation block the match started on.
        for m in pattern.finditer(script):
            # Reject if a lifecycle annotation sits in the matched span.
            span_text = m.group(0)
            if re.search(r'@(?:BeforeEach|AfterEach|BeforeAll|AfterAll|Disabled)\b', span_text):
                continue
            fname = m.group(1)
            if fname in seen:
                continue
            seen.add(fname)
            info: Dict[str, Any] = {'functionName': fname, 'name': fname, 'points': 1.0}
            dn = re.search(r'@DisplayName\s*\(\s*"([^"]*)"\s*\)', span_text)
            if dn:
                info['name'] = dn.group(1)
            info.update(_parse_directives(script, m.start()))
            tests.append(info)
        return tests

    @staticmethod
    def _parse_java_custom(script: str) -> List[Dict[str, Any]]:
        # Naive regex parsing for Java @Test
        tests = []
        # Pattern: @Test(name="...", points=5) public <returnType> testName()
        # This is a simplification; a real parser would be better but expensive
        pattern = r'@Test\s*\((.*?)\)\s*public\s+[\w<>\[\]]+\s+(\w+)'
        matches = re.finditer(pattern, script, re.DOTALL)
        
        for match in matches:
            params = match.group(1)
            func_name = match.group(2)
            test_info = {'functionName': func_name, 'name': func_name, 'points': 0}
            
            # Extract name
            name_match = re.search(r'name\s*=\s*"([^"]+)"', params)
            if name_match:
                test_info['name'] = name_match.group(1)
            
            # Extract description
            desc_match = re.search(r'description\s*=\s*"([^"]+)"', params)
            if desc_match:
                test_info['description'] = desc_match.group(1)
                
            # Extract points
            points_match = re.search(r'points\s*=\s*(\d+(\.\d+)?)', params)
            if points_match:
                test_info['points'] = float(points_match.group(1))

            # Extract timeout
            timeout_match = re.search(r'timeout\s*=\s*(\d+(\.\d+)?)', params)
            if timeout_match:
                test_info['timeout'] = int(float(timeout_match.group(1)))

            # Extract hidden
            hidden_match = re.search(r'hidden\s*=\s*(true|false)', params, re.IGNORECASE)
            if hidden_match:
                test_info['hidden'] = hidden_match.group(1).lower() == 'true'

            # Extract objectives
            objectives_match = re.search(r'objectives\s*=\s*\{([^}]*)\}', params)
            if objectives_match:
                obj_str = objectives_match.group(1)
                test_info['objectives'] = [s.strip().strip('"') for s in obj_str.split(',') if s.strip().strip('"')]

            tests.append(test_info)
            
        return tests

    @staticmethod
    def _parse_r(script: str) -> List[Dict[str, Any]]:
        # Canonical R parser support:
        # run_test("Name", 5, "Desc", function() { ... }, 30)
        tests = []

        # Runtime aligned style:
        # run_test("Name", 5, "Desc", function() { ... }, 30)
        run_test_pattern = r'run_test\s*\(\s*(["\'])(.*?)\1\s*,\s*(\d+(?:\.\d+)?)\s*(?:,\s*(["\'])(.*?)\4)?\s*(?:,\s*function\b|\s*\))'
        for match in re.finditer(run_test_pattern, script, re.DOTALL):
            test_info = {
                'functionName': re.sub(r'\W+', '_', match.group(2)).strip('_').lower() or 'run_test',
                'name': match.group(2),
                'points': float(match.group(3)),
                'description': match.group(5) or "",
            }
            directives = _parse_directives(script, match.start())
            test_info.update(directives)
            tests.append(test_info)

        return tests

    @staticmethod
    def _parse_php(script: str) -> List[Dict[str, Any]]:
        tests = []
        # Pattern: Tester::test("Name", 10.0, "Desc", function() { ... }, 30)
        pattern = r"Tester::test\s*\(\s*([\"'])(.*?)\1\s*,\s*(\d+(?:\.\d+)?)\s*(?:,\s*([\"'])(.*?)\4)?\s*(?:,\s*function|\s*\))"
        matches = re.finditer(pattern, script, re.DOTALL)

        for match in matches:
            name = match.group(2)
            points = float(match.group(3))
            description = match.group(5) or ""

            test_info = {
                'functionName': re.sub(r'\W+', '_', name).strip('_').lower() or 'tester_test',
                'name': name,
                'points': points,
                'description': description,
            }
            directives = _parse_directives(script, match.start())
            test_info.update(directives)
            tests.append(test_info)

        return tests

    @staticmethod
    def _parse_ruby(script: str) -> List[Dict[str, Any]]:
        tests = []
        # Pattern: run_test("Name", 10, "Desc") do ... end
        #          run_test("Name", 10, "Desc", 30) do ... end
        pattern = r"run_test\s*\(\s*([\"'])(.*?)\1\s*,\s*(\d+(?:\.\d+)?)\s*(?:,\s*([\"'])(.*?)\4)?\s*(?:,\s*(\d+(?:\.\d+)?))?\s*\)"
        matches = re.finditer(pattern, script, re.DOTALL)

        for match in matches:
            name = match.group(2)
            points = float(match.group(3))
            description = match.group(5) or ""

            test_info = {
                'functionName': re.sub(r'\W+', '_', name).strip('_').lower() or 'run_test',
                'name': name,
                'points': points,
                'description': description,
                **({'timeout': int(float(match.group(6)))} if match.group(6) else {}),
            }
            directives = _parse_directives(script, match.start())
            test_info.update(directives)
            tests.append(test_info)

        return tests

    @staticmethod
    def _parse_node(script: str) -> List[Dict[str, Any]]:
        tests = []
        # Pattern (runtime-aligned):
        # test("Name", 5, "Desc", function() { ... });
        # test("Name", 5, "Desc", function() { ... }, 30);
        pattern = r"test\s*\(\s*([\"'])(.*?)\1\s*,\s*(\d+(?:\.\d+)?)\s*,\s*([\"'])(.*?)\4(?P<rest>[\s\S]*?)\)\s*;"
        matches = re.finditer(pattern, script, re.DOTALL)

        for match in matches:
            name = match.group(2)
            points = float(match.group(3))
            description = match.group(5) or ""
            rest = (match.group('rest') or '').strip()

            timeout_val = None
            timeout_match = re.search(r',\s*(\d+(?:\.\d+)?)\s*$', rest)
            if timeout_match:
                timeout_val = timeout_match.group(1)

            test_info = {
                'functionName': name,
                'name': name,
                'points': points,
                'description': description,
            }

            if timeout_val:
                test_info['timeout'] = int(float(timeout_val))

            directives = _parse_directives(script, match.start())
            test_info.update(directives)
            tests.append(test_info)

        return tests

    @staticmethod
    def _parse_cpp(script: str) -> List[Dict[str, Any]]:
        tests = []

        # Parse in script order across all supported macro variants.
        combined_pattern = r'''
            TEST_DESC_TIMEOUT\s*\(\s*(\w+)\s*,\s*(\d+(?:\.\d+)?)\s*,\s*"([^"]*)"\s*,\s*(\d+(?:\.\d+)?)\s*\)
            |TEST_TIMEOUT\s*\(\s*(\w+)\s*,\s*(\d+(?:\.\d+)?)\s*,\s*(\d+(?:\.\d+)?)\s*\)
            |TEST_DESC\s*\(\s*(\w+)\s*,\s*(\d+(?:\.\d+)?)\s*,\s*"([^"]*)"\s*\)
            |TEST\s*\(\s*(\w+)\s*,\s*(\d+(?:\.\d+)?)\s*\)
        '''

        for match in re.finditer(combined_pattern, script, re.DOTALL | re.VERBOSE):
            test_info: Optional[Dict[str, Any]] = None
            if match.group(1):
                # TEST_DESC_TIMEOUT(name, points, description, timeout)
                test_info = {
                    'functionName': match.group(1),
                    'name': match.group(1),
                    'points': float(match.group(2)),
                    'description': match.group(3),
                    'timeout': int(float(match.group(4)))
                }
            elif match.group(5):
                # TEST_TIMEOUT(name, points, timeout)
                test_info = {
                    'functionName': match.group(5),
                    'name': match.group(5),
                    'points': float(match.group(6)),
                    'timeout': int(float(match.group(7)))
                }
            elif match.group(8):
                # TEST_DESC(name, points, description)
                test_info = {
                    'functionName': match.group(8),
                    'name': match.group(8),
                    'points': float(match.group(9)),
                    'description': match.group(10)
                }
            elif match.group(11):
                # TEST(name, points)
                test_info = {
                    'functionName': match.group(11),
                    'name': match.group(11),
                    'points': float(match.group(12))
                }

            if test_info:
                directives = _parse_directives(script, match.start())
                test_info.update(directives)
                tests.append(test_info)

        return tests

    @staticmethod
    def update_test_cases(test_category: TestCategory):
        """
        Sync execution of parse_script with the database TestCase objects.
        """
        language = None
        try:
            environment = getattr(getattr(test_category, 'assignment', None), 'environment', None)
            language = getattr(environment, 'language', None)
        except Exception:
            language = None

        assignment = test_category.assignment

        logger.info(f"[TestParsingService] Syncing test cases for TestCategory {test_category.pk} (language={language})")
        parsed_tests = TestParsingService.parse_script(test_category, language=language)
        logger.info(f"[TestParsingService] Parsed {len(parsed_tests)} tests from script")

        if not parsed_tests:
            logger.warning(f"[TestParsingService] No tests parsed for TestCategory {test_category.pk}. "
                           f"Script empty={not test_category.testScript}")
            return

        # Get existing tests (keyed by functionName to match script source)
        current_tests = {t.functionName: t for t in test_category.testCases.all() if t.functionName}
        parsed_fnames = set()

        # Max length for the description CharField
        desc_max_length = TestCase._meta.get_field('description').max_length or 255
        
        for test_data in parsed_tests:
            fname = test_data['functionName']
            parsed_fnames.add(fname)
            
            # Prepare fields
            description = test_data.get('name', fname)
            if description == fname:
                description = fname.replace('_', ' ').title()
            
            # Truncate description to model max_length
            description = description[:desc_max_length]
            explanation = test_data.get('description', "")

            try:
                if fname in current_tests:
                    # Update existing
                    t = current_tests[fname]
                    t.description = description
                    t.explanation = explanation
                    t.pointsPass = test_data.get('points', 0)
                    t.timeout = test_data.get('timeout', 30)
                    t.hidden = test_data.get('hidden', False)
                    t.save()
                    logger.info(f"[TestParsingService] Updated test case: {fname}")

                    # Sync learning objectives
                    TestParsingService._sync_test_objectives(t, test_data, assignment)
                else:
                    # Create new
                    t = TestCase.objects.create(
                        testCategory=test_category,
                        functionName=fname,
                        description=description,
                        explanation=explanation,
                        pointsPass=test_data.get('points', 0),
                        timeout=test_data.get('timeout', 30),
                        hidden=test_data.get('hidden', False),
                        type='script' # Default type for script-based tests
                    )
                    logger.info(f"[TestParsingService] Created test case: {fname}")

                    # Sync learning objectives
                    TestParsingService._sync_test_objectives(t, test_data, assignment)
            except Exception as e:
                logger.error(f"[TestParsingService] Failed to create/update test case '{fname}': {e}")
        
        # Delete obsolete tests
        # Tests that exist in DB (with functionName) but are NOT in the parsed script
        for fname, test_case in current_tests.items():
            if fname not in parsed_fnames:
                logger.info(f"[TestParsingService] Removing obsolete test case: {fname} (ID: {test_case.pk})")
                test_case.delete()
        
        # Calculate total max points from parsed tests
        total_points = sum(t.get('points', 0) for t in parsed_tests)
        
        # Update TestCategory maxPoints without triggering save signals
        TestCategory.objects.filter(pk=test_category.pk).update(maxPoints=total_points)
        logger.info(f"[TestParsingService] Sync complete for TestCategory {test_category.pk}: "
                    f"{len(parsed_tests)} tests, {total_points} total points")

    @staticmethod
    def _sync_test_objectives(test_case: TestCase, test_data: Dict[str, Any], assignment) -> None:
        """
        Auto-create and link LearningObjective records based on parsed objectives list.
        Skips the M2M write entirely when the linked set hasn't changed, to avoid the
        clear+set churn that would otherwise fire on every category save.
        """
        objective_ids = [oid for oid in test_data.get('objectives', []) if oid and oid.strip()]

        current_short_ids = set(test_case.learningObjectives.values_list('shortId', flat=True))
        desired_short_ids = set(objective_ids)

        if current_short_ids == desired_short_ids:
            return

        if not desired_short_ids:
            test_case.learningObjectives.clear()
            return

        objectives = []
        for short_id in objective_ids:
            obj, created = LearningObjective.objects.get_or_create(
                shortId=short_id,
                assignment=assignment,
                defaults={
                    'name': short_id.replace('-', ' ').replace('_', ' ').title(),
                }
            )
            if created:
                logger.info(f"[TestParsingService] Auto-created learning objective: {short_id}")
            objectives.append(obj)

        test_case.learningObjectives.set(objectives)
