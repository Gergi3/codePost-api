# Copyright © 2026 Rutgers, the State University of New Jersey. All rights reserved except as defined by the Rutgers Non-Commercial License, included with this software.
"""
Tests for autograder templates, output parsing, crash detection, syntax hint
detection, and result verification.

Part A — **Structural invariants**: reads every template source file and checks
         that it contains the required delimiters, crash-handling pattern, and
         placeholder tokens.

Part B — **Python template execution (round-trip)**: fills the real template
         with base64-encoded student code + test code, ``exec()``s the entire
         script in an isolated namespace, captures stderr, feeds it through
         ``Executor.parse_test_results`` → ``TestService.verify_script_test``,
         and asserts the full pipeline produces correct results.

Part B2 — **Notebook Python template execution**: same approach but uses the
          notebook template with cell-based input.

Part B3 — **JS template execution**: fills the Node.js template and runs it
          as a subprocess, verifying JSON output via ``parse_test_results``.

Part B4 — **JS Notebook template execution**: same approach but uses the
          notebook template with cell-based input.

Part B5 — **C++ template execution**: compiles and runs the C++ template with
          g++, verifying JSON output via ``parse_test_results``.

Part B6 — **Java template execution**: compiles and runs the Java template with
          javac, verifying JSON output via ``parse_test_results``.

Part B7 — **R template execution**: fills and runs the R template via Rscript,
          verifying JSON output.

Part B8 — **Ruby Notebook template execution**: fills and runs the Ruby notebook
          template via ruby, verifying JSON output.

Part B9 — **PHP Notebook template execution**: fills and runs the PHP notebook
          template via php, verifying JSON output.

Part C — **Backend parsing/processing logic**: exercises ``parse_test_results``,
         ``_detect_syntax_hint``, ``verify_script_test``, ``verify_unit_test``,
         ``_annotate_tests_with_syntax_hint``, etc. with hand-crafted output.
"""

import base64
import io
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import zipfile
from typing import Any, Dict, List, cast

from django.test import SimpleTestCase

from autograder.services.executors.base import Executor
from autograder.services.executors.cpp import _strip_cling_magics
from autograder.services.TestService import TestService

# ---------------------------------------------------------------------------
# Path to the templates directory
# ---------------------------------------------------------------------------
TEMPLATES_DIR = os.path.join(
    os.path.dirname(__file__), "..", "services", "templates"
)


# ---------------------------------------------------------------------------
# Helpers to build template-style output strings
# ---------------------------------------------------------------------------

def _wrap_result(payload: Any) -> str:
    """Wrap *payload* in the template delimiters, as all templates do."""
    return f"<<<TEST_RESULT_JSON_START>>>{json.dumps(payload)}<<<TEST_RESULT_JSON_END>>>"


def _make_test_result(
    name: str = "test_example",
    passed: bool = True,
    score: float = 1.0,
    max_score: float = 1.0,
    status: str = "passed",
    error: str = "",
    message: str = "",
    description: str = "",
    output: str = "",
) -> Dict[str, Any]:
    return {
        "name": name,
        "passed": passed,
        "score": score,
        "max_score": max_score,
        "status": status,
        "error": error,
        "message": message,
        "description": description,
        "output": output,
    }


def _make_crash_result(error_detail: str = "Test script failed to load: ImportError") -> Dict[str, Any]:
    """The synthetic error result emitted by every template when the test script crashes."""
    return _make_test_result(
        name="Test Script Execution",
        passed=False,
        score=0,
        max_score=0,
        status="error",
        error=error_detail,
    )


def _read_template(filename: str) -> str:
    """Read a template file from the templates directory."""
    path = os.path.join(TEMPLATES_DIR, filename)
    with open(path, "r") as f:
        return f.read()


def _fill_python_template(student_code: str, test_code: str, target_function: str = "") -> str:
    """
    Fill the real Python template with base64-encoded student and test code,
    exactly as the PythonExecutor._get_code_template does.
    """
    template = _read_template("template.py")
    code_b64 = base64.b64encode(student_code.encode("utf-8")).decode("utf-8")
    test_b64 = base64.b64encode(test_code.encode("utf-8")).decode("utf-8")
    template = template.replace("#{FILLER_CODE}", code_b64)
    template = template.replace("#{TEST_CODE}", test_b64)
    template = template.replace("#{TARGET_TEST_FUNCTION}", target_function)
    template = template.replace("#{STUDENT_FILE_PATH}", "/work/student.py")
    return template


def _exec_python_template(student_code: str, test_code: str, target_function: str = "") -> tuple:
    """
    Fill and exec() the Python template, capturing stdout and stderr.
    Returns (stdout_str, stderr_str).
    """
    script = _fill_python_template(student_code, test_code, target_function)
    old_stdout, old_stderr = sys.stdout, sys.stderr
    captured_stdout = io.StringIO()
    captured_stderr = io.StringIO()
    # exec in isolated namespace so template globals don't leak
    ns: Dict[str, Any] = {"__name__": "__main__", "__builtins__": __builtins__}
    try:
        sys.stdout = captured_stdout
        sys.stderr = captured_stderr
        exec(compile(script, "<python_template>", "exec"), ns)
    except SystemExit:
        pass  # template may call sys.exit
    except Exception:
        import traceback
        traceback.print_exc(file=captured_stderr)
    finally:
        sys.stdout = old_stdout
        sys.stderr = old_stderr
    return captured_stdout.getvalue(), captured_stderr.getvalue()


def _fill_notebook_python_template(
    cells: List[Dict[str, Any]],
    test_code: str,
    target_function: str = "",
) -> str:
    """
    Fill the real notebook Python template with base64-encoded cells and test code,
    exactly as the PythonNotebookExecutor._get_code_template does.
    """
    template = _read_template("notebook_template.py")
    cells_b64 = base64.b64encode(json.dumps(cells).encode("utf-8")).decode("utf-8")
    test_b64 = base64.b64encode(test_code.encode("utf-8")).decode("utf-8")
    template = template.replace("{cells_b64}", cells_b64)
    template = template.replace("{test_code_b64}", test_b64)
    template = template.replace("#{TARGET_TEST_FUNCTION}", target_function)
    return template


def _exec_notebook_python_template(
    cells: List[Dict[str, Any]],
    test_code: str,
    target_function: str = "",
) -> tuple:
    """
    Fill and exec() the notebook Python template, capturing stdout and stderr.
    Returns (stdout_str, stderr_str).
    """
    script = _fill_notebook_python_template(cells, test_code, target_function)
    old_stdout, old_stderr = sys.stdout, sys.stderr
    captured_stdout = io.StringIO()
    captured_stderr = io.StringIO()
    ns: Dict[str, Any] = {"__name__": "__main__", "__builtins__": __builtins__}
    try:
        sys.stdout = captured_stdout
        sys.stderr = captured_stderr
        exec(compile(script, "<notebook_python_template>", "exec"), ns)
    except SystemExit:
        pass
    except Exception:
        import traceback
        traceback.print_exc(file=captured_stderr)
    finally:
        sys.stdout = old_stdout
        sys.stderr = old_stderr
    return captured_stdout.getvalue(), captured_stderr.getvalue()


def _fill_js_template(student_code: str, test_code: str) -> str:
    """
    Fill the real JS template with student code and base64-encoded test code,
    exactly as the NodeExecutor._get_code_template does.
    """
    template = _read_template("template.js")
    template = template.replace("// FILLER_CODE", student_code)
    test_b64 = base64.b64encode(test_code.encode("utf-8")).decode("utf-8") if test_code else ""
    template = template.replace("{test_code_b64}", test_b64)
    return template


def _exec_js_template(student_code: str, test_code: str, timeout: int = 10) -> tuple:
    """
    Fill the JS template, write to a temp file, run with Node.js, and capture
    stdout and stderr.  Returns (stdout_str, stderr_str).
    Raises ``unittest.SkipTest`` if Node.js is not available.
    """
    node = shutil.which("node")
    if not node:
        from unittest import SkipTest
        raise SkipTest("Node.js not available — skipping JS template test")
    script = _fill_js_template(student_code, test_code)
    tmp = tempfile.NamedTemporaryFile(mode="w", suffix=".js", delete=False)
    try:
        tmp.write(script)
        tmp.close()
        result = subprocess.run(
            [node, tmp.name],
            capture_output=True,
            text=True,
            timeout=timeout,
        )
        return result.stdout, result.stderr
    finally:
        os.unlink(tmp.name)


def _fill_notebook_js_template(
    cells: List[Dict[str, Any]],
    test_code: str,
) -> str:
    """Fill the real notebook JS template with base64-encoded cells and test code."""
    template = _read_template("notebook_template.js")
    cells_b64 = base64.b64encode(json.dumps(cells).encode("utf-8")).decode("utf-8")
    test_b64 = base64.b64encode(test_code.encode("utf-8")).decode("utf-8") if test_code else ""
    template = template.replace("{cells_b64}", cells_b64)
    template = template.replace("{test_code_b64}", test_b64)
    return template


def _exec_notebook_js_template(
    cells: List[Dict[str, Any]],
    test_code: str,
    timeout: int = 10,
) -> tuple:
    """
    Fill the notebook JS template, write to a temp file, run with Node.js.
    Returns (stdout_str, stderr_str).
    """
    node = shutil.which("node")
    if not node:
        from unittest import SkipTest
        raise SkipTest("Node.js not available — skipping JS notebook template test")
    script = _fill_notebook_js_template(cells, test_code)
    tmp = tempfile.NamedTemporaryFile(mode="w", suffix=".js", delete=False)
    try:
        tmp.write(script)
        tmp.close()
        result = subprocess.run(
            [node, tmp.name],
            capture_output=True,
            text=True,
            timeout=timeout,
        )
        return result.stdout, result.stderr
    finally:
        os.unlink(tmp.name)


# ---------------------------------------------------------------------------
# C++ template helpers
# ---------------------------------------------------------------------------

def _fill_cpp_template(test_code: str) -> str:
    """Fill the C++ template with test code (student code is compiled separately)."""
    template = _read_template("template.cpp")
    return template.replace("#{TEST_CODE}", test_code)


def _exec_cpp_template(student_code: str, test_code: str, timeout: int = 30) -> tuple:
    """
    Compile and run the C++ template with student code.
    Student code is compiled as a separate object with -Dmain=__student_main.
    Returns (stdout_str, stderr_str).
    """
    gpp = shutil.which("g++")
    if not gpp:
        from unittest import SkipTest
        raise SkipTest("g++ not available — skipping C++ template test")
    filled = _fill_cpp_template(test_code)
    tmpdir = tempfile.mkdtemp()
    try:
        runner_path = os.path.join(tmpdir, "runner.cpp")
        source_path = os.path.join(tmpdir, "source.cpp")
        program_path = os.path.join(tmpdir, "program")

        with open(runner_path, "w") as f:
            f.write(filled)
        with open(source_path, "w") as f:
            f.write(student_code)

        # Compile student code with main renamed to avoid conflicts
        compile_result = subprocess.run(
            [gpp, "-Dmain=__student_main", "-c", source_path, "-o",
             os.path.join(tmpdir, "source.o")],
            capture_output=True, text=True, timeout=timeout,
        )
        if compile_result.returncode != 0:
            return "", compile_result.stderr

        # Compile runner
        compile_result = subprocess.run(
            [gpp, "-c", runner_path, "-o", os.path.join(tmpdir, "runner.o")],
            capture_output=True, text=True, timeout=timeout,
        )
        if compile_result.returncode != 0:
            return "", compile_result.stderr

        # Link
        link_result = subprocess.run(
            [gpp, os.path.join(tmpdir, "source.o"),
             os.path.join(tmpdir, "runner.o"), "-o", program_path, "-lpthread"],
            capture_output=True, text=True, timeout=timeout,
        )
        if link_result.returncode != 0:
            return "", link_result.stderr

        # Run
        result = subprocess.run(
            [program_path],
            capture_output=True, text=True, timeout=timeout,
            cwd=tmpdir,
        )
        return result.stdout, result.stderr
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)


# ---------------------------------------------------------------------------
# Java template helpers
# ---------------------------------------------------------------------------

def _fill_java_template(test_code: str) -> str:
    """Fill the Java template with test code."""
    template = _read_template("TestRunner.java")
    return template.replace("#{TEST_CODE}", test_code)


def _exec_java_template(student_code: str, test_code: str, timeout: int = 30) -> tuple:
    """
    Compile and run the Java template with student code.
    Returns (stdout_str, stderr_str).
    """
    javac = shutil.which("javac")
    java = shutil.which("java")
    if not javac or not java:
        from unittest import SkipTest
        raise SkipTest("javac/java not available — skipping Java template test")
    filled = _fill_java_template(test_code)
    tmpdir = tempfile.mkdtemp()
    try:
        runner_path = os.path.join(tmpdir, "TestRunner.java")
        student_path = os.path.join(tmpdir, "Student.java")

        with open(runner_path, "w") as f:
            f.write(filled)
        with open(student_path, "w") as f:
            f.write(student_code)

        # Compile
        compile_result = subprocess.run(
            [javac, runner_path, student_path],
            capture_output=True, text=True, timeout=timeout,
        )
        if compile_result.returncode != 0:
            return "", compile_result.stderr

        # Run
        result = subprocess.run(
            [java, "-cp", tmpdir, "TestRunner"],
            capture_output=True, text=True, timeout=timeout,
        )
        return result.stdout, result.stderr
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)


def _fill_notebook_java_template(cells: List[Dict[str, Any]], test_code: str) -> str:
    """Fill the Java notebook template exactly as JavaNotebookExecutor._get_code_template does."""
    template = _read_template("notebook_template.java")
    cells_b64 = base64.b64encode(json.dumps(cells).encode("utf-8")).decode("utf-8")
    test_b64 = base64.b64encode(test_code.encode("utf-8")).decode("utf-8") if test_code else ""
    template = template.replace("{cells_b64}", cells_b64)
    template = template.replace("{test_code_b64}", test_b64)
    template = template.replace("package autograder.services.templates;", "")
    template = template.replace("public class notebook_template", "public class NotebookRunner")
    template = template.replace("class notebook_template", "class NotebookRunner")
    return template


def _exec_notebook_java_template(cells: List[Dict[str, Any]], test_code: str, timeout: int = 120) -> tuple:
    """
    Compile and run the Java notebook template with javac/java (the same
    commands the executor runs in the container). Returns (stdout, stderr).
    """
    javac = shutil.which("javac")
    java = shutil.which("java")
    if not javac or not java:
        from unittest import SkipTest
        raise SkipTest("javac/java not available — skipping Java notebook template test")
    tmpdir = tempfile.mkdtemp()
    try:
        runner_path = os.path.join(tmpdir, "NotebookRunner.java")
        filled = _fill_notebook_java_template(cells, test_code)
        # The runner exits unless /work (the container mount) exists; point it at the temp dir.
        filled = filled.replace('"/work"', json.dumps(tmpdir))
        with open(runner_path, "w") as f:
            f.write(filled)
        compile_result = subprocess.run(
            [javac, "-d", tmpdir, runner_path],
            capture_output=True, text=True, timeout=timeout,
        )
        if compile_result.returncode != 0:
            return "", compile_result.stderr
        result = subprocess.run(
            [java, "-cp", tmpdir, "NotebookRunner"],
            capture_output=True, text=True, timeout=timeout, cwd=tmpdir,
        )
        return result.stdout, result.stderr
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)


# ---------------------------------------------------------------------------
# R template helpers
# ---------------------------------------------------------------------------

def _fill_r_template(test_code: str) -> str:
    """Fill the R template with test code.
    The R template embeds test_code directly in the script body,
    so no escaping is needed.
    """
    template = _read_template("template.r")
    return template.replace("#{TEST_CODE}", test_code)


def _exec_r_template(student_code: str, test_code: str, timeout: int = 30) -> tuple:
    """
    Fill and run the R template via Rscript.
    Student code is written as student.R in a temp dir (as the R template expects).
    Returns (stdout_str, stderr_str).
    """
    rscript = shutil.which("Rscript")
    if not rscript:
        from unittest import SkipTest
        raise SkipTest("Rscript not available — skipping R template test")
    filled = _fill_r_template(test_code)
    tmpdir = tempfile.mkdtemp()
    try:
        script_path = os.path.join(tmpdir, "runner.R")
        student_path = os.path.join(tmpdir, "student.R")

        with open(script_path, "w") as f:
            f.write(filled)
        with open(student_path, "w") as f:
            f.write(student_code)

        result = subprocess.run(
            [rscript, script_path],
            capture_output=True, text=True, timeout=timeout,
            cwd=tmpdir,
        )
        return result.stdout, result.stderr
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)


