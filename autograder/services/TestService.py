# Copyright © 2026 Rutgers, the State University of New Jersey. All rights reserved except as defined by the Rutgers Non-Commercial License, included with this software.
import logging
import re
from decimal import Decimal
from typing import Optional, Dict, Any, List

from core.models import TestCase, TestCategory, Submission, SubmissionTest, File, CachedExecutionResult
from autograder.services.executors import Executor

logger = logging.getLogger(__name__)

class TestService:
    """
    Service to orchestrate test execution using the Modern Framework (Executors).
    Replaces the legacy parse.py -> external autograder flow.
    """

    @staticmethod
    def _sanitize_overrides(file_overrides: Optional[Dict[Any, str]]) -> Dict[int, str]:
        """Ensure file_overrides keys are integers (Celery may serialize them as strings)"""
        if not file_overrides:
            return {}
        try:
            return {int(k): v for k, v in file_overrides.items()}
        except (ValueError, TypeError):
            return {}

    @staticmethod
    def _to_json_safe(value: Any) -> Any:
        """Recursively convert values to JSON-serializable primitives for JSONField writes."""
        if isinstance(value, Decimal):
            return float(value)
        if isinstance(value, dict):
            return {k: TestService._to_json_safe(v) for k, v in value.items()}
        if isinstance(value, list):
            return [TestService._to_json_safe(v) for v in value]
        if isinstance(value, tuple):
            return [TestService._to_json_safe(v) for v in value]
        return value

    @staticmethod
    def _looks_like_syntax_or_compile_error(text: str) -> bool:
        if not text:
            return False

        patterns = [
            r"\bSyntaxError\b",
            r"\bIndentationError\b",
            r"\bTabError\b",
            r"invalid syntax",
            r"unexpected EOF while parsing",
            r"unexpected token",
            r"Error:\s*Unexpected",
            r"reached end of file while parsing",
            r"';' expected",
            r"\berror:\b.*\b(expected|before)\b",
            r"compilation failed",
            r"failed to compile",
        ]

        return any(re.search(pattern, text, re.IGNORECASE) for pattern in patterns)

    @staticmethod
    def _looks_like_secondary_undefined_error(text: str) -> bool:
        if not text:
            return False

        patterns = [
            r"\bNameError\b",
            r"\bReferenceError\b",
            r"is not defined",
            r"cannot find symbol",
            r"undefined variable",
        ]

        return any(re.search(pattern, text, re.IGNORECASE) for pattern in patterns)

    @staticmethod
    def _detect_syntax_hint(execution_result: Dict[str, Any]) -> Optional[str]:
        """
        Detect whether the primary failure likely came from a syntax/parse/compile issue
        in student code and return a concise instructor-facing hint.
        """
        stdout = execution_result.get('stdout', '') or ''
        stderr = execution_result.get('stderr', '') or ''
        err = execution_result.get('error', '') or ''
        notebook_cell_errors = TestService._collect_notebook_cell_error_text(execution_result)

        combined = f"{stderr}\n{stdout}\n{err}\n{notebook_cell_errors}".strip()
        if not combined:
            return None

        # Detect test script crash (instructor-side error, not student code)
        if 'Test Script Error' in combined:
            first_line = next(
                (
                    line.strip()
                    for line in combined.splitlines()
                    if any(kw in line for kw in ('Error', 'Exception', 'Import', 'Module'))
                    and 'Test Script Error' not in line
                ),
                'Test script failed to load.',
            )
            return (
                'The test script itself crashed before tests could run. '
                'This is likely an issue with the test script, not with student code.\n'
                f"Root cause: {first_line}"
            )

        # Prefer explicit student-code crash marker emitted by templates
        if 'Student Code Runtime Error' in combined and TestService._looks_like_syntax_or_compile_error(combined):
            first_line = next(
                (
                    line.strip()
                    for line in combined.splitlines()
                    if TestService._looks_like_syntax_or_compile_error(line)
                ),
                'Syntax/parse error detected while loading student code.',
            )
            return (
                'Student code has a syntax/parse error, so tests may fail secondarily '
                f"(e.g., undefined names).\nRoot cause: {first_line}"
            )

        if TestService._looks_like_syntax_or_compile_error(combined):
            first_line = next(
                (
                    line.strip()
                    for line in combined.splitlines()
                    if TestService._looks_like_syntax_or_compile_error(line)
                ),
                'Syntax/parse/compile error detected.',
            )
            return (
                'Detected a likely syntax/parse/compile error in student code before '
                f"test assertions could run cleanly.\nRoot cause: {first_line}"
            )

        return None

    @staticmethod
    def _collect_notebook_cell_error_text(execution_result: Dict[str, Any]) -> str:
        """Collect notebook cell-level stderr/error text for syntax hint detection."""
        output_data = execution_result.get('output_data') or {}
        cells = output_data.get('cells') or []
        if not isinstance(cells, list):
            return ''

        chunks: List[str] = []
        for idx, cell in enumerate(cells):
            if not isinstance(cell, dict):
                continue
            if cell.get('cell_type') != 'code':
                continue

            cell_idx = cell.get('idx')
            if isinstance(cell_idx, int):
                cell_label = f"Notebook cell {cell_idx + 1}"
            else:
                cell_label = f"Notebook cell {idx + 1}"

            cell_source_raw = cell.get('source') or ''
            if isinstance(cell_source_raw, list):
                cell_source = ''.join(str(line) for line in cell_source_raw)
            else:
                cell_source = str(cell_source_raw)

            source_lines = cell_source.splitlines()
            numbered_source = '\n'.join(
                f"{line_no + 1:>4} | {line_text}"
                for line_no, line_text in enumerate(source_lines[:80])
            )
            if len(source_lines) > 80:
                numbered_source += '\n... [cell source truncated]'

            outputs = cell.get('outputs') or []
            if not isinstance(outputs, list):
                continue

            for output in outputs:
                if not isinstance(output, dict):
                    continue

                output_type = output.get('output_type')

                if output_type == 'error':
                    ename = str(output.get('ename') or '')
                    evalue = str(output.get('evalue') or '')
                    traceback = output.get('traceback') or []
                    if isinstance(traceback, list):
                        traceback_text = '\n'.join(str(x) for x in traceback)
                    else:
                        traceback_text = str(traceback)
                    detail = (
                        f"{cell_label}\n"
                        f"{ename}\n{evalue}\n{traceback_text}\n"
                        f"Source:\n{numbered_source}"
                    ).strip()
                    chunks.append(detail)
                elif output_type == 'stream' and str(output.get('name') or '').lower() == 'stderr':
                    stderr_text = str(output.get('text') or '')
                    if stderr_text.strip():
                        chunks.append(f"{cell_label}\n{stderr_text}\nSource:\n{numbered_source}".strip())

        return '\n'.join(chunk for chunk in chunks if chunk)

    @staticmethod
    def _annotate_tests_with_syntax_hint(tests: List[Dict[str, Any]], syntax_hint: Optional[str]) -> List[Dict[str, Any]]:
        if not syntax_hint:
            return tests

        annotated: List[Dict[str, Any]] = []
        for test in tests:
            t = dict(test)
            if t.get('passed', False):
                annotated.append(t)
                continue

            existing_error = str(t.get('error') or '').strip()

            # Only annotate tests that already look syntax-related (or classic secondary undefined fallout).
            # This avoids incorrectly labeling unrelated assertion failures in other files/tests.
            should_attach_hint = False
            if existing_error:
                should_attach_hint = (
                    TestService._looks_like_syntax_or_compile_error(existing_error)
                    or TestService._looks_like_secondary_undefined_error(existing_error)
                )
            else:
                message_text = str(t.get('message') or '')
                should_attach_hint = (
                    TestService._looks_like_syntax_or_compile_error(message_text)
                    or TestService._looks_like_secondary_undefined_error(message_text)
                )

            if not should_attach_hint:
                annotated.append(t)
                continue

            if existing_error:
                if syntax_hint not in existing_error:
                    t['error'] = f"{syntax_hint}\n\n{existing_error}"
            else:
                t['error'] = syntax_hint

            if not t.get('message'):
                t['message'] = 'Fix syntax/parse errors in student code first, then rerun tests.'

            if not t.get('status') or t.get('status') == 'failed':
                t['status'] = 'error'

            annotated.append(t)

        return annotated

    @staticmethod
    def run_suite(submission_id: int, test_case_ids: Optional[List[int]] = None, user_id: Optional[str] = None, file_overrides: Optional[Dict[int, str]] = None) -> List[Dict[str, Any]]:
        file_overrides = TestService._sanitize_overrides(file_overrides)
        """
        Runs a suite of tests for a submission.
        Optimized to group tests by Category and run each category script only once.
        """
        _results = []
        try:
            submission = Submission.objects.get(id=submission_id)
            assignment = submission.assignment

            # 1. Identify relevant TestCategories
            if test_case_ids:
                # specific tests requested
                target_tests = TestCase.objects.filter(id__in=test_case_ids).select_related('testCategory')
            else:
                # all tests for assignment
                target_tests = TestCase.objects.filter(testCategory__assignment=assignment).select_related('testCategory')
            
            if not target_tests.exists():
                return []

            logger.info(f"Running suite of {target_tests.count()} tests for submission {submission_id}")

            # 2. Ingestion Phase (Pre-calculated results)
            TestService.ingest_test_results(submission)

            # 3. Group by Category
            category_map = {} # category_id -> list of Reference TestCase objects
            for test in target_tests:
                cat_id = test.testCategory_id  # type: ignore[attr-defined]  # Django FK _id accessor
                if cat_id not in category_map:
                    category_map[cat_id] = {
                        'category': test.testCategory,
                        'tests': []
                    }
                category_map[cat_id]['tests'].append(test)

            # 4. Execute per Category
            all_results = []
            
            for _cat_id, data in category_map.items():
                category = data['category']
                category_tests = data['tests'] # Tests we explicitly want results for
                
                # Check if this category uses a script (modern) or individual tests (legacy/unit)
                # If parsed tests exist, it implies a script.
                if category.testScript:
                    # Run ONCE for the category
                    # We pick the first test as a "representative" to trigger the run, 
                    # but we really just need the category context.
                    # However, _run_ephemeral_execution currently takes a test_case.
                    # We should probably pass one test_case to satisfy the signature, 
                    # and rely on the Executor to run the WHOLE script (which it does).
                    
                    # Smart Representative Selection:
                    # Find the most common target file to avoid outliers (e.g. one test targeting wrong file)
                    # changing the executor type for the whole category.
                    from collections import Counter
                    
                    # We need to resolve target file for each test to check distinctness
                if category_tests:
                    logger.info(f"DEBUG: Category '{category_tests[0].testCategory.name}' Expected Tests: {[t.functionName for t in category_tests]}")
                    
                    # Optimization: Check test.fileName first (explicit target).
                    
                    target_counts = Counter()  # noqa: F841
                    # Determine target file
                    most_common_fname = None
                    category = category_tests[0].testCategory # Assuming category_tests is not empty here

                    if category.targetFileName:
                        most_common_fname = category.targetFileName
                    else:
                        # Fallback to a generic name if no files in assignment
                        assignment_files = list(submission.assignment.files.all())
                        if assignment_files:
                            most_common_fname = assignment_files[0].name
                        else:
                            most_common_fname = "python.py" 

                    # Find a representative test case
                    representative_test = category_tests[0]
                    
                    # Manual file lookup to ensure we respect most_common_fname override
                    target_file_obj = None
                    submission_files = list(submission.files.all())
                    if most_common_fname:
                        target_file_obj = next((f for f in submission_files if f.name == most_common_fname), None)

                    # If the category explicitly targets a file and it's missing from submission,
                    # do NOT fallback to any other file (that produces misleading syntax errors).
                    if category.targetFileName and not target_file_obj:
                        missing_msg = (
                            f"Target file '{category.targetFileName}' was not found in submission. "
                            "This test category was not executed."
                        )

                        for test in category_tests:
                            TestService._save_test_result(
                                submission,
                                test,
                                False,
                                0,
                                missing_msg,
                                True,
                                test.pointsPass,
                                [
                                    {
                                        "name": test.functionName,
                                        "passed": False,
                                        "score": 0,
                                        "max_score": test.pointsPass,
                                        "status": "error",
                                        "error": missing_msg,
                                        "message": "Required submission file is missing.",
                                    }
                                ],
                            )

                            all_results.append(
                                {
                                    "success": False,
                                    "error": missing_msg,
                                    "testCaseId": test.id,
                                    "testCaseDescription": test.description,
                                }
                            )

                        # Skip execution for this category entirely.
                        continue
                    
                    if not target_file_obj:
                         # Fallback to test case logic if not found (or if explicit target is missing from submission)
                         target_file_obj = TestService._get_target_file(submission, representative_test)

                    if not target_file_obj:
                        continue

                    # Run execution
                    exec_result_dict = TestService._run_ephemeral_execution(
                        target_file_obj, # Use manual lookup
                        representative_test,
                        user_id,
                        file_overrides=file_overrides,
                        test_function=None # Disable filtering to run ALL tests
                    )
                    
                    # 5. Process Results
                    # The executor returns a list of individual test results in 'tests' key
                    # Format: [{name: 'test_foo', passed: True, score: 5, ...}, ...]
                    raw_test_results = exec_result_dict.get('tests', [])
                    syntax_hint = TestService._detect_syntax_hint(exec_result_dict)
                    raw_test_results = TestService._annotate_tests_with_syntax_hint(raw_test_results, syntax_hint)
                    
                    logger.info(f"DEBUG: run_suite received {len(raw_test_results)} results from executor.")
                    for r in raw_test_results:
                        logger.info(f"DEBUG: Result: {r.get('name')} - {r.get('status')}")

                    # Map raw results to DB TestCases
                    # We want to match by functionName
                    
                    # Create a map of functionName -> TestCase for this category
                    # Get ALL tests for this category to maximize coverage, even if not requested?
                    # Yes, might as well update all if we have the data.
                    all_category_tests = category.testCases.all()
                    db_test_map = {t.functionName: t for t in all_category_tests}
                    
                    processed_ids = set()
                    

                    # Handle missing tests logic with Auto-Sync
                    stdout_log = exec_result_dict.get('stdout', '')
                    stderr_log = exec_result_dict.get('stderr', '')
                    combined_log = (stdout_log + "\n" + stderr_log).strip()
                    if len(combined_log) > 1000:
                        combined_log = combined_log[:1000] + "... (truncated)"

                    # 5a. Determine Sync Eligibility
                    script_success = exec_result_dict.get('success', False)
                    script_crashed = False
                    # Check for synthetic crash test
                    for r in raw_test_results:
                        if r.get('name') == "Test Script Execution" and r.get('status') == 'error':
                            script_crashed = True
                            break

                    # Fallback: if no results at all and stderr contains the crash marker,
                    # treat it as a crash even if the synthetic result wasn't parsed
                    if not script_crashed and not raw_test_results and 'Test Script Error:' in stderr_log:
                        script_crashed = True
                    
                    should_sync = script_success and not script_crashed
                    
                    # 5b. Process Results & Create New Tests
                    for raw_res in raw_test_results:
                        func_name = raw_res.get('name') 
                        if not func_name: continue

                        # Robust Matching Logic
                        test_case = db_test_map.get(func_name)
                        
                        if not test_case:
                             logger.info(f"DEBUG: No exact match for '{func_name}'. Trying fuzzy match.")
                             # Try description match
                             test_case = next((t for t in all_category_tests if t.description == func_name), None)
                        
                        if not test_case:
                             # Try normalized match
                             def normalize(s): return s.lower().replace('_', '').replace(' ', '')
                             norm_func = normalize(func_name)
                             test_case = next((t for t in all_category_tests if normalize(t.functionName) == norm_func or normalize(t.description) == norm_func), None)

                        if not test_case:
                            logger.info(f"DEBUG: Still no match for '{func_name}'. Should sync: {should_sync}")
                            # Still no match -> Create New Test (if syncing)
                            if should_sync:
                                try:
                                    logger.info(f"Auto-creating new test case '{func_name}' for category '{category.name}'")
                                    test_case = TestCase.objects.create(
                                        testCategory=category,
                                        functionName=func_name,
                                        description=raw_res.get('description', func_name),
                                        pointsPass=raw_res.get('max_score', 1.0),
                                        type='script', # Assume script type

                                    )
                                    # Add to tracking
                                    all_category_tests = list(all_category_tests) # Cast to list if queryset
                                    all_category_tests.append(test_case)
                                    processed_ids.add(test_case.id)
                                except Exception as e:
                                    logger.error(f"Failed to auto-create test case {func_name}: {e}")
                                    continue
                            else:
                                continue
                        
                        logger.info(f"DEBUG: Processing result for test '{test_case.functionName}' (ID: {test_case.id})")
                        processed_ids.add(test_case.id)
                        
                        # Save result
                        submission_test = TestService._save_test_result(
                            submission, 
                            test_case, 
                            raw_res.get('passed', False),
                            raw_res.get('score', 0),
                            raw_res.get('error') or raw_res.get('output', ''), # logs
                            raw_res.get('status', 'failed') == 'error', # isError
                            raw_res.get('max_score', test_case.pointsPass),
                            [raw_res] # results list
                        )
                        
                        # Add to return list if it was requested OR if it's new
                        if test_case in category_tests or test_case.id not in [t.id for t in category_tests]:
                            result_data = {
                                "success": True,
                                "passed": submission_test.passed,
                                "logs": submission_test.logs,
                                "isError": submission_test.isError,
                                "testCaseId": test_case.id,
                                "testCaseDescription": test_case.description,
                                "output_data": exec_result_dict.get('output_data', {})
                            }
                            all_results.append(result_data)

                    # 5c. Delete Stale Tests
                    if should_sync:
                        for test in all_category_tests:
                            if test.id not in processed_ids:
                                logger.info(f"Auto-deleting stale test case '{test.functionName}' (id={test.id}) for category '{category.name}'")
                                test.delete()
                                # Do NOT report error for deleted test
                    
                    else:
                        # REPORT MISSING IF NOT SYNCING (or if crash prevented sync)
                        for test in category_tests:
                            if test.id not in processed_ids:
                                 # It was requested but we didn't get a result
                                 if script_crashed:
                                     error_msg = f"Test script failed to execute. Tests were not run.\nExpected: {test.functionName} ({test.description})\n"
                                     # Include truncated stderr so the instructor sees the traceback
                                     if stderr_log:
                                         snippet = stderr_log[:2000]
                                         if len(stderr_log) > 2000:
                                             snippet += "\n... (truncated)"
                                         error_msg += f"\nScript error output:\n{snippet}"
                                 else:
                                     error_msg = f"Test did not execute or name mismatch.\nExpected: {test.functionName} ({test.description})\n"
                                 
                                     if raw_test_results:
                                         # Show available results
                                         available_names = [r.get('name', '') for r in raw_test_results]
                                         error_msg += f"Available Results: {', '.join(available_names)}\n"
                                         # Only omit logs when other results exist (contamination risk)
                                         error_msg += (
                                             "\nCategory execution log omitted for this test-level error to avoid "
                                             "cross-test contamination."
                                         )
                                     else:
                                         error_msg += "No test results returned by script.\n"
                                         # No results at all — safe to include stderr since there's
                                         # nothing to contaminate; the entire script produced no output
                                         if stderr_log:
                                             snippet = stderr_log[:2000]
                                             if len(stderr_log) > 2000:
                                                 snippet += "\n... (truncated)"
                                             error_msg += f"\nExecution output:\n{snippet}"

                                 # Save error result to DB so frontend sees it
                                 TestService._save_test_result(
                                     submission,
                                     test,
                                     False, 
                                     0, 
                                     error_msg, 
                                     True, 
                                     test.pointsPass
                                 )

                                 all_results.append({
                                     "success": False,
                                     "error": error_msg,
                                     "testCaseId": test.id,
                                     "testCaseDescription": test.description
                                 })

                else:
                    logger.info(f"DEBUG: Category '{category.name}' has no testScript. Falling back to individual test execution.")
                    # Legacy or non-script tests: Run individually
                    for test in category_tests:
                        try:
                            result = TestService.run_test(test.id, submission.id, user_id)
                            result['testCaseId'] = test.id
                            result['testCaseDescription'] = test.description
                            all_results.append(result)
                        except Exception as e:
                            logger.exception(f"Error running individual test {test.id} in suite")
                            all_results.append({
                                "success": False,
                                "error": str(e),
                                "testCaseId": test.id,
                                "testCaseDescription": test.description
                            })

            return all_results

        except Exception as e:
            logger.exception(f"Error running test suite for submission {submission_id}")
            return [{"success": False, "error": str(e)}]

    @staticmethod
    def run_test(test_case_id: int, submission_id: int, user_id: Optional[str] = None, file_overrides: Optional[Dict[int, str]] = None) -> Dict[str, Any]:
        file_overrides = TestService._sanitize_overrides(file_overrides)
        """
        Orchestrates the running of a single test case against a submission.
        """
        try:
            test_case = TestCase.objects.get(id=test_case_id)
            submission = Submission.objects.get(id=submission_id)

            if hasattr(test_case, 'testCategory') and test_case.testCategory and test_case.testCategory.targetFileName:
                required_name = test_case.testCategory.targetFileName
                if not submission.files.filter(name=required_name).exists():
                    return {
                        "success": False,
                        "error": (
                            f"Target file '{required_name}' was not found in submission. "
                            "This test cannot run until the required file is submitted."
                        ),
                    }
            
            # 1. Identify Target File
            target_file = TestService._get_target_file(submission, test_case)
            if not target_file:
                 return {
                     "success": False,
                     "error": f"No suitable file found for test case in category '{test_case.testCategory.name}'"
                 }

            # 2. Get Execution Result (Cached or Fresh)
            # Hybrid Logic: If test case has input or specific dataset, run ephemeral execution.
            # 2. Get Execution Result (Cached or Fresh)
            # Hybrid Logic: If test case has dataset OR is a script test (injects code), run ephemeral execution.
            # Script tests depend on the injected testCode, so they cannot use the generic file cache.
            # Hybrid Logic: If test case is a script test (injects code) or uses category resources, run ephemeral execution.
            # Script tests depend on the injected testCode, so they cannot use the generic file cache.
            if test_case.type == 'script' or (hasattr(test_case, 'testCategory') and test_case.testCategory.resources.exists()):  # type: ignore[attr-defined]  # Django reverse relation
                 # Pass test_function to run ONLY this test
                 execution_result = TestService._run_ephemeral_execution(target_file, test_case, user_id, file_overrides=file_overrides, test_function=test_case.functionName)
            else:
                # If we have overrides, we MUST force ephemeral execution even for unit tests
                if file_overrides and target_file.id in file_overrides:
                     execution_result = TestService._run_ephemeral_execution(target_file, test_case, user_id, file_overrides=file_overrides, test_function=test_case.functionName)
                else:
                    execution_result = TestService._get_or_run_execution(target_file, user_id)
            
            # 3. Verify Result
            if test_case.type == 'unit':
                 verification = TestService.verify_unit_test(test_case, execution_result)
            elif test_case.type == 'script':
                 verification = TestService.verify_script_test(test_case, execution_result)
            else:
                return {"success": False, "error": f"Unsupported or Deprecated test type: {test_case.type}"}

            # 4. Save SubmissionTest Result
            # We use update_or_create to avoid duplicates for the same run
            submission_test, created = SubmissionTest.objects.update_or_create(
                submission=submission,
                testCase=test_case,
                defaults={
                    "passed": verification['passed'],
                    "logs": verification['logs'],
                    "isError": verification.get('isError', False),
                    "score": verification.get('score', 0),
                    "maxScore": verification.get('maxScore', 0),
                    "results": verification.get('results', []),
                }
            )

            # 5. Apply Rubric Outcomes (Sync)
            TestService._sync_rubric_outcome(submission, test_case, submission_test.passed, target_file)

            return {
                "success": True,
                "passed": submission_test.passed,
                "logs": submission_test.logs,
                "isError": submission_test.isError,
                "cached": execution_result.get('cached', False),
                "output_data": execution_result.get('output_data', {})
            }

        except Exception as e:
            logger.exception(f"Error running test {test_case_id} for submission {submission_id}")
            return {
                "success": False, 
                "error": str(e)
            }

    @staticmethod
    def ingest_test_results(submission: Submission):
        """
        Scans submission directory for _test_<Category>.txt files and ingests results.
        Expected format: CSV/JSON mapping functionName -> result.
        """
        # This would require accessing the file system or cached execution results that produced these files.
        # Since we are in the API, we might need to rely on the Executor having captured these files 
        # as output_data or look up the File objects if they were uploaded.
        
        # Strategy: Look for SubmissionFile objects matching the pattern
        # This supports the "uploaded by instructor/student" use case.
        # For "generated by script", the script execution itself should ideally parse and return the results,
        # OR the script generates a file that we then read.
        
        import re
        files = submission.files.all()
        for f in files:
            # Pattern: _test_<CategoryName>.txt
            match = re.match(r'_test_(.+)\.txt', f.name, re.IGNORECASE)
            if match:
                category_name = match.group(1)
                try:
                    category = submission.assignment.testCategories.get(name__iexact=category_name)
                    TestService._parse_and_save_test_file(f, category, submission)
                except Exception as e:
                    logger.warning(f"Failed to ingest test file {f.name}: {e}")

    @staticmethod
    def _parse_and_save_test_file(file: File, category: 'TestCategory', submission: Submission):
         """Parses a _test.txt file and updates/creates submission tests."""
         # content might be bytes or str depending on storage
         content = file.data
         if isinstance(content, bytes):
             try:
                 content = content.decode('utf-8')
             except UnicodeDecodeError:
                 # Fallback to latin-1 if utf-8 fails, or ignore errors
                 content = content.decode('utf-8', errors='replace')
         
         if not content:
             return

         import json
         
         # Try JSON first
         try:
             json_data = json.loads(content)
             # Expected JSON format: { "functionName": { "passed": true, "score": 1.0, "message": "..." } }
             # OR List: [ { "name": "functionName", "passed": true ... } ]
             
             items = []
             if isinstance(json_data, dict):
                 for k, v in json_data.items():
                     if isinstance(v, dict):
                         v['name'] = k
                         items.append(v)
             elif isinstance(json_data, list):
                 items = json_data
                 
             for item in items:
                 func_name = item.get('name')
                 if not func_name:
                     continue
                     
                 try:
                     test_case = category.testCases.get(functionName=func_name)
                 except TestCase.DoesNotExist:
                     continue
                 
                 passed = bool(item.get('passed', False))
                 score = float(item.get('score', test_case.pointsPass if passed else test_case.pointsFail))
                 message = item.get('message') or item.get('logs') or ""
                 
                 SubmissionTest.objects.update_or_create(
                     submission=submission,
                     testCase=test_case,
                     defaults={
                         'passed': passed,
                         'logs': message,
                         'score': score
                     }
                 )
             return
         except json.JSONDecodeError:
             pass # Fallback to CSV

         # Simple CSV parser
         # Format: functionName, passed, score, message
         lines = content.splitlines()
         for line in lines:
             parts = [p.strip() for p in line.split(',')]
             if len(parts) >= 2:
                 func_name = parts[0]
                 passed_raw = parts[1]
                 
                 # Resolve TestCase
                 try:
                     test_case = category.testCases.get(functionName=func_name)
                 except TestCase.DoesNotExist:
                     continue
                 
                 passed = passed_raw.lower() in ['true', '1', 'pass']
                 
                 score_val = test_case.pointsPass if passed else test_case.pointsFail
                 if len(parts) > 2 and parts[2]:
                     try:
                         score_val = float(parts[2])
                     except ValueError:
                         pass
                         
                 message = parts[3] if len(parts) > 3 else ""
                 
                 # Save result
                 SubmissionTest.objects.update_or_create(
                     submission=submission,
                     testCase=test_case,
                     defaults={
                         'passed': passed,
                         'logs': message,
                         'score': score_val
                     }
                 )

    @staticmethod
    def _sync_rubric_outcome(submission: Submission, test_case: TestCase, passed: bool, target_file: File):
        """
        Syncs the test result with Rubric Comments.
        If test fails -> Ensure Rubric Comment is applied (Create Comment).
        If test passes -> Ensure Rubric Comment is NOT applied (Delete Comment).
        """
        if not test_case.rubricItem:
            return

        from core.models import Comment

        # Find existing comments on this file linked to this rubric item
        # We assume one comment per rubric item per file is sufficient.
        existing_comments = Comment.objects.filter(
            file=target_file,
            rubricComment=test_case.rubricItem
        )

        if passed:
            # If test passed, remove the deduction (comment)
            if existing_comments.exists():
                existing_comments.delete()
        else:
            # If test failed, ensure deduction exists
            if not existing_comments.exists():
                # Create a new comment
                # Author: First admin of the course (Fallback)
                author = submission.assignment.course.courseAdmins.first()
                if not author:
                     # Fallback if no admins? Rare.
                     # Maybe allow null (if I changed model) or find a system user.
                     # For now, log warning and skip?
                     logger.warning(f"No admin found for course {submission.assignment.course.id}. Cannot create rubric comment.")
                     return

                Comment.objects.create(
                    file=target_file,
                    rubricComment=test_case.rubricItem,
                    text=test_case.rubricItem.text, # Pre-fill text
                    author=author,
                    startLine=1,
                    endLine=1,
                    startChar=0,
                    endChar=0
                )

    @staticmethod
    def _get_target_file(submission: Submission, test_case: TestCase) -> Optional[File]:
        """
        Finds the file in the submission that this test case targets.
        """
        files = submission.files.all()
        
        # If Category defines a specific target file, use it
        if hasattr(test_case, 'testCategory') and test_case.testCategory.targetFileName:
             cat_target = test_case.testCategory.targetFileName
             for f in files:
                if f.name == cat_target:
                    return f
        
        # Heuristic: Find first executable file (based on extension)
        # TODO: Refine this heuristic or enforce explicit selection
        for f in files:
            # Simple check for code files
            if any(f.name.endswith(ext) for ext in ['.py', '.java', '.c', '.cpp', '.js', '.R', '.ipynb']):
                return f
                
        return files.first() if files.exists() else None

    @staticmethod
    def _get_or_run_execution(file: File, user_id: Optional[str] = None) -> Dict[str, Any]:
        """
        Retrieves cached result or runs the executor.
        Returns a dict resembling ExecutionResult but with a 'cached' flag.
        """
        # Check Cache
        cached = CachedExecutionResult.get_cached_result(file)
        if cached:
            from autograder.services.execution_events import record_execution_event
            record_execution_event(trigger='test_run', cached=True, success=True, file=file)
            return {
                "success": True, # Cached results imply successful execution, usually
                "stdout": cached.output_data.get('stdout', ''),
                "stderr": cached.output_data.get('stderr', ''),
                "output_data": cached.output_data,
                "execution_time": cached.execution_time_seconds,
                "cached": True
            }

        # Not Cached: Run Executor
        # Use Factory to get instantiated executor
        executor = Executor.factory(file)
        if not executor:
             raise ValueError(f"No executor found for file type: {file.name}")
             
        result = executor.execute() # Synchronous execution
        
        # Save to Cache
        # We need the user object if possible, but can be None
        from core.models import User
        user = None
        if user_id:
            try:
                user = User.objects.get(id=user_id)
            except User.DoesNotExist:
                pass
                
        result.save_cache(file, executed_by=user)

        from autograder.services.execution_events import record_execution_event
        record_execution_event(trigger='test_run', cached=False, success=result.success,
                               file=file, error_text=result.err or result.stderr)

        return {
            "success": result.success,
            "stdout": result.stdout,
            "stderr": result.stderr,
             "output_data": result.output_data,
             "execution_time": result.execution_time,
             "cached": False,
             "error": result.err
        }

    @staticmethod
    def _run_ephemeral_execution(file: 'File', test_case: 'TestCase', user_id: Optional[str] = None, file_overrides: Optional[Dict[int, str]] = None, test_function: Optional[str] = "__DEFAULT__") -> Dict[str, Any]:
        """
        Runs an ephemeral execution for a specific test case (with specific input/dataset).
        Does NOT save to the global cache (to avoid polluting it with test-specific runs).
        
        test_function: If "__DEFAULT__", uses test_case.functionName. 
                       If None, runs ALL tests (no filter).
                       If string, runs that specific function.
        """
        # Prepare datasets
        datasets = []

        # Prepare resources (File and DataSet aliasing)
        resources = []
        if hasattr(test_case, 'testCategory'):
            # Fetch all resources linked to this category
            # Each resource has a target_path and either a file or a dataset
            category_resources = test_case.testCategory.resources.all()  # type: ignore[attr-defined]  # Django reverse relation
            for res in category_resources:
                resource_entry = {
                    'target_path': res.target_path
                }
                if res.file:
                    resource_entry['type'] = 'file'
                    # Check for override
                    if file_overrides and res.file.id in file_overrides:
                         resource_entry['content'] = file_overrides[res.file.id]
                    else:
                         resource_entry['content'] = res.file.data
                elif res.dataset:
                    resource_entry['type'] = 'dataset'
                    resource_entry['obj'] = res.dataset # Executor will handle mounting/linking
                
                resources.append(resource_entry)

        # Prepare Test Code with Timeouts
        raw_test_code = test_case.testCategory.testScript if hasattr(test_case, 'testCategory') and test_case.testCategory.testScript else test_case.testCode
        
        # Inject timeouts
        import json
        test_timeouts = {}
        if hasattr(test_case, 'testCategory') and test_case.testCategory:
            # We use .values() to efficiently fetch just what we need
            # Note: We iterate via all objects manager to avoid prefetch complexities not being present
            for t in test_case.testCategory.testCases.values('functionName', 'timeout'):
                if t['functionName'] and t['timeout'] != 30:
                    test_timeouts[t['functionName']] = t['timeout']
        
        injected_code = raw_test_code + f"\n\nCODEPOST_TEST_TIMEOUTS = {json.dumps(test_timeouts)}\n"

        # Multi-file JUnit categories: collect all test files + the DB-owned points
        # map so the Java executor can stage every file and score from the DB
        # (not by re-parsing the script). Falls back to the single testScript when
        # a category has no TestCategoryFile rows.
        test_files = []
        points_by_function = {}
        if hasattr(test_case, 'testCategory') and test_case.testCategory:
            category = test_case.testCategory
            for f in category.testFiles.all():
                if (f.content or '').strip():
                    test_files.append({'name': f.name, 'content': f.content})
            for t in category.testCases.values('functionName', 'pointsPass'):
                if t['functionName']:
                    points_by_function[t['functionName']] = float(t['pointsPass'] or 0)
        
        # DEBUG: Log the code to backend
        logger.info(f"DEBUG_INJECTED_CODE_START\n{injected_code}\nDEBUG_INJECTED_CODE_END")

        if not injected_code.strip() or not raw_test_code.strip():
             return {
                 "success": False,
                 "error": "Test Script is empty. Please open the Script Editor and click 'Generate (AI)' or write a script."
             }

        # Sanitize overrides (redundant safety)
        file_overrides = TestService._sanitize_overrides(file_overrides)

        # Check for override for the main file being executed
        main_file_content = None
        additional_files_overrides = {}

        if file_overrides:
            if file.id in file_overrides:
                main_file_content = file_overrides[file.id]
            
            # Identify other overridden files
            override_ids = [fid for fid in file_overrides.keys() if fid != file.id]
            if override_ids:
                 from core.models import File as FileModel
                 override_files = FileModel.objects.filter(id__in=override_ids)
                 for f in override_files:
                     full_path = f.name
                     if f.path:
                         import os
                         full_path = os.path.join(f.path, f.name)
                     additional_files_overrides[full_path] = file_overrides[f.id]

        # Determine regex filter
        target_function = test_function
        if target_function == "__DEFAULT__":
             target_function = test_case.functionName

        # Use Factory to get instantiated executor with context
        executor = Executor.factory(
            file,
            datasets=datasets, # Legacy dataset field (maybe deprecated?)
            input_data=None,
            target_cell_id= getattr(test_case, 'targetCellId', None),
            test_code=injected_code,
            test_function= target_function,
            resources=resources, # Pass new resources list
            content_override=main_file_content,
            additional_files=additional_files_overrides,
            test_files=test_files,  # multi-file JUnit categories
            points_by_function=points_by_function,  # DB-owned per-test points
        )
        
        if not executor:
             raise ValueError(f"No executor found for file type: {file.name}")
        
        result = executor.execute()
        
        return {
            "success": result.success,
            "stdout": result.stdout,
            "stderr": result.stderr,
            "output_data": result.output_data,
            "execution_time": result.execution_time,
            "cached": False, 
            "error": result.err,
            "tests": getattr(result, 'tests', [])
        }



    @staticmethod
    def verify_unit_test(test_case: TestCase, execution_result: Dict[str, Any]) -> Dict[str, Any]:
        """
        Verifies Unit Tests (Java, Python unittest, etc).
        Relies on the executor returning success=True (exit code 0) for passing tests.
        """
        passed = execution_result.get('success', False)
        
        # Combine logs
        stdout = execution_result.get('stdout', '')
        stderr = execution_result.get('stderr', '')
        logs = f"{stdout}\n{stderr}".strip()
        
        syntax_hint = TestService._detect_syntax_hint(execution_result)
        if syntax_hint:
            logs = f"{syntax_hint}\n\n{logs}".strip()

        # Check for error (infrastructure level)
        # However, for unit tests, if 'error' exists it usually means compilation failed or similar.
        # So passed should definitely be False if error exists.
        is_error = execution_result.get('error') is not None
        if is_error:
            passed = False
        
        return {
            "passed": passed,
            "logs": logs,
            "isError": is_error
        }
    @staticmethod
    def verify_script_test(test_case: TestCase, execution_result: Dict[str, Any]) -> Dict[str, Any]:
        """
        Verifies Custom Script Tests.
        Parses the JSON results returned by the test framework (e.g. from Tester class).
        """
        tests = execution_result.get('tests', [])
        syntax_hint = TestService._detect_syntax_hint(execution_result)
        notebook_syntax_detail = TestService._collect_notebook_cell_error_text(execution_result).strip()
        
        # Combine logs
        stdout = execution_result.get('stdout', '')
        stderr = execution_result.get('stderr', '')
        # Only parse tests if we have them
        
        if not tests:
            # Maybe the script failed to run or produced no output
            _passed = False
            logs = f"{stdout}\n{stderr}".strip()
            if not logs:
                logs = "[Error] Test script produced no output and no test results found."
            if syntax_hint:
                logs = f"{syntax_hint}\n\n{logs}".strip()
            
            return {
                "passed": False,
                "logs": logs,
                "isError": execution_result.get('error') is not None,
                "score": 0,
                "maxScore": 0,
                "results": []
            }

        tests = TestService._annotate_tests_with_syntax_hint(tests, syntax_hint)
        syntax_hint_attached_to_test = bool(
            syntax_hint
            and any(
                syntax_hint in str(t.get('error') or '')
                for t in tests
            )
        )
            
        # Aggregate results
        # If ANY test failed, the whole TestCase fails (strict mode for now)
        all_passed = all(t.get('passed', False) for t in tests)
        
        # Aggregate scores from all subtests
        total_score = sum(float(t.get('score', 0)) for t in tests)
        total_max_score = sum(float(t.get('max_score', 0)) for t in tests)
        
        # Build aggregated logs
        # The user requested JSON logs. Since 'tests' is already a list of dicts (JSON-compatible),
        # and SubmissionTest.results is a JSONField, we can store the structured data there.
        # However, SubmissionTest.logs is a TextField. We can store a JSON string representation 
        # of the logs for flexibility, or keep the human-readable format.
        # The user said: "I want the logs to be json that way they can be flexable."
        
        # We will store the full list of test results as a JSON string in 'logs'.
        # This allows frontend to parse it if needed, while 'results' JSONField also holds it.
        import json
        try:
            logs = json.dumps(tests, indent=2)
        except Exception:
             # Fallback to text if serialization fails
             log_parts = []
             for t in tests:
                status = "✓" if t.get('passed') else "✗"
                name = t.get('name', 'Test')
                score = f"{t.get('score', 0)}/{t.get('max_score', 0)}"
                log_parts.append(f"{status} {name}: {score}")
                if t.get('description'):
                    log_parts.append(f"   Description: {t.get('description')}")
                if t.get('output'):
                    log_parts.append(f"   Output: {t.get('output')}")
                if t.get('error'):
                     log_parts.append(f"   Error: {t.get('error')}")
             logs = "\n".join(log_parts)
        
        # Append system error if exists
        if execution_result.get('error'):
             logs += f"\n\nSystem Error: {execution_result.get('error')}"
        if syntax_hint_attached_to_test:
            logs = f"{syntax_hint}\n\n{logs}".strip()
        elif syntax_hint:
            advisory_detail = notebook_syntax_detail or syntax_hint
            logs = (
                f"{logs}\n\n"
                "Notebook syntax advisory: one or more notebook cells had syntax/parse/compile issues. "
                "This specific test result may still be valid if it does not depend on those cells.\n"
                f"Full syntax details:\n{advisory_detail}"
            ).strip()
             
        return {
            "passed": all_passed,
            "logs": logs,
            "isError": execution_result.get('error') is not None,
            "score": total_score,
            "maxScore": total_max_score,
            "stdout": execution_result.get('stdout'),
            "stderr": execution_result.get('stderr'),
            "results": tests
        }

    @staticmethod
    def _save_test_result(
        submission: Submission,
        test_case: TestCase,
        passed: bool,
        score: float,
        logs: str,
        is_error: bool,
        max_score: float,
        results: Optional[List[Dict[str, Any]]] = None,
    ) -> SubmissionTest:
        """
        Helper to save/update a SubmissionTest result and sync rubric outcomes.
        """
        safe_results = TestService._to_json_safe(results or [])

        # Save SubmissionTest Result
        submission_test, created = SubmissionTest.objects.update_or_create(
            submission=submission,
            testCase=test_case,
            defaults={
                "passed": passed,
                "logs": logs,
                "isError": is_error,
                "score": score,
                "maxScore": max_score,
                "results": safe_results,
            }
        )
        
        # Sync Rubric
        target_file = TestService._get_target_file(submission, test_case)
        if target_file:
             TestService._sync_rubric_outcome(submission, test_case, passed, target_file)
             
        return submission_test