# ---------------------------------------------------------------------------
# Ruby Notebook template helpers
# ---------------------------------------------------------------------------

def _fill_notebook_ruby_template(
    cells: List[Dict[str, Any]],
    test_code: str,
) -> str:
    """Fill the Ruby notebook template with base64-encoded cells and test code."""
    template = _read_template("notebook_template.rb")
    cells_b64 = base64.b64encode(json.dumps(cells).encode("utf-8")).decode("utf-8")
    test_b64 = base64.b64encode(test_code.encode("utf-8")).decode("utf-8") if test_code else ""
    template = template.replace("{cells_b64}", cells_b64)
    template = template.replace("{test_code_b64}", test_b64)
    return template


def _exec_notebook_ruby_template(
    cells: List[Dict[str, Any]],
    test_code: str,
    timeout: int = 15,
) -> tuple:
    """
    Fill the Ruby notebook template and run via ruby subprocess.
    Returns (stdout_str, stderr_str).
    """
    ruby = shutil.which("ruby")
    if not ruby:
        from unittest import SkipTest
        raise SkipTest("Ruby not available — skipping Ruby notebook template test")
    script = _fill_notebook_ruby_template(cells, test_code)
    tmp = tempfile.NamedTemporaryFile(mode="w", suffix=".rb", delete=False)
    try:
        tmp.write(script)
        tmp.close()
        result = subprocess.run(
            [ruby, tmp.name],
            capture_output=True,
            text=True,
            timeout=timeout,
        )
        return result.stdout, result.stderr
    finally:
        os.unlink(tmp.name)


def _fill_notebook_php_template(
    cells: List[Dict[str, Any]],
    test_code: str,
) -> str:
    """Fill the PHP notebook template with base64-encoded cells and test code."""
    template = _read_template("notebook_template.php")
    cells_b64 = base64.b64encode(json.dumps(cells).encode("utf-8")).decode("utf-8")
    test_b64 = base64.b64encode(test_code.encode("utf-8")).decode("utf-8") if test_code else ""
    template = template.replace("{cells_b64}", cells_b64)
    template = template.replace("{test_code_b64}", test_b64)
    return template


def _exec_notebook_php_template(
    cells: List[Dict[str, Any]],
    test_code: str,
    timeout: int = 15,
) -> tuple:
    """
    Fill the PHP notebook template and run via php subprocess.
    Returns (stdout_str, stderr_str).
    """
    php = shutil.which("php")
    if not php:
        from unittest import SkipTest
        raise SkipTest("PHP not available — skipping PHP notebook template test")
    script = _fill_notebook_php_template(cells, test_code)
    tmp = tempfile.NamedTemporaryFile(mode="w", suffix=".php", delete=False)
    try:
        tmp.write(script)
        tmp.close()
        result = subprocess.run(
            [php, tmp.name],
            capture_output=True,
            text=True,
            timeout=timeout,
        )
        return result.stdout, result.stderr
    finally:
        os.unlink(tmp.name)


# ###################################################################
# PART A — Structural invariant tests for ALL template source files
# ###################################################################

class TemplateStructuralTests(SimpleTestCase):
    """Verify every template file contains the required structural elements."""

    # Templates that have full test frameworks with JSON output
    FULL_FRAMEWORK_TEMPLATES = [
        "template.py",
        "template.js",
        "template.cpp",
        "template.r",
        "TestRunner.java",
        "notebook_template.py",
        "notebook_template.js",
        "notebook_template.r",
        "notebook_template.rb",
        "notebook_template.php",
        "notebook_template.java",
    ]

    # Templates that have crash handling emitting "Test Script Execution"
    CRASH_HANDLING_TEMPLATES = [
        "template.py",
        "template.js",
        "template.cpp",
        "template.r",
        "TestRunner.java",
        "notebook_template.py",
        "notebook_template.js",
        "notebook_template.r",
        "notebook_template.rb",
        "notebook_template.php",
        "notebook_template.java",
    ]

    # Simpler templates without a test framework
    SIMPLE_TEMPLATES = ["template.rb", "template.php"]

    def test_all_framework_templates_have_json_delimiters(self):
        for tpl_name in self.FULL_FRAMEWORK_TEMPLATES:
            src = _read_template(tpl_name)
            self.assertIn("TEST_RESULT_JSON_START", src, f"{tpl_name} missing TEST_RESULT_JSON_START")
            self.assertIn("TEST_RESULT_JSON_END", src, f"{tpl_name} missing TEST_RESULT_JSON_END")

    def test_all_crash_templates_emit_test_script_execution(self):
        for tpl_name in self.CRASH_HANDLING_TEMPLATES:
            src = _read_template(tpl_name)
            self.assertIn("Test Script Execution", src, f"{tpl_name} missing crash result name")

    def test_all_crash_templates_have_error_status(self):
        """Crash results must set status to 'error'."""
        for tpl_name in self.CRASH_HANDLING_TEMPLATES:
            src = _read_template(tpl_name)
            has_error_status = (
                '"error"' in src
                or "'error'" in src
                or '"status":"error"' in src
                or 'status = "error"' in src
                or "status = 'error'" in src
                or '"status\\": \\"error' in src  # Java string escaping
                or '"status":"error"' in src
            )
            self.assertTrue(has_error_status, f"{tpl_name} crash result missing error status")

    def test_python_template_has_required_placeholders(self):
        src = _read_template("template.py")
        self.assertIn("#{FILLER_CODE}", src)
        self.assertIn("#{TEST_CODE}", src)
        self.assertIn("#{TARGET_TEST_FUNCTION}", src)
        self.assertIn("#{STUDENT_FILE_PATH}", src)

    def test_js_template_has_required_placeholders(self):
        src = _read_template("template.js")
        self.assertIn("FILLER_CODE", src)
        self.assertIn("test_code_b64", src)

    def test_java_template_has_required_placeholders(self):
        src = _read_template("TestRunner.java")
        self.assertIn("#{TEST_CODE}", src)

    def test_cpp_template_has_required_placeholders(self):
        src = _read_template("template.cpp")
        self.assertIn("#{TEST_CODE}", src)

    def test_r_template_has_required_placeholders(self):
        src = _read_template("template.r")
        self.assertIn("#{TEST_CODE}", src)

    def test_simple_templates_have_filler_code(self):
        for tpl_name in self.SIMPLE_TEMPLATES:
            src = _read_template(tpl_name)
            self.assertIn("FILLER_CODE", src, f"{tpl_name} missing FILLER_CODE placeholder")

    def test_python_template_has_test_framework_classes(self):
        src = _read_template("template.py")
        self.assertIn("class TestResult", src)
        self.assertIn("class TestCase", src)
        self.assertIn("class TestRunner", src)
        self.assertIn("def test(", src)

    def test_java_template_has_test_annotation(self):
        src = _read_template("TestRunner.java")
        self.assertIn("@interface Test", src)
        self.assertIn("@Retention", src)

    def test_junit_platform_runner_has_required_elements(self):
        src = _read_template("JUnitPlatformRunner.java")
        # Output protocol markers the autograder parses.
        self.assertIn("TEST_RESULT_JSON_START", src)
        self.assertIn("TEST_RESULT_JSON_END", src)
        # Launcher API + classpath discovery of the compiled 'out' dir.
        self.assertIn("selectClasspathRoots", src)
        self.assertIn("LauncherFactory", src)
        self.assertIn("TestExecutionListener", src)
        # Per-test timeout isolation so one hung test can't lose the whole suite.
        self.assertIn("junit.jupiter.execution.timeout.testable.method.default", src)
        # Injected points map + default timeout placeholders.
        self.assertIn("#{POINTS_JSON}", src)
        self.assertIn("#{DEFAULT_TIMEOUT}", src)
        # Required result fields for parse_test_results.
        for field in ("name", "score", "max_score", "passed", "status", "error"):
            self.assertIn(field, src, f"JUnitPlatformRunner missing field {field}")

    def test_cpp_template_has_test_macros(self):
        src = _read_template("template.cpp")
        self.assertIn("#define TEST(", src)
        self.assertIn("TestRegistry", src)

    def test_js_template_has_test_function(self):
        src = _read_template("template.js")
        self.assertIn("function test(", src)
        self.assertIn("runAllTests", src)

    def test_r_template_has_run_test(self):
        src = _read_template("template.r")
        self.assertIn("run_test", src)
        self.assertIn("output_test_results", src)

    def test_scripted_templates_have_result_marker(self):
        """Templates that emit <<<RESULT>>> as a log separation marker."""
        # Not all templates use this — R and C++ don't.
        has_marker = ["template.py", "template.js", "template.rb", "template.php"]
        for tpl_name in has_marker:
            src = _read_template(tpl_name)
            self.assertIn("<<<RESULT>>>", src, f"{tpl_name} missing <<<RESULT>>> marker")

    def test_template_json_fields_match_parser_expectations(self):
        """The JSON fields the parser expects (name, score, max_score, passed, status, error)
        must all be present in each template's result construction."""
        required_fields = ["name", "score", "max_score", "passed", "status", "error"]
        for tpl_name in self.FULL_FRAMEWORK_TEMPLATES:
            src = _read_template(tpl_name)
            for field in required_fields:
                # Fields appear as string keys, object keys, or struct members:
                # "name", 'name', .name, name:, $name, etc.
                has_field = (
                    f'"{field}"' in src
                    or f"'{field}'" in src
                    or f".{field}" in src
                    or f"${field}" in src
                    or re.search(rf'\b{field}\s*[:=]', src) is not None
                )
                self.assertTrue(has_field, f"{tpl_name} missing JSON field: {field}")


# ###################################################################
# PART B — Python template round-trip execution tests
# ###################################################################

class PythonTemplateRoundTripTests(SimpleTestCase):
    """
    Fill the REAL template.py with student code + test code, execute it,
    feed output through parse_test_results → verify_script_test, and assert
    the full pipeline produces correct results.
    """

    def _run_and_parse(self, student_code: str, test_code: str, target_function: str = ""):
        """Helper: exec template → parse results → return (tests, stdout, stderr)."""
        stdout, stderr = _exec_python_template(student_code, test_code, target_function)
        clean_stdout, clean_stderr, tests = Executor.parse_test_results(stdout, stderr)
        return tests, stdout, stderr

    def test_passing_test(self):
        """A simple test that passes produces correct JSON."""
        student = "def add(a, b): return a + b"
        tests = """
@test(name="Addition", points=2)
def test_add():
    assert add(1, 2) == 3
"""
        results, _, _ = self._run_and_parse(student, tests)
        self.assertEqual(len(results), 1)
        self.assertTrue(results[0]["passed"])
        self.assertEqual(results[0]["score"], 2.0)
        self.assertEqual(results[0]["max_score"], 2.0)
        self.assertEqual(results[0]["status"], "passed")
        self.assertEqual(results[0]["name"], "Addition")

    def test_failing_test(self):
        """A test that fails an assertion produces status='failed'."""
        student = "def add(a, b): return a - b  # intentionally wrong"
        tests = """
@test(name="Addition", points=3)
def test_add():
    assert add(1, 2) == 3, "Expected 3"
"""
        results, _, _ = self._run_and_parse(student, tests)
        self.assertEqual(len(results), 1)
        self.assertFalse(results[0]["passed"])
        self.assertEqual(results[0]["score"], 0)
        self.assertEqual(results[0]["status"], "failed")
        self.assertIn("Expected 3", results[0]["error"])

    def test_partial_credit_numeric(self):
        """Returning a number gives partial credit."""
        student = ""
        tests = """
@test(name="Partial", points=10)
def test_partial():
    return 7
"""
        results, _, _ = self._run_and_parse(student, tests)
        self.assertEqual(len(results), 1)
        self.assertFalse(results[0]["passed"])
        self.assertEqual(results[0]["score"], 7.0)
        self.assertEqual(results[0]["status"], "partial")

    def test_partial_credit_tuple(self):
        """Returning [score, message] gives partial credit with message."""
        student = ""
        tests = """
@test(name="Tuple", points=5)
def test_tuple():
    return [3, "Got 3 out of 5"]
"""
        results, _, _ = self._run_and_parse(student, tests)
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0]["score"], 3.0)
        self.assertEqual(results[0]["message"], "Got 3 out of 5")
        self.assertEqual(results[0]["status"], "partial")

    def test_full_credit_no_return(self):
        """A test that returns None gets full credit."""
        student = ""
        tests = """
@test(name="NoReturn", points=5)
def test_nothing():
    x = 1 + 1
"""
        results, _, _ = self._run_and_parse(student, tests)
        self.assertEqual(len(results), 1)
        self.assertTrue(results[0]["passed"])
        self.assertEqual(results[0]["score"], 5.0)

    def test_exception_gives_error_status(self):
        """An unexpected exception gives status='error' with traceback."""
        student = ""
        tests = """
@test(name="Crash", points=1)
def test_crash():
    raise ValueError("something went wrong")
"""
        results, _, _ = self._run_and_parse(student, tests)
        self.assertEqual(len(results), 1)
        self.assertFalse(results[0]["passed"])
        self.assertEqual(results[0]["status"], "error")
        self.assertIn("ValueError", results[0]["error"])
        self.assertIn("something went wrong", results[0]["error"])

    def test_multiple_tests(self):
        """Multiple tests all produce separate results."""
        student = "def f(x): return x * 2"
        tests = """
@test(name="Double 5", points=1)
def test_a():
    assert f(5) == 10

@test(name="Double 0", points=1)
def test_b():
    assert f(0) == 0

@test(name="Double negative", points=1)
def test_c():
    assert f(-3) == -6
"""
        results, _, _ = self._run_and_parse(student, tests)
        self.assertEqual(len(results), 3)
        self.assertTrue(all(r["passed"] for r in results))
        names = [r["name"] for r in results]
        self.assertEqual(names, ["Double 5", "Double 0", "Double negative"])

    def test_mixed_pass_fail(self):
        """Mix of passing and failing tests."""
        student = "def f(x): return x + 1"
        tests = """
@test(name="Correct", points=2)
def test_ok():
    assert f(1) == 2

@test(name="Wrong", points=3)
def test_bad():
    assert f(1) == 3, "Expected 3"
"""
        results, _, _ = self._run_and_parse(student, tests)
        self.assertEqual(len(results), 2)
        self.assertTrue(results[0]["passed"])
        self.assertFalse(results[1]["passed"])

    def test_student_syntax_error_marks_all_tests_error(self):
        """If student code has a SyntaxError, all tests get status='error'."""
        student = "def broken(\n"  # SyntaxError
        tests = """
@test(name="T1", points=1)
def test_one():
    pass
"""
        results, _, stderr = self._run_and_parse(student, tests)
        self.assertEqual(len(results), 1)
        self.assertFalse(results[0]["passed"])
        self.assertEqual(results[0]["status"], "error")
        self.assertIn("syntax", results[0]["error"].lower())

    def test_test_script_crash_emits_synthetic_result(self):
        """If test code itself crashes at import time, a synthetic crash result appears."""
        student = ""
        tests = "import nonexistent_module_xyz"
        results, _, stderr = self._run_and_parse(student, tests)
        crash = [r for r in results if r.get("name") == "Test Script Execution"]
        self.assertEqual(len(crash), 1)
        self.assertFalse(crash[0]["passed"])
        self.assertEqual(crash[0]["status"], "error")
        self.assertIn("nonexistent_module_xyz", crash[0]["error"])

    def test_score_clamped_to_max(self):
        """Returning a score > max_score gets clamped."""
        student = ""
        tests = """
@test(name="Clamped", points=5)
def test_clamp():
    return 100
"""
        results, _, _ = self._run_and_parse(student, tests)
        self.assertEqual(results[0]["score"], 5.0)
        self.assertTrue(results[0]["passed"])

    def test_score_clamped_to_zero(self):
        """Returning a negative score gets clamped to 0."""
        student = ""
        tests = """
@test(name="Negative", points=5)
def test_neg():
    return -10
"""
        results, _, _ = self._run_and_parse(student, tests)
        self.assertEqual(results[0]["score"], 0)

    def test_description_preserved(self):
        """Test description passes through to the JSON result."""
        student = ""
        tests = """
@test(name="Described", points=1, description="A very important test")
def test_desc():
    pass
"""
        results, _, _ = self._run_and_parse(student, tests)
        self.assertEqual(results[0]["description"], "A very important test")

    def test_stdout_captured_in_output(self):
        """Print statements inside the test function appear in 'output'."""
        student = ""
        tests = """
@test(name="Prints", points=1)
def test_prints():
    print("hello from test")
"""
        results, _, _ = self._run_and_parse(student, tests)
        self.assertIn("hello from test", results[0]["output"])

    def test_target_function_filters_tests(self):
        """When target_function is set, only the matching test runs."""
        student = ""
        tests = """
@test(name="A", points=1)
def func_a():
    pass

@test(name="B", points=1)
def func_b():
    pass
"""
        results, _, _ = self._run_and_parse(student, tests, target_function="func_b")
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0]["name"], "B")

    def test_round_trip_through_verify_script_test(self):
        """Full pipeline: template exec → parse → verify_script_test."""
        student = "def sq(x): return x ** 2"
        tests = """
@test(name="Square 3", points=5)
def test_sq():
    assert sq(3) == 9

@test(name="Square 0", points=5)
def test_sq0():
    assert sq(0) == 0
"""
        results, stdout, stderr = self._run_and_parse(student, tests)
        exec_result = {
            "stdout": stdout,
            "stderr": stderr,
            "error": None,
            "tests": results,
        }
        v = TestService.verify_script_test(cast(Any, None), exec_result)
        self.assertTrue(v["passed"])
        self.assertEqual(v["score"], 10)
        self.assertEqual(v["maxScore"], 10)
        self.assertFalse(v["isError"])

    def test_crash_round_trip_through_verify(self):
        """Crash pipeline: template crashes → parse → verify detects error."""
        student = ""
        tests = "raise RuntimeError('boom')"
        results, stdout, stderr = self._run_and_parse(student, tests)
        exec_result = {
            "stdout": stdout,
            "stderr": stderr,
            "error": None,
            "tests": results,
        }
        v = TestService.verify_script_test(cast(Any, None), exec_result)
        self.assertFalse(v["passed"])
        self.assertIn("boom", v["logs"])

    def test_syntax_error_round_trip_with_hint(self):
        """Syntax error pipeline: bad student code → parse → syntax hint detected."""
        student = "def broken(\n"
        tests = """
@test(name="T1", points=1)
def test_t1():
    broken()
"""
        results, stdout, stderr = self._run_and_parse(student, tests)
        exec_result = {
            "stdout": stdout,
            "stderr": stderr,
            "error": None,
            "tests": results,
        }
        hint = TestService._detect_syntax_hint(exec_result)
        self.assertIsNotNone(hint)
        self.assertIn("syntax", hint.lower())


# ###################################################################
# PART B2 — Notebook Python template round-trip execution tests
# ###################################################################

class NotebookPythonTemplateRoundTripTests(SimpleTestCase):
    """
    Fills the real ``notebook_template.py`` with base64-encoded cells and test
    code, exec()s it, and verifies the output JSON + per-test stderr markers.
    """

    @staticmethod
    def _make_cells(sources: List[str]) -> List[Dict[str, Any]]:
        """Build a cell list from source strings (all code cells)."""
        return [{"type": "code", "source": s, "idx": i} for i, s in enumerate(sources)]

    def _run_and_parse(
        self,
        cell_sources: List[str],
        test_code: str,
        target_function: str = "",
    ):
        """
        Execute template, parse results JSON from stdout and per-test markers
        from stderr.  Returns (results_json, per_test_results, stdout, stderr).
        """
        stdout, stderr = _exec_notebook_python_template(
            self._make_cells(cell_sources), test_code, target_function
        )
        # Parse the <<<RESULTS_START>>> block from stdout
        results_json = None
        m = re.search(r"<<<RESULTS_START>>>\s*(.*?)\s*<<<RESULTS_END>>>", stdout, re.DOTALL)
        if m:
            results_json = json.loads(m.group(1))

        # Also parse per-test JSON markers from stderr (same as regular template)
        _, _, per_test = Executor.parse_test_results(stdout, stderr)
        return results_json, per_test, stdout, stderr

    # -- Basic passing test -------------------------------------------------

    def test_single_passing_test(self):
        """One cell defines a function, test asserts it works."""
        cells = ["def add(a, b): return a + b"]
        tests = """
@test(name="test_add", points=5)
def test_add():
    assert add(2, 3) == 5
"""
        results_json, per_test, stdout, stderr = self._run_and_parse(cells, tests)
        self.assertIsNotNone(results_json)
        self.assertTrue(results_json["success"])
        self.assertEqual(len(results_json["tests"]), 1)
        self.assertTrue(results_json["tests"][0]["passed"])
        self.assertEqual(results_json["tests"][0]["score"], 5)
        # Per-test markers also parsed
        self.assertEqual(len(per_test), 1)
        self.assertEqual(per_test[0]["name"], "test_add")

    # -- Multi-cell state persistence ---------------------------------------

    def test_multi_cell_state_persists(self):
        """Variables from earlier cells are accessible in later cells."""
        cells = [
            "x = 10",
            "y = x * 2",
        ]
        tests = """
@test(name="test_state", points=1)
def test_state():
    assert y == 20, f"y should be 20 but was {y}"
"""
        results_json, per_test, _, _ = self._run_and_parse(cells, tests)
        self.assertTrue(results_json["tests"][0]["passed"])

    # -- Failing test -------------------------------------------------------

    def test_failing_assertion(self):
        """Failed assert → passed=False, status='failed'."""
        cells = ["result = 42"]
        tests = """
@test(name="wrong", points=3)
def wrong():
    assert result == 99, "not 99"
"""
        results_json, per_test, _, _ = self._run_and_parse(cells, tests)
        t = results_json["tests"][0]
        self.assertFalse(t["passed"])
        self.assertEqual(t["status"], "failed")
        self.assertEqual(t["score"], 0)

    # -- Multiple tests -----------------------------------------------------

    def test_multiple_tests_mixed(self):
        """Two tests: one pass, one fail — both recorded."""
        cells = ["val = 7"]
        tests = """
@test(name="pass", points=1)
def pass_test():
    assert val == 7

@test(name="fail", points=2)
def fail_test():
    assert val == 0
"""
        results_json, per_test, _, _ = self._run_and_parse(cells, tests)
        self.assertEqual(len(results_json["tests"]), 2)
        names = {t["name"]: t for t in results_json["tests"]}
        self.assertTrue(names["pass"]["passed"])
        self.assertFalse(names["fail"]["passed"])
        self.assertEqual(len(per_test), 2)

    # -- Test script crash --------------------------------------------------

    def test_crash_produces_synthetic_error(self):
        """If the test script itself raises, a synthetic error result appears."""
        cells = ["x = 1"]
        tests = "raise RuntimeError('test script exploded')"
        results_json, per_test, _, _ = self._run_and_parse(cells, tests)
        self.assertEqual(len(results_json["tests"]), 1)
        t = results_json["tests"][0]
        self.assertFalse(t["passed"])
        self.assertEqual(t["status"], "error")
        self.assertIn("exploded", t["error"])

    # -- Syntax error in student cell ---------------------------------------

    def test_syntax_error_in_cell(self):
        """A syntax error in a student cell is caught; test code still runs."""
        cells = ["def broken(\n"]  # SyntaxError
        tests = """
@test(name="after_error", points=1)
def after_error():
    pass  # Should still register
"""
        results_json, per_test, _, stderr = self._run_and_parse(cells, tests)
        # The notebook template still runs and produces results JSON
        self.assertIsNotNone(results_json)
        # Cell output should contain an error
        code_cells = [c for c in results_json["output_data"]["cells"] if c.get("cell_type") == "code"]
        self.assertTrue(len(code_cells) > 0)
        has_error = any(
            o.get("output_type") == "error"
            for c in code_cells
            for o in c.get("outputs", [])
        )
        self.assertTrue(has_error)

    # -- Partial credit returns score directly ------------------------------

    def test_partial_credit_via_return(self):
        """Test returning a numeric score gives partial credit."""
        cells = ["data = [1, 2, 3]"]
        tests = """
@test(name="partial", points=10)
def partial():
    return 7  # partial credit
"""
        results_json, _, _, _ = self._run_and_parse(cells, tests)
        t = results_json["tests"][0]
        self.assertEqual(t["score"], 7)
        self.assertFalse(t["passed"])
        self.assertEqual(t["status"], "partial")

    # -- Cell output captured ------------------------------------------------

    def test_cell_stdout_captured(self):
        """Print statements in cells appear as cell output."""
        cells = ["print('hello from cell')"]
        tests = ""
        results_json, _, _, _ = self._run_and_parse(cells, tests)
        code_cells = [c for c in results_json["output_data"]["cells"] if c.get("cell_type") == "code"]
        self.assertEqual(len(code_cells), 1)
        stdout_outputs = [o for o in code_cells[0]["outputs"] if o.get("name") == "stdout"]
        self.assertTrue(len(stdout_outputs) > 0)
        self.assertIn("hello from cell", stdout_outputs[0]["text"])

    # -- Markdown cells pass through ----------------------------------------

    def test_markdown_cells_preserved(self):
        """Markdown cells appear in output without execution."""
        cells_list = [
            {"type": "markdown", "source": "# Title", "idx": 0},
            {"type": "code", "source": "x = 1", "idx": 1},
        ]
        stdout, stderr = _exec_notebook_python_template(cells_list, "")
        m = re.search(r"<<<RESULTS_START>>>\s*(.*?)\s*<<<RESULTS_END>>>", stdout, re.DOTALL)
        results_json = json.loads(m.group(1))
        cell_types = [c["cell_type"] for c in results_json["output_data"]["cells"]]
        self.assertEqual(cell_types, ["markdown", "code"])

    # -- IPython magics and shell escapes -----------------------------------

    @staticmethod
    def _cell_stream(results_json, idx: int, name: str) -> str:
        """Concatenated text of the 'stdout'/'stderr' stream outputs on code cell *idx*."""
        cell = results_json["output_data"]["cells"][idx]
        return "".join(
            o.get("text", "")
            for o in cell["outputs"]
            if o.get("output_type") == "stream" and o.get("name") == name
        )

    @staticmethod
    def _cell_has_error(results_json, idx: int) -> bool:
        cell = results_json["output_data"]["cells"][idx]
        return any(o.get("output_type") == "error" for o in cell["outputs"])

    def test_matplotlib_inline_magic_does_not_break_cell(self):
        """`%matplotlib inline` is dropped silently; imports in the same cell still run."""
        cells = ["import math\n%matplotlib inline\nroot = math.sqrt(16)"]
        tests = """
@test(name="root", points=1)
def root_test():
    assert root == 4
"""
        results_json, _, _, _ = self._run_and_parse(cells, tests)
        self.assertTrue(results_json["tests"][0]["passed"])
        self.assertFalse(self._cell_has_error(results_json, 0))
        self.assertEqual(self._cell_stream(results_json, 0, "stderr"), "")

    def test_unknown_line_magic_dropped_with_note(self):
        cells = ["%who\ny = 2"]
        tests = """
@test(name="y", points=1)
def y_test():
    assert y == 2
"""
        results_json, _, _, _ = self._run_and_parse(cells, tests)
        self.assertTrue(results_json["tests"][0]["passed"])
        self.assertIn("Ignored IPython magic", self._cell_stream(results_json, 0, "stderr"))

    def test_time_line_magic_runs_statement(self):
        cells = ["%time total = sum(range(10))"]
        tests = """
@test(name="total", points=1)
def total_test():
    assert total == 45
"""
        results_json, _, _, _ = self._run_and_parse(cells, tests)
        self.assertTrue(results_json["tests"][0]["passed"])

    def test_pip_install_lines_skipped(self):
        cells = ["%pip install somepkg\n!pip install otherpkg\nw = 1"]
        tests = """
@test(name="w", points=1)
def w_test():
    assert w == 1
"""
        results_json, _, _, _ = self._run_and_parse(cells, tests)
        self.assertTrue(results_json["tests"][0]["passed"])
        self.assertFalse(self._cell_has_error(results_json, 0))
        stderr = self._cell_stream(results_json, 0, "stderr")
        self.assertIn("Skipped package install magic", stderr)
        self.assertIn("Skipped package install command", stderr)

    def test_shell_escape_runs_and_captures_output(self):
        cells = ["!echo hello-from-shell\nv = 3"]
        tests = """
@test(name="v", points=1)
def v_test():
    assert v == 3
"""
        results_json, _, _, _ = self._run_and_parse(cells, tests)
        self.assertTrue(results_json["tests"][0]["passed"])
        self.assertIn("hello-from-shell", self._cell_stream(results_json, 0, "stdout"))

    def test_writefile_cell_magic_creates_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "helper.py")
            cells = [
                f"%%writefile {path}\ndef helper():\n    return 'from-file'",
                f"content = open({path!r}).read()",
            ]
            tests = """
@test(name="content", points=1)
def content_test():
    assert "def helper" in content
"""
            results_json, _, _, _ = self._run_and_parse(cells, tests)
            self.assertTrue(results_json["tests"][0]["passed"])
            self.assertIn("Writing", self._cell_stream(results_json, 0, "stdout"))

    def test_bash_cell_magic_runs_body(self):
        cells = ["%%bash\necho from-bash\necho second-line"]
        results_json, _, _, _ = self._run_and_parse(cells, "")
        out = self._cell_stream(results_json, 0, "stdout")
        self.assertIn("from-bash", out)
        self.assertIn("second-line", out)
        self.assertFalse(self._cell_has_error(results_json, 0))

    def test_time_cell_magic_runs_body(self):
        cells = ["%%time\nt = 5 * 5"]
        tests = """
@test(name="t", points=1)
def t_test():
    assert t == 25
"""
        results_json, _, _, _ = self._run_and_parse(cells, tests)
        self.assertTrue(results_json["tests"][0]["passed"])

    def test_unsupported_cell_magic_skips_cell(self):
        cells = ["%%html\n<b>hi</b>", "u = 9"]
        tests = """
@test(name="u", points=1)
def u_test():
    assert u == 9
"""
        results_json, _, _, _ = self._run_and_parse(cells, tests)
        self.assertTrue(results_json["tests"][0]["passed"])
        self.assertFalse(self._cell_has_error(results_json, 0))
        self.assertIn("Skipped cell", self._cell_stream(results_json, 0, "stderr"))

    def test_display_shim_prints_repr(self):
        cells = ["display(42)"]
        results_json, _, _, _ = self._run_and_parse(cells, "")
        self.assertIn("42", self._cell_stream(results_json, 0, "stdout"))

    def test_percent_inside_string_literal_untouched(self):
        """Valid Python is never rewritten, even if a line starts with % or !."""
        cells = ["s = '''\n%not a magic\n!not a shell\n'''"]
        tests = """
@test(name="s", points=1)
def s_test():
    assert "%not a magic" in s and "!not a shell" in s
"""
        results_json, _, _, _ = self._run_and_parse(cells, tests)
        self.assertTrue(results_json["tests"][0]["passed"])
        self.assertEqual(self._cell_stream(results_json, 0, "stderr"), "")

    def test_real_syntax_error_alongside_magic_still_reported(self):
        cells = ["%matplotlib inline\ndef broken(:\n    pass"]
        results_json, _, _, _ = self._run_and_parse(cells, "")
        self.assertTrue(self._cell_has_error(results_json, 0))

    # -- Target function filtering ------------------------------------------

    def test_target_function_filters_tests(self):
        """Only the targeted test function runs when target_function is specified."""
        cells = ["val = 5"]
        tests = """
@test(name="run_me", points=1)
def run_me():
    assert val == 5

@test(name="skip_me", points=1)
def skip_me():
    assert False
"""
        results_json, _, _, _ = self._run_and_parse(cells, tests, target_function="run_me")
        self.assertEqual(len(results_json["tests"]), 1)
        self.assertEqual(results_json["tests"][0]["name"], "run_me")
        self.assertTrue(results_json["tests"][0]["passed"])

    # -- Verify integration with TestService --------------------------------

    def test_round_trip_through_verify(self):
        """Full pipeline: notebook exec → parse → verify_script_test."""
        cells = ["def double(n): return n * 2"]
        tests = """
@test(name="test_double", points=10)
def test_double():
    assert double(4) == 8
"""
        results_json, per_test, stdout, stderr = self._run_and_parse(cells, tests)
        exec_result = {
            "stdout": stdout,
            "stderr": stderr,
            "error": None,
            "tests": per_test,
        }
        v = TestService.verify_script_test(cast(Any, None), exec_result)
        self.assertTrue(v["passed"])
        self.assertEqual(v["score"], 10)
        self.assertEqual(v["maxScore"], 10)

    # -- Expression result display ------------------------------------------

    def test_last_expression_displayed(self):
        """The last expression in a cell is displayed in output (repr)."""
        cells = ["2 + 3"]
        tests = ""
        results_json, _, _, _ = self._run_and_parse(cells, tests)
        code_cells = [c for c in results_json["output_data"]["cells"] if c.get("cell_type") == "code"]
        stdout_outputs = [o for o in code_cells[0]["outputs"] if o.get("name") == "stdout"]
        self.assertTrue(len(stdout_outputs) > 0)
        self.assertIn("5", stdout_outputs[0]["text"])


# ###################################################################
# PART B3 — JS template round-trip execution tests
# ###################################################################

class JSTemplateRoundTripTests(SimpleTestCase):
    """
    Fills the real ``template.js`` with student code + base64 test code,
    runs it via Node.js subprocess, and verifies the
    ``<<<TEST_RESULT_JSON_START>>>`` output.
    """

    def _run_and_parse(self, student_code: str, test_code: str):
        """Execute template, parse test result markers.
        Returns (results_list, stdout, stderr).
        """
        stdout, stderr = _exec_js_template(student_code, test_code)
        _, _, results = Executor.parse_test_results(stdout, stderr)
        return results, stdout, stderr

    # -- Basic passing ------------------------------------------------------

    def test_single_passing_test(self):
        """One passing test → correct JSON."""
        student = "function add(a, b) { return a + b; }"
        tests = """
test("add works", 5, function() {
    if (add(2, 3) !== 5) throw new Error("Expected 5");
});
"""
        results, stdout, stderr = self._run_and_parse(student, tests)
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0]["name"], "add works")
        self.assertTrue(results[0]["passed"])
        self.assertEqual(results[0]["score"], 5)

    # -- Failing test -------------------------------------------------------

    def test_failing_test(self):
        """Failed assertion → passed=false."""
        student = "function greet() { return 'hi'; }"
        tests = """
test("wrong", 3, function() {
    if (greet() !== "hello") throw new Error("Expected hello");
});
"""
        results, _, _ = self._run_and_parse(student, tests)
        self.assertEqual(len(results), 1)
        self.assertFalse(results[0]["passed"])

    # -- Multiple tests -----------------------------------------------------

    def test_multiple_tests(self):
        """Two tests emitted, both parsed."""
        student = "var x = 10;"
        tests = """
test("pass", 1, function() {
    if (x !== 10) throw new Error("wrong");
});
test("fail", 2, function() {
    if (x !== 99) throw new Error("wrong");
});
"""
        results, _, _ = self._run_and_parse(student, tests)
        self.assertEqual(len(results), 2)
        names = {r["name"]: r for r in results}
        self.assertTrue(names["pass"]["passed"])
        self.assertFalse(names["fail"]["passed"])

    # -- Partial credit via return ------------------------------------------

    def test_partial_credit_numeric_return(self):
        """Returning a number gives partial credit."""
        student = ""
        tests = """
test("partial", 10, function() {
    return 7;
});
"""
        results, _, _ = self._run_and_parse(student, tests)
        self.assertEqual(results[0]["score"], 7)

    # -- Crash in test script -----------------------------------------------

    def test_crash_in_test_code_still_produces_results(self):
        """If the test code throws during setup, synthetic error test is emitted."""
        student = ""
        tests = "throw new Error('test script went boom');"
        results, _, _ = self._run_and_parse(student, tests)
        # The template wraps eval errors and registers a "Test Script Execution" test
        self.assertTrue(len(results) >= 1)
        # The crash test should have error
        self.assertFalse(results[0]["passed"])

    # -- Student syntax error -----------------------------------------------

    def test_student_syntax_error_flagged(self):
        """Bad student code at runtime sets STUDENT_CODE_SYNTAX_INVALID, tests get error."""
        # Use eval() to trigger a runtime SyntaxError that the template's
        # try/catch can handle (a literal syntax error in the file would
        # prevent Node from parsing the script at all).
        student = "eval('function broken( {');"
        tests = """
test("after_error", 1, function() {
    return; // should still register
});
"""
        results, _, stderr = self._run_and_parse(student, tests)
        self.assertTrue(len(results) >= 1)
        # The test should be marked with syntax error context
        self.assertFalse(results[0]["passed"])

    # -- Full pipeline through verify ---------------------------------------

    def test_round_trip_through_verify(self):
        """Full pipeline: template → node → parse → verify_script_test."""
        student = "function mul(a, b) { return a * b; }"
        tests = """
test("mul works", 10, function() {
    if (mul(3, 4) !== 12) throw new Error("Expected 12");
});
"""
        results, stdout, stderr = self._run_and_parse(student, tests)
        exec_result = {
            "stdout": stdout,
            "stderr": stderr,
            "error": None,
            "tests": results,
        }
        v = TestService.verify_script_test(cast(Any, None), exec_result)
        self.assertTrue(v["passed"])
        self.assertEqual(v["score"], 10)

    # -- No tests registered still works ------------------------------------

    def test_no_tests_no_crash(self):
        """Template with no test() calls doesn't crash."""
        student = "const x = 1;"
        tests = ""
        results, stdout, stderr = self._run_and_parse(student, tests)
        self.assertEqual(len(results), 0)


# ###################################################################
# PART B4 — JS Notebook template round-trip execution tests
# ###################################################################

class JSNotebookTemplateRoundTripTests(SimpleTestCase):
    """
    Fills the real ``notebook_template.js`` with base64-encoded cells and test
    code, runs via Node.js subprocess, and verifies the combined JSON output.
    """

    @staticmethod
    def _make_cells(sources: List[str]) -> List[Dict[str, Any]]:
        return [{"type": "code", "source": s, "idx": i} for i, s in enumerate(sources)]

    def _run_and_parse(self, cell_sources: List[str], test_code: str):
        """Returns (results_json_from_stdout, per_test_results, stdout, stderr)."""
        stdout, stderr = _exec_notebook_js_template(self._make_cells(cell_sources), test_code)
        # Parse <<<RESULTS_START>>> from stdout
        results_json = None
        m = re.search(r"<<<RESULTS_START>>>\s*(.*?)\s*<<<RESULTS_END>>>", stdout, re.DOTALL)
        if m:
            results_json = json.loads(m.group(1))
        _, _, per_test = Executor.parse_test_results(stdout, stderr)
        return results_json, per_test, stdout, stderr

    # -- Basic passing test -------------------------------------------------

    def test_single_passing_test(self):
        """One cell defines a function, test verifies it."""
        cells = ["function add(a, b) { return a + b; }"]
        tests = """
test("add works", 5, function() {
    if (add(2, 3) !== 5) throw new Error("Expected 5");
});
"""
        results_json, per_test, _, _ = self._run_and_parse(cells, tests)
        self.assertIsNotNone(results_json)
        self.assertTrue(results_json["success"])
        # JS notebook: test results in per_test (via <<<TEST_RESULT_JSON_START>>>), not in results_json
        self.assertEqual(len(per_test), 1)
        self.assertTrue(per_test[0]["passed"])
        self.assertEqual(per_test[0]["score"], 5)

    # -- Multi-cell state persistence ---------------------------------------

    def test_multi_cell_state_persists(self):
        """Variables from earlier cells are accessible in later cells."""
        cells = [
            "var x = 10;",
            "var y = x * 2;",
        ]
        tests = """
test("state", 1, function() {
    if (y !== 20) throw new Error("y should be 20 but was " + y);
});
"""
        results_json, per_test, _, _ = self._run_and_parse(cells, tests)
        self.assertTrue(per_test[0]["passed"])

    # -- Failing test -------------------------------------------------------

    def test_failing_assertion(self):
        """Failed assertion → passed=false."""
        cells = ["var result = 42;"]
        tests = """
test("wrong", 3, function() {
    if (result !== 99) throw new Error("not 99");
});
"""
        results_json, per_test, _, _ = self._run_and_parse(cells, tests)
        self.assertFalse(per_test[0]["passed"])

    # -- Cell output captured -----------------------------------------------

    def test_cell_stdout_captured(self):
        """console.log in cells appears in cell output."""
        cells = ["console.log('hello from cell');"]
        tests = ""
        results_json, _, _, _ = self._run_and_parse(cells, tests)
        code_cells = [c for c in results_json["output_data"]["cells"] if c.get("cell_type") == "code"]
        self.assertEqual(len(code_cells), 1)
        stdout_outputs = [o for o in code_cells[0]["outputs"] if o.get("name") == "stdout"]
        self.assertTrue(len(stdout_outputs) > 0)
        self.assertIn("hello from cell", stdout_outputs[0]["text"])

    # -- Markdown cells pass through ----------------------------------------

    def test_markdown_cells_preserved(self):
        """Markdown cells appear in results without execution."""
        cells_list = [
            {"type": "markdown", "source": "# Title", "idx": 0},
            {"type": "code", "source": "var x = 1;", "idx": 1},
        ]
        stdout, stderr = _exec_notebook_js_template(cells_list, "")
        m = re.search(r"<<<RESULTS_START>>>\s*(.*?)\s*<<<RESULTS_END>>>", stdout, re.DOTALL)
        results_json = json.loads(m.group(1))
        cell_types = [c["cell_type"] for c in results_json["output_data"]["cells"]]
        self.assertEqual(cell_types, ["markdown", "code"])

    # -- Test script crash --------------------------------------------------

    def test_crash_produces_error(self):
        """If the test script throws, an error test result appears."""
        cells = ["var x = 1;"]
        tests = "throw new Error('test went boom');"
        results_json, per_test, _, _ = self._run_and_parse(cells, tests)
        self.assertTrue(len(per_test) >= 1)
        self.assertFalse(per_test[0]["passed"])

    # -- Full pipeline through verify ---------------------------------------

    def test_round_trip_through_verify(self):
        """Full pipeline: notebook exec → parse → verify_script_test."""
        cells = ["function double(n) { return n * 2; }"]
        tests = """
test("test_double", 10, function() {
    if (double(4) !== 8) throw new Error("Expected 8");
});
"""
        results_json, per_test, stdout, stderr = self._run_and_parse(cells, tests)
        exec_result = {
            "stdout": stdout,
            "stderr": stderr,
            "error": None,
            "tests": per_test,
        }
        v = TestService.verify_script_test(cast(Any, None), exec_result)
        self.assertTrue(v["passed"])
        self.assertEqual(v["score"], 10)


# ###################################################################
# PART B5 — C++ template execution tests
# ###################################################################

class CppTemplateRoundTripTests(SimpleTestCase):
    """
    Compiles and runs the real ``template.cpp`` with student code + test code
    via g++, then verifies the ``<<<TEST_RESULT_JSON_START>>>`` output.
    """

    def _run_and_parse(self, student_code: str, test_code: str):
        """Compile, run, and parse C++ template results.
        Returns (results_list, stdout, stderr).
        """
        stdout, stderr = _exec_cpp_template(student_code, test_code)
        _, _, results = Executor.parse_test_results(stdout, stderr)
        return results, stdout, stderr

    # -- Basic passing -------------------------------------------------------

    def test_single_passing_test(self):
        """One passing test → correct JSON."""
        student = "int add(int a, int b) { return a + b; }"
        tests = """
extern int add(int, int);
TEST(add_works, 5) {
    assertTrue(add(2, 3) == 5, "Expected 5");
}
"""
        results, _, _ = self._run_and_parse(student, tests)
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0]["name"], "add_works")
        self.assertTrue(results[0]["passed"])
        self.assertEqual(results[0]["score"], 5)

    # -- Failing test -------------------------------------------------------

    def test_failing_assertion(self):
        """Failed assertion → passed=false, status='failed'."""
        student = "int val() { return 42; }"
        tests = """
extern int val();
TEST(wrong_value, 3) {
    assertTrue(val() == 99, "not 99");
}
"""
        results, _, _ = self._run_and_parse(student, tests)
        self.assertEqual(len(results), 1)
        self.assertFalse(results[0]["passed"])
        self.assertEqual(results[0]["status"], "failed")
        self.assertIn("not 99", results[0]["error"])

    # -- Runtime exception → error -------------------------------------------

    def test_exception_gives_error_status(self):
        """An exception during test → status='error'."""
        student = "#include <stdexcept>\nvoid boom() { throw std::runtime_error(\"kaboom\"); }"
        tests = """
extern void boom();
TEST(exception_test, 2) {
    boom();
}
"""
        results, _, _ = self._run_and_parse(student, tests)
        self.assertEqual(len(results), 1)
        self.assertFalse(results[0]["passed"])
        self.assertEqual(results[0]["status"], "error")
        self.assertIn("kaboom", results[0]["error"])

    # -- Multiple tests -----------------------------------------------------

    def test_multiple_tests(self):
        """Two tests: one pass, one fail."""
        student = "int square(int x) { return x * x; }"
        tests = """
extern int square(int);
TEST(square_4, 5) {
    assertTrue(square(4) == 16, "4^2 should be 16");
}

TEST(square_wrong, 2) {
    assertTrue(square(3) == 10, "3^2 is not 10");
}
"""
        results, _, _ = self._run_and_parse(student, tests)
        self.assertEqual(len(results), 2)
        names = {r["name"]: r for r in results}
        self.assertTrue(names["square_4"]["passed"])
        self.assertFalse(names["square_wrong"]["passed"])
        self.assertEqual(names["square_wrong"]["status"], "failed")

    # -- Partial credit -----------------------------------------------------

    def test_partial_credit_numeric(self):
        """Returning partial score via return_score()."""
        student = ""
        tests = """
TEST_PARTIAL(partial, 10) {
    return return_score(7, "only 7 out of 10");
}
"""
        results, _, _ = self._run_and_parse(student, tests)
        self.assertEqual(results[0]["score"], 7)
        self.assertEqual(results[0]["status"], "partial")
        self.assertIn("only 7 out of 10", results[0]["message"])

    # -- Output captured ----------------------------------------------------

    def test_stdout_captured(self):
        """cout output inside test is captured in the output field."""
        student = ""
        tests = """
TEST(output_test, 1) {
    std::cout << "hello from test" << std::endl;
}
"""
        results, _, _ = self._run_and_parse(student, tests)
        self.assertIn("hello from test", results[0]["output"])

    # -- Round-trip through verify ------------------------------------------

    def test_round_trip_through_verify(self):
        """Full pipeline: compile → run → parse → verify_script_test."""
        student = "int mul(int a, int b) { return a * b; }"
        tests = """
extern int mul(int, int);
TEST(mul_works, 10) {
    assertTrue(mul(3, 4) == 12, "Expected 12");
}
"""
        results, stdout, stderr = self._run_and_parse(student, tests)
        exec_result = {
            "stdout": stdout,
            "stderr": stderr,
            "error": None,
            "tests": results,
        }
        v = TestService.verify_script_test(cast(Any, None), exec_result)
        self.assertTrue(v["passed"])
        self.assertEqual(v["score"], 10)


# ###################################################################
# PART B6 — Java template execution tests
# ###################################################################

class JavaTemplateRoundTripTests(SimpleTestCase):
    """
    Compiles and runs the real ``TestRunner.java`` with student code + test code
    via javac/java, then verifies the ``<<<TEST_RESULT_JSON_START>>>`` output.
    """

    def _run_and_parse(self, student_code: str, test_code: str):
        """Compile, run, and parse Java template results.
        Returns (results_list, stdout, stderr).
        """
        stdout, stderr = _exec_java_template(student_code, test_code)
        _, _, results = Executor.parse_test_results(stdout, stderr)
        return results, stdout, stderr

    # -- Basic passing -------------------------------------------------------

    def test_single_passing_test(self):
        """One passing test → correct JSON."""
        student = "public class Student { public static int add(int a, int b) { return a + b; } }"
        tests = """
    @Test(name = "add works", points = 5)
    public void testAdd() {
        assertEquals(5, Student.add(2, 3));
    }
"""
        results, _, _ = self._run_and_parse(student, tests)
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0]["name"], "add works")
        self.assertTrue(results[0]["passed"])
        self.assertEqual(results[0]["score"], 5)

    # -- Failing test -------------------------------------------------------

    def test_failing_assertion(self):
        """Failed assertion → passed=false, status='failed'."""
        student = "public class Student { public static int val() { return 42; } }"
        tests = """
    @Test(name = "wrong", points = 3)
    public void testWrong() {
        assertEquals(99, Student.val());
    }
"""
        results, _, _ = self._run_and_parse(student, tests)
        self.assertEqual(len(results), 1)
        self.assertFalse(results[0]["passed"])
        self.assertEqual(results[0]["status"], "failed")

    # -- Runtime exception → error -------------------------------------------

    def test_exception_gives_error_status(self):
        """A runtime exception during test → status='error'."""
        student = "public class Student { public static void boom() { throw new RuntimeException(\"kaboom\"); } }"
        tests = """
    @Test(name = "boom_test", points = 2)
    public void testBoom() {
        Student.boom();
    }
"""
        results, _, _ = self._run_and_parse(student, tests)
        self.assertEqual(len(results), 1)
        self.assertFalse(results[0]["passed"])
        self.assertEqual(results[0]["status"], "error")

    # -- Multiple tests -----------------------------------------------------

    def test_multiple_tests(self):
        """Two tests: one pass, one fail."""
        student = "public class Student { public static int square(int x) { return x * x; } }"
        tests = """
    @Test(name = "square_4", points = 5)
    public void testSquare4() {
        assertEquals(16, Student.square(4));
    }

    @Test(name = "square_wrong", points = 2)
    public void testSquareWrong() {
        assertEquals(10, Student.square(3));
    }
"""
        results, _, _ = self._run_and_parse(student, tests)
        self.assertEqual(len(results), 2)
        names = {r["name"]: r for r in results}
        self.assertTrue(names["square_4"]["passed"])
        self.assertFalse(names["square_wrong"]["passed"])

    # -- Partial credit -----------------------------------------------------

    def test_partial_credit_numeric(self):
        """Returning a Number gives partial credit."""
        student = "public class Student {}"
        tests = """
    @Test(name = "partial", points = 10)
    public Object testPartial() {
        return 7;
    }
"""
        results, _, _ = self._run_and_parse(student, tests)
        self.assertEqual(results[0]["score"], 7)
        self.assertEqual(results[0]["status"], "partial")

    # -- Round-trip through verify ------------------------------------------

    def test_round_trip_through_verify(self):
        """Full pipeline: compile → run → parse → verify_script_test."""
        student = "public class Student { public static int mul(int a, int b) { return a * b; } }"
        tests = """
    @Test(name = "mul works", points = 10)
    public void testMul() {
        assertEquals(12, Student.mul(3, 4));
    }
"""
        results, stdout, stderr = self._run_and_parse(student, tests)
        exec_result = {
            "stdout": stdout,
            "stderr": stderr,
            "error": None,
            "tests": results,
        }
        v = TestService.verify_script_test(cast(Any, None), exec_result)
        self.assertTrue(v["passed"])
        self.assertEqual(v["score"], 10)


# ###################################################################
# PART B6b — Real JUnit (JUnitPlatformRunner) round-trip execution
# ###################################################################

class JUnitPlatformRunnerRoundTripTests(SimpleTestCase):
    """
    Resolves the pinned JUnit/Mockito jars (via libs-pom.xml + mvn), compiles a
    student class + an UNMODIFIED JUnit test + the real JUnitPlatformRunner.java
    (rendered through the executor's own helpers), runs it, and verifies the
    emitted JSON — the whole JUnit path short of Docker.
    """

    _libs_dir = None  # resolved once per class

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        from unittest import SkipTest
        for tool in ("javac", "java", "mvn"):
            if not shutil.which(tool):
                raise SkipTest(f"{tool} not available — skipping JUnit runner round-trip")
        # Resolve the baked libs once into a shared temp dir.
        libs_pom = os.path.join(TEMPLATES_DIR, "..", "..", "testUtils", "libs-pom.xml")
        libs_pom = os.path.abspath(libs_pom)
        cls._libs_dir = tempfile.mkdtemp(prefix="codepost-junit-libs-")
        proc = subprocess.run(
            ["mvn", "-q", "-f", libs_pom, "dependency:copy-dependencies",
             f"-DoutputDirectory={cls._libs_dir}"],
            capture_output=True, text=True, timeout=600,
        )
        if proc.returncode != 0:
            shutil.rmtree(cls._libs_dir, ignore_errors=True)
            cls._libs_dir = None
            raise SkipTest(
                "Could not resolve JUnit libs via mvn (offline?). "
                f"stderr tail:\n{proc.stderr[-800:]}"
            )

    @classmethod
    def tearDownClass(cls):
        if cls._libs_dir:
            shutil.rmtree(cls._libs_dir, ignore_errors=True)
        super().tearDownClass()

    def _render_runner(self, test_code: str, points_by_fn: dict, default_timeout: int = 30) -> str:
        """Render JUnitPlatformRunner.java exactly as JavaExecutor._execute_junit5 does."""
        src = _read_template("JUnitPlatformRunner.java")
        put_lines = "\n        ".join(
            f'm.put({json.dumps(str(fn))}, {float(pts)});' for fn, pts in points_by_fn.items()
        )
        src = src.replace("#{POINTS_JSON}", put_lines)
        src = src.replace("#{DEFAULT_TIMEOUT}", f"{int(default_timeout)}s")
        return src

    def _run(self, student_code: str, student_name: str, test_code: str,
             points_by_fn: dict, agent: bool = False):
        """Compile student + test + runner against the libs, run, return parsed results."""
        libs_glob = os.path.join(self._libs_dir, "*")
        tmpdir = tempfile.mkdtemp()
        try:
            with open(os.path.join(tmpdir, student_name), "w") as f:
                f.write(student_code)
            # Name the test file after its public class.
            from autograder.services.executors.java import JavaExecutor
            test_name = JavaExecutor._test_class_filename(test_code)
            with open(os.path.join(tmpdir, test_name), "w") as f:
                f.write(test_code)
            with open(os.path.join(tmpdir, "JUnitPlatformRunner.java"), "w") as f:
                f.write(self._render_runner(test_code, points_by_fn))

            os.makedirs(os.path.join(tmpdir, "out"), exist_ok=True)
            sources = [os.path.join(tmpdir, n) for n in os.listdir(tmpdir) if n.endswith(".java")]
            compile_proc = subprocess.run(
                ["javac", "-cp", libs_glob, "-d", "out", *sources],
                capture_output=True, text=True, timeout=120, cwd=tmpdir,
            )
            if compile_proc.returncode != 0:
                return None, "", compile_proc.stderr

            run_cmd = ["java", "-ea"]
            if agent:
                import glob as _glob
                agents = _glob.glob(os.path.join(self._libs_dir, "byte-buddy-agent-*.jar"))
                if agents:
                    run_cmd.append(f"-javaagent:{agents[0]}")
            run_cmd += ["-cp", f"{libs_glob}:out", "JUnitPlatformRunner"]
            run_proc = subprocess.run(
                run_cmd, capture_output=True, text=True, timeout=120, cwd=tmpdir,
            )
            _, _, results = Executor.parse_test_results(run_proc.stdout, run_proc.stderr)
            return results, run_proc.stdout, run_proc.stderr
        finally:
            shutil.rmtree(tmpdir, ignore_errors=True)

    IMPORTS = (
        "import org.junit.jupiter.api.Test;\n"
        "import static org.junit.jupiter.api.Assertions.*;\n"
    )

    def test_pass_fail_and_error_statuses(self):
        student = "public class Calc { public static int add(int a,int b){return a+b;} "\
                  "public static void boom(){ throw new RuntimeException(\"x\"); } }"
        test = self.IMPORTS + """
public class CalcTest {
    @Test void testAdd() { assertEquals(3, Calc.add(1, 2)); }
    @Test void testWrong() { assertEquals(99, Calc.add(1, 2)); }
    @Test void testBoom() { Calc.boom(); }
}
"""
        points = {"testAdd": 1.0, "testWrong": 1.0, "testBoom": 1.0}
        results, out, err = self._run(student, "Calc.java", test, points)
        self.assertIsNotNone(results, f"compile failed:\n{err}")
        by = {r["name"]: r for r in results}
        self.assertEqual(set(by), {"testAdd", "testWrong", "testBoom"})
        self.assertEqual(by["testAdd"]["status"], "passed")
        self.assertTrue(by["testAdd"]["passed"])
        self.assertEqual(by["testWrong"]["status"], "failed")   # AssertionError
        self.assertEqual(by["testBoom"]["status"], "error")     # RuntimeException

    def test_points_drive_score(self):
        student = "public class P { public static boolean ok(){return true;} }"
        test = self.IMPORTS + """
public class PTest {
    @Test void worthThree() { assertTrue(P.ok()); }
    @Test void worthOneFails() { assertTrue(false); }
}
"""
        points = {"worthThree": 3.0, "worthOneFails": 1.0}
        results, out, err = self._run(student, "P.java", test, points)
        self.assertIsNotNone(results, f"compile failed:\n{err}")
        by = {r["name"]: r for r in results}
        self.assertEqual(by["worthThree"]["score"], 3.0)
        self.assertEqual(by["worthThree"]["max_score"], 3.0)
        self.assertEqual(by["worthOneFails"]["score"], 0)
        self.assertEqual(by["worthOneFails"]["max_score"], 1.0)

    def test_assertThrows_passes(self):
        # assertThrows is a core reason to run real JUnit instead of the custom harness.
        student = "public class Thrower { public static void f(){ throw new IllegalArgumentException(); } }"
        test = self.IMPORTS + """
public class ThrowerTest {
    @Test void throwsIAE() {
        assertThrows(IllegalArgumentException.class, () -> Thrower.f());
    }
}
"""
        results, out, err = self._run(student, "Thrower.java", test, {"throwsIAE": 1.0})
        self.assertIsNotNone(results, f"compile failed:\n{err}")
        self.assertTrue(results[0]["passed"])

    def test_lab01_reference_test_against_correct_solution(self):
        # The real lab-01 reference test, unmodified, against a correct solution.
        ref = "/Users/I778242/grader/java-course-reference-main-MJT2026/lab-01"
        test_file = os.path.join(ref, "test", "UniqueSubstringFinderTest.java")
        sol_file = os.path.join(ref, "src", "UniqueSubstringFinder.java")
        if not (os.path.exists(test_file) and os.path.exists(sol_file)):
            from unittest import SkipTest
            raise SkipTest("lab-01 reference files not present")
        student = open(sol_file).read()
        test = open(test_file).read()
        from autograder.services.TestParsingService import TestParsingService
        points = {t["functionName"]: t.get("points", 1.0)
                  for t in TestParsingService._parse_java(test)}
        results, out, err = self._run(student, "UniqueSubstringFinder.java", test, points)
        self.assertIsNotNone(results, f"compile failed:\n{err}")
        self.assertEqual(len(results), 10)
        self.assertTrue(all(r["passed"] for r in results),
                        msg=f"not all passed: {[(r['name'], r['status']) for r in results]}")


# ###################################################################
# PART B7 — R template execution tests
# ###################################################################

class RTemplateRoundTripTests(SimpleTestCase):
    """
    Fills and runs the real ``template.r`` via Rscript, then verifies
    the ``<<<TEST_RESULT_JSON_START>>>`` output.
    """

    def _run_and_parse(self, student_code: str, test_code: str):
        """Run R template and parse results.
        Returns (results_list, stdout, stderr).
        """
        stdout, stderr = _exec_r_template(student_code, test_code)
        _, _, results = Executor.parse_test_results(stdout, stderr)
        return results, stdout, stderr

    # -- Basic passing -------------------------------------------------------

    def test_single_passing_test(self):
        """One passing test → correct JSON."""
        student = "add <- function(a, b) a + b"
        tests = """
run_test("add works", 5, function() {
    stopifnot(add(2, 3) == 5)
})
"""
        results, _, _ = self._run_and_parse(student, tests)
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0]["name"], "add works")
        self.assertTrue(results[0]["passed"])
        self.assertEqual(results[0]["score"], 5)

    # -- Failing test -------------------------------------------------------

    def test_failing_assertion(self):
        """Failed assertion → passed=false."""
        student = "val <- 42"
        tests = """
run_test("wrong", 3, function() {
    assertion_error("not 99")
})
"""
        results, _, _ = self._run_and_parse(student, tests)
        self.assertEqual(len(results), 1)
        self.assertFalse(results[0]["passed"])
        self.assertEqual(results[0]["status"], "failed")

    # -- Runtime error → error -----------------------------------------------

    def test_stop_gives_error_status(self):
        """stop() during test → status='error'."""
        student = ""
        tests = """
run_test("boom", 2, function() {
    stop("kaboom")
})
"""
        results, _, _ = self._run_and_parse(student, tests)
        self.assertEqual(len(results), 1)
        self.assertFalse(results[0]["passed"])
        self.assertEqual(results[0]["status"], "error")
        self.assertIn("kaboom", results[0]["error"])

    # -- Multiple tests -----------------------------------------------------

    def test_multiple_tests(self):
        """Two tests: one pass, one fail."""
        student = "sq <- function(x) x * x"
        tests = """
run_test("sq_4", 5, function() {
    stopifnot(sq(4) == 16)
})

run_test("sq_wrong", 2, function() {
    assertion_error("3^2 is not 10")
})
"""
        results, _, _ = self._run_and_parse(student, tests)
        self.assertEqual(len(results), 2)
        names = {r["name"]: r for r in results}
        self.assertTrue(names["sq_4"]["passed"])
        self.assertFalse(names["sq_wrong"]["passed"])

    # -- Partial credit -----------------------------------------------------

    def test_partial_credit_numeric(self):
        """Returning a number gives partial credit."""
        student = ""
        tests = """
run_test("partial", 10, function() {
    return(7)
})
"""
        results, _, _ = self._run_and_parse(student, tests)
        self.assertEqual(results[0]["score"], 7)
        self.assertEqual(results[0]["status"], "partial")

    # -- Crash in test script -----------------------------------------------

    def test_crash_produces_synthetic_error(self):
        """If the test code itself errors, a synthetic error test is emitted."""
        student = ""
        tests = 'stop("test script exploded")'
        results, _, _ = self._run_and_parse(student, tests)
        self.assertTrue(len(results) >= 1)
        self.assertFalse(results[0]["passed"])
        self.assertEqual(results[0]["status"], "error")
        self.assertIn("exploded", results[0]["error"])

    # -- Round-trip through verify ------------------------------------------

    def test_round_trip_through_verify(self):
        """Full pipeline: Rscript → parse → verify_script_test."""
        student = "mul <- function(a, b) a * b"
        tests = """
run_test("mul works", 10, function() {
    stopifnot(mul(3, 4) == 12)
})
"""
        results, stdout, stderr = self._run_and_parse(student, tests)
        exec_result = {
            "stdout": stdout,
            "stderr": stderr,
            "error": None,
            "tests": results,
        }
        v = TestService.verify_script_test(cast(Any, None), exec_result)
        self.assertTrue(v["passed"])
        self.assertEqual(v["score"], 10)


# ###################################################################
# PART B8 — Ruby Notebook template execution tests
# ###################################################################

class RubyNotebookTemplateRoundTripTests(SimpleTestCase):
    """
    Fills and runs the real ``notebook_template.rb`` via ruby subprocess,
    then verifies the ``<<<TEST_RESULT_JSON_START>>>`` output.
    """

    def _run_and_parse(self, cells: List[str], test_code: str):
        """Build cell dicts, fill notebook template, run via ruby, parse results.
        Returns (results_json_from_stdout, per_test_results, stdout, stderr).
        """
        cell_dicts = [
            {"type": "code", "source": src, "idx": i}
            for i, src in enumerate(cells)
        ]
        stdout, stderr = _exec_notebook_ruby_template(cell_dicts, test_code)
        _, _, per_test = Executor.parse_test_results(stdout, stderr)
        # Parse the RESULTS_START JSON for the full notebook result
        results_json = {}
        match = re.search(r"<<<RESULTS_START>>>\s*(.*?)\s*<<<RESULTS_END>>>", stdout, re.DOTALL)
        if match:
            try:
                results_json = json.loads(match.group(1))
            except json.JSONDecodeError:
                pass
        return results_json, per_test, stdout, stderr

    # -- Basic passing -------------------------------------------------------

    def test_single_passing_test(self):
        """One passing test → correct JSON."""
        cells = ["$result = 42"]
        tests = """
run_test("check val", 5) do
    raise AssertionError, "not 42" unless $result == 42
end
"""
        _, per_test, _, _ = self._run_and_parse(cells, tests)
        self.assertEqual(len(per_test), 1)
        self.assertTrue(per_test[0]["passed"])
        self.assertEqual(per_test[0]["score"], 5)

    # -- Failing test -------------------------------------------------------

    def test_failing_assertion(self):
        """Failed assertion → passed=false, status='failed'."""
        cells = ["$result = 42"]
        tests = """
run_test("wrong", 3) do
    raise AssertionError, "not 99"
end
"""
        _, per_test, _, _ = self._run_and_parse(cells, tests)
        self.assertEqual(len(per_test), 1)
        self.assertFalse(per_test[0]["passed"])
        self.assertEqual(per_test[0]["status"], "failed")

    # -- Runtime error → error -----------------------------------------------

    def test_exception_gives_error_status(self):
        """A runtime exception during test → status='error'."""
        cells = ["$x = 1"]
        tests = """
run_test("boom", 2) do
    raise "kaboom"
end
"""
        _, per_test, _, _ = self._run_and_parse(cells, tests)
        self.assertEqual(len(per_test), 1)
        self.assertFalse(per_test[0]["passed"])
        self.assertEqual(per_test[0]["status"], "error")

    # -- Multi-cell state persistence ----------------------------------------

    def test_multi_cell_persistence(self):
        """Variables from one cell are available in later cells."""
        cells = ["$a = 10", "$b = $a + 5"]
        tests = """
run_test("sum ok", 1) do
    raise AssertionError, "expected 15" unless $b == 15
end
"""
        _, per_test, _, _ = self._run_and_parse(cells, tests)
        self.assertTrue(per_test[0]["passed"])

    # -- Test script crash ---------------------------------------------------

    def test_crash_produces_synthetic_error(self):
        """If the test code itself raises, a synthetic error result appears."""
        cells = ["$x = 1"]
        tests = 'raise "test script exploded"'
        _, per_test, _, _ = self._run_and_parse(cells, tests)
        self.assertTrue(len(per_test) >= 1)
        self.assertFalse(per_test[0]["passed"])
        self.assertEqual(per_test[0]["status"], "error")
        self.assertIn("exploded", per_test[0]["error"])

    # -- Round-trip through verify ------------------------------------------

    def test_round_trip_through_verify(self):
        """Full pipeline: ruby → parse → verify_script_test."""
        cells = ["def double(x); x * 2; end"]
        tests = """
run_test("double works", 10) do
    raise AssertionError, "Expected 8" unless double(4) == 8
end
"""
        _, per_test, stdout, stderr = self._run_and_parse(cells, tests)
        exec_result = {
            "stdout": stdout,
            "stderr": stderr,
            "error": None,
            "tests": per_test,
        }
        v = TestService.verify_script_test(cast(Any, None), exec_result)
        self.assertTrue(v["passed"])
        self.assertEqual(v["score"], 10)


# ###################################################################
# PART B9 — PHP Notebook template execution
# ###################################################################

class PHPNotebookTemplateRoundTripTests(SimpleTestCase):
    """
    Fills and runs the real ``notebook_template.php`` via php subprocess,
    then verifies the ``<<<TEST_RESULT_JSON_START>>>`` output.
    """

    def _run_and_parse(self, cells: List[str], test_code: str):
        """Build cell dicts, fill notebook template, run via php, parse results.
        Returns (results_json_from_stdout, per_test_results, stdout, stderr).
        """
        cell_dicts = [
            {"type": "code", "source": src, "idx": i}
            for i, src in enumerate(cells)
        ]
        stdout, stderr = _exec_notebook_php_template(cell_dicts, test_code)
        _, _, per_test = Executor.parse_test_results(stdout, stderr)
        results_json = {}
        match = re.search(r"<<<RESULTS_START>>>\s*(.*?)\s*<<<RESULTS_END>>>", stdout, re.DOTALL)
        if match:
            try:
                results_json = json.loads(match.group(1))
            except json.JSONDecodeError:
                pass
        return results_json, per_test, stdout, stderr

    # -- Basic passing -------------------------------------------------------

    def test_single_passing_test(self):
        """One passing test → correct JSON."""
        cells = ["$result = 42;"]
        tests = """
Tester::test("check val", 5, null, function() {
    global $result;
    if ($result !== 42) throw new AssertionError("not 42");
});
"""
        _, per_test, _, _ = self._run_and_parse(cells, tests)
        self.assertEqual(len(per_test), 1)
        self.assertTrue(per_test[0]["passed"])
        self.assertEqual(per_test[0]["score"], 5)

    # -- Failing test -------------------------------------------------------

    def test_failing_assertion(self):
        """Failed assertion → passed=false, status='failed'."""
        cells = ["$result = 42;"]
        tests = """
Tester::test("wrong", 3, null, function() {
    throw new AssertionError("not 99");
});
"""
        _, per_test, _, _ = self._run_and_parse(cells, tests)
        self.assertEqual(len(per_test), 1)
        self.assertFalse(per_test[0]["passed"])
        self.assertEqual(per_test[0]["status"], "failed")

    # -- Runtime error → error -----------------------------------------------

    def test_exception_gives_error_status(self):
        """A runtime exception during test → status='error'."""
        cells = ["$x = 1;"]
        tests = """
Tester::test("boom", 2, null, function() {
    throw new Exception("kaboom");
});
"""
        _, per_test, _, _ = self._run_and_parse(cells, tests)
        self.assertEqual(len(per_test), 1)
        self.assertFalse(per_test[0]["passed"])
        self.assertEqual(per_test[0]["status"], "error")

    # -- Multi-cell state persistence ----------------------------------------

    def test_multi_cell_persistence(self):
        """Variables from one cell are available in later cells."""
        cells = ["$a = 10;", "$b = $a + 5;"]
        tests = """
Tester::test("sum ok", 1, null, function() {
    global $b;
    if ($b !== 15) throw new AssertionError("expected 15");
});
"""
        _, per_test, _, _ = self._run_and_parse(cells, tests)
        self.assertTrue(per_test[0]["passed"])

    # -- Test script crash ---------------------------------------------------

    def test_crash_produces_synthetic_error(self):
        """If the test code itself raises, a synthetic error result appears."""
        cells = ["$x = 1;"]
        tests = 'throw new Exception("test script exploded");'
        _, per_test, _, _ = self._run_and_parse(cells, tests)
        self.assertTrue(len(per_test) >= 1)
        self.assertFalse(per_test[0]["passed"])
        self.assertEqual(per_test[0]["status"], "error")
        self.assertIn("exploded", per_test[0]["error"])

    # -- Round-trip through verify ------------------------------------------

    def test_round_trip_through_verify(self):
        """Full pipeline: php → parse → verify_script_test."""
        cells = ["function double($x) { return $x * 2; }"]
        tests = """
Tester::test("double works", 10, null, function() {
    if (double(4) !== 8) throw new AssertionError("Expected 8");
});
"""
        _, per_test, stdout, stderr = self._run_and_parse(cells, tests)
        exec_result = {
            "stdout": stdout,
            "stderr": stderr,
            "error": None,
            "tests": per_test,
        }
        v = TestService.verify_script_test(cast(Any, None), exec_result)
        self.assertTrue(v["passed"])
        self.assertEqual(v["score"], 10)


# ###################################################################
# PART B9b — Java notebook template: IJava magics
# ###################################################################

class NotebookJavaTemplateMagicTests(SimpleTestCase):
    """
    Compiles and runs the real ``notebook_template.java`` with javac/java and
    verifies IJava magics (`%maven`, `%jars`, `%%loadFromPOM`) are rewritten
    instead of erroring the cell.
    """

    TEST_CODE = """
class Tests {
    @Test(name = "check", points = 1)
    public void check() {
        assertTrue(CHECK, "check failed");
    }
}
"""

    def _run(self, cell_sources: List[str], check_expr: str):
        cells = [{"type": "code", "source": s, "idx": i} for i, s in enumerate(cell_sources)]
        stdout, stderr = _exec_notebook_java_template(cells, self.TEST_CODE.replace("CHECK", check_expr))
        m = re.search(r"<<<RESULTS_START>>>\s*(.*?)\s*<<<RESULTS_END>>>", stdout, re.DOTALL)
        if m is None:
            self.fail(f"no results block.\nstdout: {stdout}\nstderr: {stderr}")
        # Per-test results are stdout markers parsed host-side, not part of the results JSON.
        _, _, per_test = Executor.parse_test_results(stdout, stderr)
        return json.loads(m.group(1)), per_test

    @staticmethod
    def _outputs(results_json, idx: int):
        return results_json["output_data"]["cells"][idx]["outputs"]

    def _stderr_text(self, results_json, idx: int) -> str:
        return "".join(o.get("text", "") for o in self._outputs(results_json, idx) if o.get("name") == "stderr")

    def _has_error(self, results_json, idx: int) -> bool:
        return any(o.get("output_type") == "error" for o in self._outputs(results_json, idx))

    def test_maven_magic_dropped_with_note(self):
        results, per_test = self._run(["%maven org.apache.commons:commons-lang3:3.12.0\nint x = 40 + 2;"], "x == 42")
        self.assertFalse(self._has_error(results, 0))
        self.assertIn("Skipped %maven", self._stderr_text(results, 0))
        self.assertTrue(per_test and per_test[0]["passed"], per_test)

    def test_load_from_pom_cell_skipped(self):
        results, per_test = self._run(
            ["%%loadFromPOM\n<dependency><groupId>x</groupId></dependency>", "int y = 7;"],
            "y == 7",
        )
        self.assertFalse(self._has_error(results, 0))
        self.assertIn("Skipped cell", self._stderr_text(results, 0))
        self.assertTrue(per_test and per_test[0]["passed"], per_test)

    def test_jars_magic_adds_jar_to_classpath(self):
        javac = shutil.which("javac")
        if not javac:
            from unittest import SkipTest
            raise SkipTest("javac not available")
        tmpdir = tempfile.mkdtemp()
        try:
            # JShell snippets live in their own package, so the class must not be in the default package.
            src = os.path.join(tmpdir, "Hello.java")
            with open(src, "w") as f:
                f.write('package lib; public class Hello { public static String greet() { return "hi-from-jar"; } }')
            subprocess.run([javac, "-d", tmpdir, src], check=True, capture_output=True)
            with zipfile.ZipFile(os.path.join(tmpdir, "hello.jar"), "w") as zf:
                zf.write(os.path.join(tmpdir, "lib", "Hello.class"), "lib/Hello.class")
            results, per_test = self._run(
                [f"%jars {tmpdir}/*.jar", "String g = lib.Hello.greet();"],
                'g.equals("hi-from-jar")',
            )
            self.assertFalse(self._has_error(results, 0))
            self.assertFalse(self._has_error(results, 1))
            self.assertTrue(per_test and per_test[0]["passed"], per_test)
        finally:
            shutil.rmtree(tmpdir, ignore_errors=True)

    def test_modulo_continuation_line_untouched(self):
        """A line starting with `% 3` is Java, not a magic."""
        results, per_test = self._run(["int r = 7\n    % 3;"], "r == 1")
        self.assertFalse(self._has_error(results, 0))
        self.assertTrue(per_test and per_test[0]["passed"], per_test)


# ###################################################################
# PART B9c — C++ notebook: xeus-cling magics
# ###################################################################

class CppNotebookMagicStripTests(SimpleTestCase):
    """xeus-cling magics are commented out before the notebook is handed to g++."""

    def test_line_magic_commented_out(self):
        lines = _strip_cling_magics("int a = 1;\n%timeit a + 1\nint b = 2;").split("\n")
        self.assertEqual(lines[0], "int a = 1;")
        self.assertTrue(lines[1].startswith("//"))
        self.assertEqual(lines[2], "int b = 2;")

    def test_cell_magic_comments_out_whole_cell(self):
        lines = _strip_cling_magics("%%file data.txt\nnot c++\nat all").split("\n")
        self.assertEqual(len(lines), 3)
        self.assertTrue(all(l.startswith("//") for l in lines))

    def test_timeit_cell_magic_keeps_body(self):
        lines = _strip_cling_magics("%%timeit\nint c = 3;").split("\n")
        self.assertTrue(lines[0].startswith("//"))
        self.assertEqual(lines[1], "int c = 3;")

    def test_modulo_continuation_untouched(self):
        src = "int r = 7\n    % 3;"
        self.assertEqual(_strip_cling_magics(src), src)


# ###################################################################
# PART B10 — Executor template-filling tests (all languages)
# ###################################################################

class ExecutorTemplateFillTests(SimpleTestCase):
    """
    Verifies that each language executor's ``_get_code_template`` properly
    replaces all placeholder tokens, by checking that no raw placeholders
    remain in the filled output.
    """

    def _assert_no_placeholders(self, filled: str, name: str):
        """Fail if any well-known placeholder pattern remains unfilled."""
        # Python-style placeholders
        self.assertNotIn("#{FILLER_CODE}", filled, f"{name}: #{'{'}FILLER_CODE{'}'} not replaced")
        self.assertNotIn("#{TEST_CODE}", filled, f"{name}: #{'{'}TEST_CODE{'}'} not replaced")
        self.assertNotIn("#{TARGET_TEST_FUNCTION}", filled, f"{name}: #{'{'}TARGET_TEST_FUNCTION{'}'} not replaced")
        self.assertNotIn("#{STUDENT_FILE_PATH}", filled, f"{name}: #{'{'}STUDENT_FILE_PATH{'}'} not replaced")
        # Notebook-style placeholders
        self.assertNotRegex(filled, r"(?<!')(?<!\w)\{cells_b64\}", f"{name}: cells_b64 not replaced")
        self.assertNotRegex(filled, r"(?<!')(?<!\w)\{test_code_b64\}", f"{name}: test_code_b64 not replaced")

    def test_python_template_filled(self):
        """Python template has all placeholders replaced."""
        filled = _fill_python_template("print('hi')", "@test(name='t', points=1)\ndef t(): pass")
        self._assert_no_placeholders(filled, "Python")

    def test_notebook_python_template_filled(self):
        """Notebook Python template has all placeholders replaced."""
        cells = [{"type": "code", "source": "x = 1", "idx": 0}]
        filled = _fill_notebook_python_template(cells, "@test(name='t', points=1)\ndef t(): pass")
        self._assert_no_placeholders(filled, "NotebookPython")

    def test_js_template_filled(self):
        """JS template has all placeholders replaced."""
        filled = _fill_js_template("const x = 1;", "test('t', 1, () => {});")
        self._assert_no_placeholders(filled, "JS")

    def test_notebook_js_template_filled(self):
        """Notebook JS template has all placeholders replaced."""
        cells = [{"type": "code", "source": "var x = 1;", "idx": 0}]
        filled = _fill_notebook_js_template(cells, "test('t', 1, () => {});")
        self._assert_no_placeholders(filled, "NotebookJS")

    def test_cpp_template_placeholders_present(self):
        """C++ template has expected placeholders in its raw source."""
        src = _read_template("template.cpp")
        # C++ template: student code from file, test code injected via placeholder
        self.assertIn("#{TEST_CODE}", src)

    def test_java_template_placeholders_present(self):
        """Java template has expected placeholders in its raw source."""
        src = _read_template("TestRunner.java")
        self.assertIn("#{TEST_CODE}", src)
        self.assertTrue(len(src) > 100)

    def test_r_template_placeholders_present(self):
        """R template has expected placeholders in its raw source."""
        src = _read_template("template.r")
        # R template: student code from student.R file, test code inline
        self.assertIn("#{TEST_CODE}", src)

    def test_ruby_template_placeholders_present(self):
        """Ruby template has FILLER_CODE comment placeholder."""
        src = _read_template("template.rb")
        # Ruby uses a comment-style placeholder, not #{} syntax
        self.assertIn("# FILLER_CODE", src)

    def test_php_template_placeholders_present(self):
        """PHP template has FILLER_CODE comment placeholder."""
        src = _read_template("template.php")
        # PHP uses a comment-style placeholder
        self.assertIn("# FILLER_CODE", src)


# ===================================================================
# 1. parse_test_results — delimiter extraction from stdout / stderr
# ===================================================================

class ParseTestResultsTests(SimpleTestCase):
    """Tests for ``Executor.parse_test_results``."""

    # -- Single result (Python template style: one JSON object per test, in stderr) --

    def test_single_result_in_stderr(self):
        """Python template emits one object per test to stderr."""
        result = _make_test_result()
        stderr = _wrap_result(result)
        clean_stdout, clean_stderr, tests = Executor.parse_test_results("", stderr)
        self.assertEqual(len(tests), 1)
        self.assertTrue(tests[0]["passed"])
        self.assertEqual(clean_stderr.strip(), "")

    def test_single_result_in_stdout(self):
        """Some templates (C++, Java) write the array to stdout."""
        result = _make_test_result()
        stdout = _wrap_result([result])
        clean_stdout, clean_stderr, tests = Executor.parse_test_results(stdout, "")
        self.assertEqual(len(tests), 1)
        self.assertTrue(tests[0]["passed"])
        self.assertEqual(clean_stdout.strip(), "")

    # -- Array result (JS / C++ / Java / R template style) --

    def test_array_of_results(self):
        results = [
            _make_test_result(name="test_a", passed=True, score=5, max_score=5),
            _make_test_result(name="test_b", passed=False, score=0, max_score=3, status="failed", error="AssertionError"),
        ]
        stdout = _wrap_result(results)
        _, _, tests = Executor.parse_test_results(stdout, "")
        self.assertEqual(len(tests), 2)
        self.assertTrue(tests[0]["passed"])
        self.assertFalse(tests[1]["passed"])

    # -- Multiple separate markers (Python emits one per test call) --

    def test_multiple_separate_markers_in_stderr(self):
        """Python template emits individual markers per test (not wrapped in array)."""
        r1 = _make_test_result(name="test_1", passed=True)
        r2 = _make_test_result(name="test_2", passed=False, status="failed", error="oops")
        stderr = _wrap_result(r1) + "\n" + _wrap_result(r2)
        _, _, tests = Executor.parse_test_results("", stderr)
        self.assertEqual(len(tests), 2)
        self.assertEqual(tests[0]["name"], "test_1")
        self.assertEqual(tests[1]["name"], "test_2")

    # -- Surrounding noise preserved --

    def test_surrounding_output_preserved(self):
        stdout = "some debug line\n" + _wrap_result([_make_test_result()]) + "\nmore output"
        clean_stdout, _, tests = Executor.parse_test_results(stdout, "")
        self.assertEqual(len(tests), 1)
        self.assertIn("some debug line", clean_stdout)
        self.assertIn("more output", clean_stdout)
        self.assertNotIn("TEST_RESULT_JSON", clean_stdout)

    # -- Empty / no markers --

    def test_no_markers_returns_empty(self):
        _, _, tests = Executor.parse_test_results("hello", "world")
        self.assertEqual(tests, [])

    # -- Malformed JSON --

    def test_malformed_json_skipped(self):
        stderr = "<<<TEST_RESULT_JSON_START>>>{bad json<<<TEST_RESULT_JSON_END>>>"
        _, _, tests = Executor.parse_test_results("", stderr)
        self.assertEqual(tests, [])

    # -- Cross-stream: results in both stdout and stderr --

    def test_results_from_both_streams(self):
        r1 = _make_test_result(name="from_stdout")
        r2 = _make_test_result(name="from_stderr")
        _, _, tests = Executor.parse_test_results(_wrap_result(r1), _wrap_result(r2))
        names = {t["name"] for t in tests}
        self.assertEqual(names, {"from_stdout", "from_stderr"})


# ===================================================================
# 2. Template-specific output format tests
# ===================================================================

class PythonTemplateOutputTests(SimpleTestCase):
    """Python template emits individual JSON objects to *stderr*, one per test."""

    def test_passed_test(self):
        payload = {
            "name": "test_addition",
            "max_score": 2.0,
            "description": "Tests basic addition",
            "score": 2.0,
            "passed": True,
            "error": None,
            "message": None,
            "output": "stdout from test",
            "status": "passed",
        }
        _, _, tests = Executor.parse_test_results("", _wrap_result(payload))
        self.assertEqual(len(tests), 1)
        self.assertTrue(tests[0]["passed"])
        self.assertEqual(tests[0]["score"], 2.0)

    def test_failed_test_with_error(self):
        payload = {
            "name": "test_subtraction",
            "max_score": 1.0,
            "description": None,
            "score": 0,
            "passed": False,
            "error": "AssertionError: 3 != 4",
            "message": None,
            "output": "",
            "status": "failed",
        }
        _, _, tests = Executor.parse_test_results("", _wrap_result(payload))
        self.assertEqual(tests[0]["status"], "failed")
        self.assertIn("AssertionError", tests[0]["error"])

    def test_partial_credit(self):
        payload = {
            "name": "test_partial",
            "max_score": 10.0,
            "description": None,
            "score": 7.5,
            "passed": False,
            "error": None,
            "message": "Got 3 out of 4 cases",
            "output": "",
            "status": "partial",
        }
        _, _, tests = Executor.parse_test_results("", _wrap_result(payload))
        self.assertEqual(tests[0]["status"], "partial")
        self.assertEqual(tests[0]["score"], 7.5)

    def test_syntax_error_status(self):
        payload = {
            "name": "test_foo",
            "max_score": 1.0,
            "description": None,
            "score": 0,
            "passed": False,
            "error": "Student code syntax was invalid. Fix syntax errors before running tests.",
            "message": "Student code syntax was invalid. Fix syntax errors before running tests.",
            "output": "",
            "status": "error",
        }
        _, _, tests = Executor.parse_test_results("", _wrap_result(payload))
        self.assertEqual(tests[0]["status"], "error")


class JavascriptTemplateOutputTests(SimpleTestCase):
    """JS template emits an array to stdout."""

    def test_array_output(self):
        results = [
            {"name": "test_concat", "max_score": 1, "description": None, "message": "", "score": 1, "passed": True, "status": "passed", "error": ""},
            {"name": "test_length", "max_score": 2, "description": None, "message": "", "score": 0, "passed": False, "status": "failed", "error": "Expected 5, got 3"},
        ]
        stdout = _wrap_result(results)
        _, _, tests = Executor.parse_test_results(stdout, "")
        self.assertEqual(len(tests), 2)
        self.assertEqual(tests[1]["error"], "Expected 5, got 3")

    def test_timeout_status(self):
        results = [
            {"name": "test_slow", "max_score": 1, "description": None, "message": "", "score": 0, "passed": False, "status": "error", "error": "Test timed out after 30s"},
        ]
        _, _, tests = Executor.parse_test_results(_wrap_result(results), "")
        self.assertEqual(tests[0]["status"], "error")
        self.assertIn("timed out", tests[0]["error"])


class CppTemplateOutputTests(SimpleTestCase):
    """C++ template emits a JSON array to stdout."""

    def test_cpp_array_output(self):
        results = [
            {"name": "TestBasic", "description": "", "message": "", "score": 3.0, "max_score": 3.0, "passed": True, "status": "passed", "error": "", "output": ""},
            {"name": "TestEdge", "description": "edge cases", "message": "", "score": 0.0, "max_score": 2.0, "passed": False, "status": "failed", "error": "Assertion failed", "output": "some debug"},
        ]
        stdout = _wrap_result(results)
        _, _, tests = Executor.parse_test_results(stdout, "")
        self.assertEqual(len(tests), 2)
        self.assertEqual(tests[0]["score"], 3.0)


class JavaTemplateOutputTests(SimpleTestCase):
    """Java template emits a JSON array to stdout."""

    def test_java_array_output(self):
        results = [
            {"name": "testAdd", "description": "", "message": "", "score": 1.0, "max_score": 1.0, "passed": True, "status": "passed", "error": None, "output": ""},
        ]
        stdout = _wrap_result(results)
        _, _, tests = Executor.parse_test_results(stdout, "")
        self.assertEqual(len(tests), 1)
        self.assertTrue(tests[0]["passed"])

    def test_java_reflection_timeout(self):
        results = [
            {"name": "testInfiniteLoop", "description": "", "message": "", "score": 0, "max_score": 5, "passed": False, "status": "error", "error": "Test timed out after 30s", "output": ""},
        ]
        _, _, tests = Executor.parse_test_results(_wrap_result(results), "")
        self.assertEqual(tests[0]["status"], "error")


class RTemplateOutputTests(SimpleTestCase):
    """R template emits a JSON array (via jsonlite) to stdout."""

    def test_r_jsonlite_output(self):
        results = [
            {"name": "Test R Logic", "max_score": 2, "description": None, "message": "", "score": 2, "passed": True, "status": "passed", "error": ""},
        ]
        stdout = f"<<<TEST_RESULT_JSON_START>>>{json.dumps(results)}<<<TEST_RESULT_JSON_END>>>"
        _, _, tests = Executor.parse_test_results(stdout, "")
        self.assertEqual(len(tests), 1)
        self.assertTrue(tests[0]["passed"])

    def test_r_syntax_invalid(self):
        results = [
            {"name": "Test R", "max_score": 1, "description": None, "message": "Student code syntax was invalid. Fix syntax errors before running tests.", "score": 0, "passed": False, "status": "error", "error": "Student code syntax was invalid. Fix syntax errors before running tests.\nError in parse: unexpected symbol"},
        ]
        _, _, tests = Executor.parse_test_results(_wrap_result(results), "")
        self.assertEqual(tests[0]["status"], "error")
        self.assertIn("syntax", tests[0]["error"].lower())


# ===================================================================
# 3. Crash detection — synthetic "Test Script Execution" result
# ===================================================================

class CrashDetectionTests(SimpleTestCase):
    """All templates emit a crash result with name="Test Script Execution" and status="error"."""

    def _assert_crash(self, results: List[Dict[str, Any]]):
        crash = [r for r in results if r.get("name") == "Test Script Execution" and r.get("status") == "error"]
        self.assertEqual(len(crash), 1, "Expected exactly one crash-synthetic result")
        self.assertFalse(crash[0]["passed"])
        self.assertEqual(crash[0]["score"], 0)

    def test_python_crash_result(self):
        crash = _make_crash_result("Test script failed to load:\nImportError: No module named 'nonexistent'")
        _, _, tests = Executor.parse_test_results("", _wrap_result(crash))
        self._assert_crash(tests)

    def test_js_crash_result(self):
        """JS wraps crash as a test via: test("Test Script Execution", 0, () => { throw ... })"""
        results = [_make_crash_result("Failed to run test script: Cannot find module 'foo'")]
        _, _, tests = Executor.parse_test_results(_wrap_result(results), "")
        self._assert_crash(tests)

    def test_java_crash_result(self):
        results = [_make_crash_result("Test script failed to load: java.lang.NoClassDefFoundError: Foo")]
        _, _, tests = Executor.parse_test_results(_wrap_result(results), "")
        self._assert_crash(tests)

    def test_cpp_crash_result(self):
        results = [_make_crash_result("Test script failed to load: segfault")]
        _, _, tests = Executor.parse_test_results(_wrap_result(results), "")
        self._assert_crash(tests)

    def test_r_crash_result(self):
        results = [_make_crash_result("Failed to run test script: Error in eval: object 'x' not found")]
        _, _, tests = Executor.parse_test_results(_wrap_result(results), "")
        self._assert_crash(tests)

    def test_crash_blocks_sync_logic(self):
        """When a crash result is present, should_sync must be False."""
        results = [_make_crash_result()]
        script_crashed = any(
            r.get("name") == "Test Script Execution" and r.get("status") == "error"
            for r in results
        )
        self.assertTrue(script_crashed)

    def test_no_crash_allows_sync(self):
        results = [_make_test_result(name="test_ok")]
        script_crashed = any(
            r.get("name") == "Test Script Execution" and r.get("status") == "error"
            for r in results
        )
        self.assertFalse(script_crashed)


# ===================================================================
# 4. _detect_syntax_hint
# ===================================================================

class DetectSyntaxHintTests(SimpleTestCase):

    def test_python_syntax_error_in_stderr(self):
        hint = TestService._detect_syntax_hint({
            "stdout": "",
            "stderr": "Student Code Syntax Error:\nSyntaxError: invalid syntax (student.py, line 3)",
            "error": None,
        })
        self.assertIsNotNone(hint)
        self.assertIn("syntax", hint.lower())

    def test_python_indentation_error(self):
        hint = TestService._detect_syntax_hint({
            "stdout": "",
            "stderr": "IndentationError: unexpected indent",
            "error": None,
        })
        self.assertIsNotNone(hint)

    def test_java_compilation_error(self):
        hint = TestService._detect_syntax_hint({
            "stdout": "",
            "stderr": "TestRunner.java:5: error: ';' expected\n    int x = 5\n              ^",
            "error": None,
        })
        self.assertIsNotNone(hint)
        self.assertIn("syntax", hint.lower())

    def test_cpp_compilation_error(self):
        hint = TestService._detect_syntax_hint({
            "stdout": "",
            "stderr": "student.cpp:10: error: expected ';' before '}' token\ncompilation failed",
            "error": None,
        })
        self.assertIsNotNone(hint)

    def test_js_syntax_error(self):
        hint = TestService._detect_syntax_hint({
            "stdout": "",
            "stderr": "SyntaxError: Unexpected token '}'",
            "error": None,
        })
        self.assertIsNotNone(hint)

    def test_no_hint_for_clean_output(self):
        hint = TestService._detect_syntax_hint({
            "stdout": "all tests passed",
            "stderr": "",
            "error": None,
        })
        self.assertIsNone(hint)

    def test_test_script_crash_detected(self):
        hint = TestService._detect_syntax_hint({
            "stdout": "",
            "stderr": "Test Script Error:\nImportError: No module named 'foobar'\nTraceback ...",
            "error": None,
        })
        self.assertIsNotNone(hint)
        self.assertIn("test script itself crashed", hint.lower())

    def test_student_runtime_error_with_syntax(self):
        hint = TestService._detect_syntax_hint({
            "stdout": "",
            "stderr": "Student Code Runtime Error:\nSyntaxError: invalid syntax",
            "error": None,
        })
        self.assertIsNotNone(hint)
        self.assertIn("student code", hint.lower())

    def test_r_unexpected_token(self):
        """R errors with 'unexpected token' phrasing are detected."""
        hint = TestService._detect_syntax_hint({
            "stdout": "",
            "stderr": "Error: unexpected token '}' in source",
            "error": None,
        })
        self.assertIsNotNone(hint)

    def test_r_unexpected_symbol_not_detected(self):
        """R's 'unexpected symbol' phrasing is NOT in the pattern list."""
        hint = TestService._detect_syntax_hint({
            "stdout": "",
            "stderr": "Error in parse(text = code): unexpected symbol in 'x y'",
            "error": None,
        })
        self.assertIsNone(hint)

    def test_empty_execution_result(self):
        hint = TestService._detect_syntax_hint({
            "stdout": "",
            "stderr": "",
            "error": None,
        })
        self.assertIsNone(hint)


# ===================================================================
# 5. _looks_like_syntax_or_compile_error
# ===================================================================

class LooksSyntaxOrCompileTests(SimpleTestCase):

    def test_python_patterns(self):
        self.assertTrue(TestService._looks_like_syntax_or_compile_error("SyntaxError: invalid syntax"))
        self.assertTrue(TestService._looks_like_syntax_or_compile_error("IndentationError: unexpected indent"))
        self.assertTrue(TestService._looks_like_syntax_or_compile_error("TabError: inconsistent use of tabs"))

    def test_java_patterns(self):
        self.assertTrue(TestService._looks_like_syntax_or_compile_error("error: ';' expected"))
        self.assertTrue(TestService._looks_like_syntax_or_compile_error("compilation failed"))

    def test_cpp_patterns(self):
        self.assertTrue(TestService._looks_like_syntax_or_compile_error("student.cpp:10: ';' expected"))
        self.assertTrue(TestService._looks_like_syntax_or_compile_error("failed to compile"))
        self.assertTrue(TestService._looks_like_syntax_or_compile_error("compilation failed"))

    def test_js_patterns(self):
        self.assertTrue(TestService._looks_like_syntax_or_compile_error("SyntaxError: Unexpected token '}'"))
        self.assertTrue(TestService._looks_like_syntax_or_compile_error("unexpected EOF while parsing"))

    def test_clean_text_returns_false(self):
        self.assertFalse(TestService._looks_like_syntax_or_compile_error("All tests passed"))
        self.assertFalse(TestService._looks_like_syntax_or_compile_error("AssertionError: expected 5 got 3"))

    def test_empty_string(self):
        self.assertFalse(TestService._looks_like_syntax_or_compile_error(""))

    def test_none_returns_false(self):
        self.assertFalse(TestService._looks_like_syntax_or_compile_error(None))


# ===================================================================
# 6. _looks_like_secondary_undefined_error
# ===================================================================

class LooksSecondaryUndefinedTests(SimpleTestCase):

    def test_python_name_error(self):
        self.assertTrue(TestService._looks_like_secondary_undefined_error("NameError: name 'foo' is not defined"))

    def test_js_reference_error(self):
        self.assertTrue(TestService._looks_like_secondary_undefined_error("ReferenceError: x is not defined"))

    def test_java_cannot_find_symbol(self):
        self.assertTrue(TestService._looks_like_secondary_undefined_error("cannot find symbol: variable foo"))

    def test_clean_text_returns_false(self):
        self.assertFalse(TestService._looks_like_secondary_undefined_error("AssertionError: 1 != 2"))

    def test_empty_string(self):
        self.assertFalse(TestService._looks_like_secondary_undefined_error(""))


# ===================================================================
# 7. _annotate_tests_with_syntax_hint
# ===================================================================

class AnnotateTestsWithSyntaxHintTests(SimpleTestCase):

    def test_hint_attached_to_name_error(self):
        tests = [_make_test_result(name="test_a", passed=False, status="failed", error="NameError: name 'foo' is not defined")]
        hint = "Student code has a syntax error."
        annotated = TestService._annotate_tests_with_syntax_hint(tests, hint)
        self.assertIn(hint, annotated[0]["error"])

    def test_hint_not_attached_to_passing_test(self):
        tests = [_make_test_result(name="test_ok", passed=True)]
        hint = "Student code has a syntax error."
        annotated = TestService._annotate_tests_with_syntax_hint(tests, hint)
        self.assertNotIn(hint, annotated[0].get("error", ""))

    def test_hint_not_attached_to_unrelated_failure(self):
        tests = [_make_test_result(name="test_logic", passed=False, status="failed", error="Wrong answer: expected 42 got 0")]
        hint = "Student code has a syntax error."
        annotated = TestService._annotate_tests_with_syntax_hint(tests, hint)
        self.assertNotIn(hint, annotated[0]["error"])

    def test_hint_attached_to_syntax_error(self):
        tests = [_make_test_result(name="test_x", passed=False, status="error", error="SyntaxError: invalid syntax")]
        hint = "Detected a likely syntax/parse/compile error."
        annotated = TestService._annotate_tests_with_syntax_hint(tests, hint)
        self.assertIn(hint, annotated[0]["error"])

    def test_no_hint_returns_unmodified(self):
        tests = [_make_test_result(name="test_a", passed=False, error="oops")]
        annotated = TestService._annotate_tests_with_syntax_hint(tests, None)
        self.assertEqual(annotated, tests)

    def test_status_promoted_to_error(self):
        tests = [_make_test_result(name="test_a", passed=False, status="failed", error="NameError: name 'x' is not defined")]
        hint = "syntax error"
        annotated = TestService._annotate_tests_with_syntax_hint(tests, hint)
        self.assertEqual(annotated[0]["status"], "error")


# ===================================================================
# 8. verify_script_test
# ===================================================================

class VerifyScriptTestTests(SimpleTestCase):

    def test_all_passing(self):
        exec_result = {
            "stdout": "", "stderr": "", "error": None,
            "tests": [
                _make_test_result(name="a", passed=True, score=3, max_score=3),
                _make_test_result(name="b", passed=True, score=2, max_score=2),
            ],
        }
        v = TestService.verify_script_test(cast(Any, None), exec_result)
        self.assertTrue(v["passed"])
        self.assertEqual(v["score"], 5)
        self.assertEqual(v["maxScore"], 5)
        self.assertFalse(v["isError"])

    def test_one_failure(self):
        exec_result = {
            "stdout": "", "stderr": "", "error": None,
            "tests": [
                _make_test_result(name="a", passed=True, score=3, max_score=3),
                _make_test_result(name="b", passed=False, score=0, max_score=2, status="failed", error="fail"),
            ],
        }
        v = TestService.verify_script_test(cast(Any, None), exec_result)
        self.assertFalse(v["passed"])
        self.assertEqual(v["score"], 3)
        self.assertEqual(v["maxScore"], 5)

    def test_partial_credit(self):
        exec_result = {
            "stdout": "", "stderr": "", "error": None,
            "tests": [
                _make_test_result(name="a", passed=False, score=7, max_score=10, status="partial"),
            ],
        }
        v = TestService.verify_script_test(cast(Any, None), exec_result)
        self.assertFalse(v["passed"])
        self.assertEqual(v["score"], 7)
        self.assertEqual(v["maxScore"], 10)

    def test_no_results_is_error(self):
        exec_result = {
            "stdout": "", "stderr": "something broke", "error": "container crashed",
            "tests": [],
        }
        v = TestService.verify_script_test(cast(Any, None), exec_result)
        self.assertFalse(v["passed"])
        self.assertTrue(v["isError"])
        self.assertEqual(v["score"], 0)

    def test_no_results_no_error(self):
        exec_result = {
            "stdout": "", "stderr": "", "error": None,
            "tests": [],
        }
        v = TestService.verify_script_test(cast(Any, None), exec_result)
        self.assertFalse(v["passed"])
        self.assertFalse(v["isError"])
        self.assertIn("no test results", v["logs"].lower())

    def test_execution_error_sets_is_error(self):
        exec_result = {
            "stdout": "", "stderr": "", "error": "Docker timeout",
            "tests": [_make_test_result(name="a", passed=True, score=1, max_score=1)],
        }
        v = TestService.verify_script_test(cast(Any, None), exec_result)
        self.assertTrue(v["isError"])

    def test_results_stored_as_json_in_logs(self):
        exec_result = {
            "stdout": "", "stderr": "", "error": None,
            "tests": [_make_test_result(name="test_json")],
        }
        v = TestService.verify_script_test(cast(Any, None), exec_result)
        parsed = json.loads(v["logs"])
        self.assertIsInstance(parsed, list)
        self.assertEqual(parsed[0]["name"], "test_json")


# ===================================================================
# 9. verify_unit_test
# ===================================================================

class VerifyUnitTestTests(SimpleTestCase):

    def test_passing(self):
        exec_result = {"success": True, "stdout": "OK", "stderr": "", "error": None}
        v = TestService.verify_unit_test(cast(Any, None), exec_result)
        self.assertTrue(v["passed"])
        self.assertFalse(v["isError"])

    def test_failing(self):
        exec_result = {"success": False, "stdout": "", "stderr": "FAIL: test_foo", "error": None}
        v = TestService.verify_unit_test(cast(Any, None), exec_result)
        self.assertFalse(v["passed"])

    def test_error_flag(self):
        exec_result = {"success": False, "stdout": "", "stderr": "", "error": "compilation failed"}
        v = TestService.verify_unit_test(cast(Any, None), exec_result)
        self.assertFalse(v["passed"])
        self.assertTrue(v["isError"])

    def test_syntax_hint_appended(self):
        exec_result = {"success": False, "stdout": "", "stderr": "SyntaxError: invalid syntax", "error": None}
        v = TestService.verify_unit_test(cast(Any, None), exec_result)
        self.assertIn("syntax", v["logs"].lower())


# ===================================================================
# 10. _collect_notebook_cell_error_text
# ===================================================================

class CollectNotebookCellErrorTextTests(SimpleTestCase):

    def test_error_output_collected(self):
        exec_result = {
            "output_data": {
                "cells": [{
                    "cell_type": "code",
                    "idx": 0,
                    "source": "x = 1 +",
                    "outputs": [{
                        "output_type": "error",
                        "ename": "SyntaxError",
                        "evalue": "invalid syntax",
                        "traceback": ["SyntaxError: invalid syntax"],
                    }],
                }],
            },
        }
        text = TestService._collect_notebook_cell_error_text(exec_result)
        self.assertIn("SyntaxError", text)
        self.assertIn("Notebook cell 1", text)
        self.assertIn("x = 1 +", text)

    def test_stderr_stream_collected(self):
        exec_result = {
            "output_data": {
                "cells": [{
                    "cell_type": "code",
                    "idx": 2,
                    "source": "import bad",
                    "outputs": [{
                        "output_type": "stream",
                        "name": "stderr",
                        "text": "ModuleNotFoundError: No module named 'bad'",
                    }],
                }],
            },
        }
        text = TestService._collect_notebook_cell_error_text(exec_result)
        self.assertIn("ModuleNotFoundError", text)
        self.assertIn("Notebook cell 3", text)

    def test_markdown_cells_ignored(self):
        exec_result = {
            "output_data": {
                "cells": [{
                    "cell_type": "markdown",
                    "source": "# Title",
                    "outputs": [],
                }],
            },
        }
        text = TestService._collect_notebook_cell_error_text(exec_result)
        self.assertEqual(text, "")

    def test_no_cells(self):
        text = TestService._collect_notebook_cell_error_text({"output_data": {}})
        self.assertEqual(text, "")

    def test_no_output_data(self):
        text = TestService._collect_notebook_cell_error_text({})
        self.assertEqual(text, "")


# ===================================================================
# 11. _sanitize_overrides
# ===================================================================

class SanitizeOverridesTests(SimpleTestCase):

    def test_string_keys_converted(self):
        result = TestService._sanitize_overrides({"1": "code", "2": "more"})
        self.assertEqual(result, {1: "code", 2: "more"})

    def test_int_keys_preserved(self):
        result = TestService._sanitize_overrides({1: "code"})
        self.assertEqual(result, {1: "code"})

    def test_none_returns_empty(self):
        result = TestService._sanitize_overrides(None)
        self.assertEqual(result, {})

    def test_empty_returns_empty(self):
        result = TestService._sanitize_overrides({})
        self.assertEqual(result, {})


# ===================================================================
# 12. _to_json_safe
# ===================================================================

class ToJsonSafeTests(SimpleTestCase):

    def test_decimal_converted(self):
        from decimal import Decimal
        self.assertEqual(TestService._to_json_safe(Decimal("3.14")), 3.14)

    def test_nested_dict(self):
        from decimal import Decimal
        data = {"score": Decimal("10"), "details": {"sub": Decimal("5.5")}}
        result = TestService._to_json_safe(data)
        self.assertEqual(result, {"score": 10.0, "details": {"sub": 5.5}})

    def test_list_with_decimals(self):
        from decimal import Decimal
        result = TestService._to_json_safe([Decimal("1"), Decimal("2")])
        self.assertEqual(result, [1.0, 2.0])

    def test_tuple_converted_to_list(self):
        result = TestService._to_json_safe((1, 2, 3))
        self.assertEqual(result, [1, 2, 3])

    def test_plain_values_unchanged(self):
        self.assertEqual(TestService._to_json_safe("hello"), "hello")
        self.assertEqual(TestService._to_json_safe(42), 42)
        self.assertIsNone(TestService._to_json_safe(None))


# ===================================================================
# 13. End-to-end template output scenarios
# ===================================================================

class EndToEndTemplateScenarioTests(SimpleTestCase):
    """Simulate realistic template output and verify the full pipeline."""

    def test_python_3_tests_mixed(self):
        """Python template: 3 tests, 1 passed, 1 failed, 1 error."""
        stderr_parts = []
        stderr_parts.append("<<<RESULT>>>")
        stderr_parts.append("SCRIPT_DEBUG: @test(...)")
        stderr_parts.append(_wrap_result({
            "name": "test_add", "max_score": 2.0, "description": "addition",
            "score": 2.0, "passed": True, "error": None, "message": None, "output": "", "status": "passed",
        }))
        stderr_parts.append(_wrap_result({
            "name": "test_sub", "max_score": 3.0, "description": "subtraction",
            "score": 0, "passed": False, "error": "AssertionError: -1 != 1", "message": None, "output": "", "status": "failed",
        }))
        stderr_parts.append(_wrap_result({
            "name": "test_timeout", "max_score": 1.0, "description": None,
            "score": 0, "passed": False, "error": "Test timed out after 5 seconds", "message": "Test timed out", "output": "", "status": "error",
        }))
        stderr = "\n".join(stderr_parts)

        _, clean_stderr, tests = Executor.parse_test_results("", stderr)
        self.assertEqual(len(tests), 3)

        exec_result = {
            "stdout": "", "stderr": stderr, "error": None, "tests": tests,
        }
        v = TestService.verify_script_test(cast(Any, None), exec_result)
        self.assertFalse(v["passed"])
        self.assertEqual(v["score"], 2.0)
        self.assertEqual(v["maxScore"], 6.0)

    def test_js_all_passing(self):
        """JS template: array of 2 passing tests in stdout."""
        results = [
            {"name": "test_a", "max_score": 5, "description": None, "message": "", "score": 5, "passed": True, "status": "passed", "error": ""},
            {"name": "test_b", "max_score": 5, "description": None, "message": "", "score": 5, "passed": True, "status": "passed", "error": ""},
        ]
        stdout = f"console log\n{_wrap_result(results)}\ndone"
        _, _, tests = Executor.parse_test_results(stdout, "")
        self.assertEqual(len(tests), 2)

        exec_result = {"stdout": stdout, "stderr": "", "error": None, "tests": tests}
        v = TestService.verify_script_test(cast(Any, None), exec_result)
        self.assertTrue(v["passed"])
        self.assertEqual(v["score"], 10)

    def test_cpp_crash_with_real_tests(self):
        """C++ template: test loop itself crashes—only crash result returned."""
        results = [_make_crash_result("Test script failed to load: std::bad_alloc")]
        stdout = _wrap_result(results)
        _, _, tests = Executor.parse_test_results(stdout, "")
        self.assertEqual(len(tests), 1)

        # Verify crash detection
        script_crashed = any(
            r.get("name") == "Test Script Execution" and r.get("status") == "error"
            for r in tests
        )
        self.assertTrue(script_crashed)

    def test_java_mixed_with_crash(self):
        """Java: some tests ran before crash — crash result appended."""
        results = [
            _make_test_result(name="testBasic", passed=True, score=1, max_score=1),
            _make_crash_result("Test script failed to load: NullPointerException"),
        ]
        stdout = _wrap_result(results)
        _, _, tests = Executor.parse_test_results(stdout, "")
        self.assertEqual(len(tests), 2)

        _exec_result = {"stdout": stdout, "stderr": "", "error": None, "tests": tests}
        # crash is present => should_sync = False
        script_crashed = any(
            r.get("name") == "Test Script Execution" and r.get("status") == "error"
            for r in tests
        )
        self.assertTrue(script_crashed)

    def test_syntax_hint_full_pipeline(self):
        """Python: student has syntax error, all tests fail with NameError."""
        tests = [
            _make_test_result(name="test_a", passed=False, status="failed", error="NameError: name 'add' is not defined"),
            _make_test_result(name="test_b", passed=False, status="failed", error="NameError: name 'sub' is not defined"),
        ]
        exec_result = {
            "stdout": "",
            "stderr": "Student Code Syntax Error:\nSyntaxError: invalid syntax (student.py, line 5)",
            "error": None,
            "tests": tests,
        }
        hint = TestService._detect_syntax_hint(exec_result)
        self.assertIsNotNone(hint)

        annotated = TestService._annotate_tests_with_syntax_hint(tests, hint)
        for t in annotated:
            self.assertIn(hint, t["error"])
            self.assertEqual(t["status"], "error")

        v = TestService.verify_script_test(cast(Any, None), {**exec_result, "tests": annotated})
        self.assertFalse(v["passed"])
        self.assertIn(hint, v["logs"])

    def test_stderr_crash_marker_fallback(self):
        """When no synthetic result parsed but stderr has crash marker, detect crash."""
        stderr = "Test Script Error:\nImportError: No module named 'missing'\nTraceback ..."
        _, _, tests = Executor.parse_test_results("", stderr)
        self.assertEqual(len(tests), 0)

        # Backend fallback logic from run_suite
        script_crashed = not tests and "Test Script Error:" in stderr
        self.assertTrue(script_crashed)
