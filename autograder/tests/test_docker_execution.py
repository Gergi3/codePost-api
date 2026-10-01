# Copyright © 2026 Rutgers, the State University of New Jersey. All rights reserved except as defined by the Rutgers Non-Commercial License, included with this software.
"""
Docker-based end-to-end executor tests.

These tests exercise the REAL executor → Docker → template → parse pipeline.
Each test creates a MockFile, instantiates the actual executor, calls execute(),
and verifies the returned ExecutionResult plus parsed test results.

Requirements:
- Docker daemon running
- Base language images already pulled (python:3.12-slim, node:20-slim, etc.)

Run only Docker tests:
    pytest autograder/tests/test_docker_execution.py -v

Skip Docker tests (runs in CI without Docker):
    pytest -m "not docker"
"""

from typing import Any, Dict, List, cast

import pytest
from django.test import SimpleTestCase

from autograder.services.executors.base import ExecutionResult
from autograder.services.executors.mock_file import MockFile
from autograder.services.TestService import TestService

# Conditional imports for executor classes
from autograder.services.executors.python import PythonExecutor, PythonNotebookExecutor
from autograder.services.executors.node import NodeExecutor, NodeNotebookExecutor
from autograder.services.executors.cpp import CPPExecutor
from autograder.services.executors.java import JavaExecutor, JavaNotebookExecutor
from autograder.services.executors.r import RExecutor, RNotebookExecutor
from autograder.services.executors.ruby import RubyNotebookExecutor
from autograder.services.executors.php import PHPNotebookExecutor


# ---------------------------------------------------------------------------
# Docker availability check
# ---------------------------------------------------------------------------

def _docker_available() -> bool:
    """Check if Docker daemon is running and responsive."""
    try:
        import docker as docker_lib
        client = docker_lib.from_env()
        client.ping()
        return True
    except Exception:
        return False


DOCKER_OK = _docker_available()
skip_no_docker = pytest.mark.skipif(not DOCKER_OK, reason="Docker not available")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_mock_file(data: str, name: str = "student.py", extension: str = ".py") -> MockFile:
    """Create a MockFile with a get_course method."""
    mf = MockFile(data=data, name=name, extension=extension)
    mf.get_course = lambda: None  # type: ignore[attr-defined]
    return mf


def _make_notebook_mock_file(cells: List[Dict[str, Any]], name: str = "notebook.ipynb") -> MockFile:
    """Create a MockFile containing a Jupyter notebook with the given cells."""
    import nbformat
    nb = nbformat.v4.new_notebook()
    for cell in cells:
        cell_type = cell.get("type", "code")
        source = cell.get("source", "")
        if cell_type == "code":
            nb.cells.append(nbformat.v4.new_code_cell(source))
        elif cell_type == "markdown":
            nb.cells.append(nbformat.v4.new_markdown_cell(source))
    notebook_json = nbformat.writes(nb)
    mf = MockFile(data=notebook_json, name=name, extension=".ipynb")
    mf.get_course = lambda: None  # type: ignore[attr-defined]
    return mf


# ###################################################################
# Python Executor — Docker Tests
# ###################################################################


@skip_no_docker
class PythonDockerExecutionTests(SimpleTestCase):
    """End-to-end Python executor tests using real Docker containers."""

    def _execute(self, student_code: str, test_code: str) -> ExecutionResult:
        mock_file = _make_mock_file(student_code, name="student.py", extension=".py")
        executor = PythonExecutor(mock_file, test_code=test_code)
        return executor.execute()

    def test_runner_is_unlinked_before_student_code(self):
        """The staged template must be gone from /work by the time student code runs."""
        result = self._execute(
            student_code="import os\nRUNNER_GONE = not os.path.exists('/work/.codepost_runner.py')",
            test_code="""
@test(name="runner gone", points=1)
def test_runner_gone():
    assert RUNNER_GONE
""",
        )
        self.assertTrue(len(result.tests) >= 1, f"No tests found. stderr: {result.stderr}")
        self.assertTrue(result.tests[0]["passed"], result.tests[0])

    def test_passing_test(self):
        """Basic passing test through full Docker pipeline."""
        result = self._execute(
            student_code="def add(a, b): return a + b",
            test_code="""
@test(name="add works", points=5)
def test_add():
    assert add(2, 3) == 5
""",
        )
        self.assertTrue(len(result.tests) >= 1, f"No tests found. stderr: {result.stderr}")
        self.assertTrue(result.tests[0]["passed"])
        self.assertEqual(result.tests[0]["score"], 5)
        self.assertEqual(result.tests[0]["name"], "add works")

    def test_failing_assertion(self):
        """Failed assertion → status='failed'."""
        result = self._execute(
            student_code="val = 42",
            test_code="""
@test(name="wrong", points=3)
def wrong():
    assert val == 99, "not 99"
""",
        )
        self.assertTrue(len(result.tests) >= 1, f"No tests. tests: {result.tests}")
        t = next(t for t in result.tests if t["name"] == "wrong")
        self.assertFalse(t["passed"])
        self.assertEqual(t["status"], "failed")

    def test_runtime_exception_gives_error(self):
        """An unexpected exception → status='error'."""
        result = self._execute(
            student_code="pass",
            test_code="""
@test(name="boom", points=2)
def boom():
    raise RuntimeError("kaboom")
""",
        )
        self.assertTrue(len(result.tests) >= 1, f"No tests. tests: {result.tests}")
        t = next(t for t in result.tests if t["name"] == "boom")
        self.assertEqual(t["status"], "error")
        self.assertIn("kaboom", t["error"])

    def test_multiple_tests_mixed(self):
        """Multiple tests: one pass, one fail."""
        result = self._execute(
            student_code="x = 7",
            test_code="""
@test(name="pass", points=1)
def t1():
    assert x == 7

@test(name="fail", points=2)
def t2():
    assert x == 0, "x is not 0"
""",
        )
        self.assertTrue(len(result.tests) >= 2, f"Expected >= 2 tests. tests: {result.tests}")
        names = {t["name"]: t for t in result.tests}
        self.assertIn("pass", names)
        self.assertIn("fail", names)
        self.assertTrue(names["pass"]["passed"])
        self.assertFalse(names["fail"]["passed"])

    def test_partial_credit(self):
        """Returning a number gives partial credit."""
        result = self._execute(
            student_code="pass",
            test_code="""
@test(name="partial", points=10)
def partial():
    return 7
""",
        )
        self.assertEqual(result.tests[0]["score"], 7)
        self.assertEqual(result.tests[0]["status"], "partial")

    def test_stdout_captured(self):
        """print() output is captured in the output field."""
        result = self._execute(
            student_code="pass",
            test_code="""
@test(name="output", points=1)
def output_test():
    print("hello from docker")
""",
        )
        self.assertIn("hello from docker", result.tests[0]["output"])

    def test_crash_produces_synthetic_error(self):
        """Test script crash → synthetic error result."""
        result = self._execute(
            student_code="x = 1",
            test_code='raise RuntimeError("test script exploded")',
        )
        self.assertTrue(len(result.tests) >= 1)
        crash = [t for t in result.tests if t.get("name") == "Test Script Execution"]
        self.assertTrue(len(crash) >= 1, f"No crash result. tests: {result.tests}")
        self.assertEqual(crash[0]["status"], "error")

    def test_verify_pipeline(self):
        """Full pipeline: executor → Docker → parse → verify_script_test."""
        result = self._execute(
            student_code="def mul(a, b): return a * b",
            test_code="""
@test(name="mul", points=10)
def t():
    assert mul(3, 4) == 12
""",
        )
        exec_result = {
            "stdout": result.stdout,
            "stderr": result.stderr,
            "error": result.err,
            "tests": result.tests,
        }
        v = TestService.verify_script_test(cast(Any, None), exec_result)
        self.assertTrue(v["passed"])
        self.assertEqual(v["score"], 10)


# ###################################################################
# Node.js Executor — Docker Tests
# ###################################################################


@skip_no_docker
class NodeDockerExecutionTests(SimpleTestCase):
    """End-to-end Node.js executor tests using real Docker containers."""

    def _execute(self, student_code: str, test_code: str) -> ExecutionResult:
        mock_file = _make_mock_file(student_code, name="student.js", extension=".js")
        executor = NodeExecutor(mock_file, test_code=test_code)
        return executor.execute()

    def test_passing_test(self):
        result = self._execute(
            student_code="function add(a, b) { return a + b; }",
            test_code="""
test("add works", 5, function() {
    if (add(2, 3) !== 5) throw new Error("Expected 5");
});
""",
        )
        self.assertTrue(len(result.tests) >= 1, f"No tests. stderr: {result.stderr}")
        self.assertTrue(result.tests[0]["passed"])
        self.assertEqual(result.tests[0]["score"], 5)

    def test_failing_test(self):
        result = self._execute(
            student_code="function greet() { return 'hi'; }",
            test_code="""
test("wrong", 3, function() {
    if (greet() !== "hello") throw new Error("Expected hello");
});
""",
        )
        self.assertTrue(len(result.tests) >= 1, f"No tests. tests: {result.tests}")
        t = next(t for t in result.tests if t["name"] == "wrong")
        self.assertFalse(t["passed"])

    def test_multiple_tests(self):
        result = self._execute(
            student_code="var x = 10;",
            test_code="""
test("pass", 1, function() {
    if (x !== 10) throw new Error("wrong");
});
test("fail", 2, function() {
    if (x !== 99) throw new Error("wrong");
});
""",
        )
        self.assertTrue(len(result.tests) >= 2, f"Expected >= 2 tests. tests: {result.tests}")
        names = {r["name"]: r for r in result.tests}
        self.assertIn("pass", names)
        self.assertIn("fail", names)
        self.assertTrue(names["pass"]["passed"])
        self.assertFalse(names["fail"]["passed"])

    def test_verify_pipeline(self):
        result = self._execute(
            student_code="function mul(a, b) { return a * b; }",
            test_code="""
test("mul works", 10, function() {
    if (mul(3, 4) !== 12) throw new Error("Expected 12");
});
""",
        )
        exec_result = {
            "stdout": result.stdout,
            "stderr": result.stderr,
            "error": result.err,
            "tests": result.tests,
        }
        v = TestService.verify_script_test(cast(Any, None), exec_result)
        self.assertTrue(v["passed"])
        self.assertEqual(v["score"], 10)


# ###################################################################
# C++ Executor — Docker Tests
# ###################################################################


@skip_no_docker
class CppDockerExecutionTests(SimpleTestCase):
    """End-to-end C++ executor tests using real Docker containers."""

    def _execute(self, student_code: str, test_code: str) -> ExecutionResult:
        mock_file = _make_mock_file(student_code, name="source.cpp", extension=".cpp")
        executor = CPPExecutor(mock_file, test_code=test_code)
        return executor.execute()

    def test_passing_test(self):
        result = self._execute(
            student_code="int add(int a, int b) { return a + b; }",
            test_code="""
int add(int a, int b);

TEST(add_works, 5) {
    assertTrue(add(2, 3) == 5, "Expected 5");
}
""",
        )
        self.assertTrue(len(result.tests) >= 1, f"No tests. stderr: {result.stderr}")
        self.assertTrue(result.tests[0]["passed"])
        self.assertEqual(result.tests[0]["score"], 5)

    def test_failing_assertion(self):
        result = self._execute(
            student_code="int val() { return 42; }",
            test_code="""
int val();

TEST(wrong, 3) {
    assertTrue(val() == 99, "not 99");
}
""",
        )
        self.assertTrue(len(result.tests) >= 1, f"No tests. tests: {result.tests}")
        t = next(t for t in result.tests if t["name"] == "wrong")
        self.assertFalse(t["passed"])
        self.assertEqual(t["status"], "failed")

    def test_runtime_exception_gives_error(self):
        result = self._execute(
            student_code='#include <stdexcept>\nvoid boom() { throw std::runtime_error("kaboom"); }',
            test_code="""
void boom();

TEST(boom_test, 2) {
    boom();
}
""",
        )
        self.assertTrue(len(result.tests) >= 1, f"No tests. tests: {result.tests}")
        t = next(t for t in result.tests if t["name"] == "boom_test")
        self.assertEqual(t["status"], "error")

    def test_verify_pipeline(self):
        result = self._execute(
            student_code="int mul(int a, int b) { return a * b; }",
            test_code="""
int mul(int a, int b);

TEST(mul_works, 10) {
    assertTrue(mul(3, 4) == 12, "Expected 12");
}
""",
        )
        exec_result = {
            "stdout": result.stdout,
            "stderr": result.stderr,
            "error": result.err,
            "tests": result.tests,
        }
        v = TestService.verify_script_test(cast(Any, None), exec_result)
        self.assertTrue(v["passed"])
        self.assertEqual(v["score"], 10)


# ###################################################################
# Java Executor — Docker Tests
# ###################################################################


@skip_no_docker
class JavaDockerExecutionTests(SimpleTestCase):
    """End-to-end Java executor tests using real Docker containers."""

    def _execute(self, student_code: str, test_code: str) -> ExecutionResult:
        mock_file = _make_mock_file(student_code, name="Student.java", extension=".java")
        executor = JavaExecutor(mock_file, test_code=test_code)
        return executor.execute()

    def test_passing_test(self):
        result = self._execute(
            student_code="public class Student { public static int add(int a, int b) { return a + b; } }",
            test_code="""
    @Test(name = "add works", points = 5)
    public void testAdd() {
        assertEquals(5, Student.add(2, 3));
    }
""",
        )
        self.assertTrue(len(result.tests) >= 1, f"No tests. stdout: {result.stdout}\nstderr: {result.stderr}")
        self.assertTrue(result.tests[0]["passed"])
        self.assertEqual(result.tests[0]["score"], 5)

    def test_failing_assertion(self):
        result = self._execute(
            student_code="public class Student { public static int val() { return 42; } }",
            test_code="""
    @Test(name = "wrong", points = 3)
    public void testWrong() {
        assertEquals(99, Student.val());
    }
""",
        )
        self.assertTrue(len(result.tests) >= 1, f"No tests. tests: {result.tests}")
        t = next(t for t in result.tests if t["name"] == "wrong")
        self.assertFalse(t["passed"])

    def test_runtime_exception_gives_error(self):
        result = self._execute(
            student_code='public class Student { public static void boom() { throw new RuntimeException("kaboom"); } }',
            test_code="""
    @Test(name = "boom_test", points = 2)
    public void testBoom() {
        Student.boom();
    }
""",
        )
        self.assertTrue(len(result.tests) >= 1, f"No tests. tests: {result.tests}")
        t = next(t for t in result.tests if t["name"] == "boom_test")
        self.assertEqual(t["status"], "error")

    def test_verify_pipeline(self):
        result = self._execute(
            student_code="public class Student { public static int mul(int a, int b) { return a * b; } }",
            test_code="""
    @Test(name = "mul works", points = 10)
    public void testMul() {
        assertEquals(12, Student.mul(3, 4));
    }
""",
        )
        exec_result = {
            "stdout": result.stdout,
            "stderr": result.stderr,
            "error": result.err,
            "tests": result.tests,
        }
        v = TestService.verify_script_test(cast(Any, None), exec_result)
        self.assertTrue(v["passed"])
        self.assertEqual(v["score"], 10)


# ###################################################################
# R Executor — Docker Tests
# ###################################################################


@skip_no_docker
class RDockerExecutionTests(SimpleTestCase):
    """End-to-end R executor tests using real Docker containers."""

    def _execute(self, student_code: str, test_code: str) -> ExecutionResult:
        mock_file = _make_mock_file(student_code, name="student.R", extension=".R")
        executor = RExecutor(mock_file, test_code=test_code)
        return executor.execute()

    def test_passing_test(self):
        result = self._execute(
            student_code="add <- function(a, b) a + b",
            test_code="""
run_test("add works", 5, function() {
    stopifnot(add(2, 3) == 5)
})
""",
        )
        self.assertTrue(len(result.tests) >= 1, f"No tests. stdout: {result.stdout}\nstderr: {result.stderr}")
        self.assertTrue(result.tests[0]["passed"])
        self.assertEqual(result.tests[0]["score"], 5)

    def test_failing_test(self):
        result = self._execute(
            student_code="val <- 42",
            test_code="""
run_test("wrong", 3, function() {
    assertion_error("not 99")
})
""",
        )
        self.assertTrue(len(result.tests) >= 1, f"No tests. tests: {result.tests}")
        t = next(t for t in result.tests if t["name"] == "wrong")
        self.assertFalse(t["passed"])

    def test_multiple_tests(self):
        result = self._execute(
            student_code="sq <- function(x) x * x",
            test_code="""
run_test("sq_4", 5, function() {
    stopifnot(sq(4) == 16)
})

run_test("sq_wrong", 2, function() {
    assertion_error("3^2 is not 10")
})
""",
        )
        self.assertTrue(len(result.tests) >= 2, f"Expected >= 2 tests. tests: {result.tests}")
        names = {r["name"]: r for r in result.tests}
        self.assertIn("sq_4", names)
        self.assertIn("sq_wrong", names)
        self.assertTrue(names["sq_4"]["passed"])
        self.assertFalse(names["sq_wrong"]["passed"])

    def test_verify_pipeline(self):
        result = self._execute(
            student_code="mul <- function(a, b) a * b",
            test_code="""
run_test("mul works", 10, function() {
    stopifnot(mul(3, 4) == 12)
})
""",
        )
        exec_result = {
            "stdout": result.stdout,
            "stderr": result.stderr,
            "error": result.err,
            "tests": result.tests,
        }
        v = TestService.verify_script_test(cast(Any, None), exec_result)
        self.assertTrue(v["passed"])
        self.assertEqual(v["score"], 10)


# ###################################################################
# Python Notebook Executor — Docker Tests
# ###################################################################


@skip_no_docker
class PythonNotebookDockerExecutionTests(SimpleTestCase):
    """End-to-end Python notebook executor tests using real Docker containers."""

    def _execute(self, cells: List[str], test_code: str) -> ExecutionResult:
        cell_dicts = [{"type": "code", "source": src} for src in cells]
        mock_file = _make_notebook_mock_file(cell_dicts, name="notebook.ipynb")
        executor = PythonNotebookExecutor(mock_file, test_code=test_code)
        return executor.execute()

    def test_passing_test(self):
        result = self._execute(
            cells=["result = 42"],
            test_code="""
@test(name="check val", points=5)
def check():
    assert result == 42
""",
        )
        # Notebook executors store test results via parse_test_results
        self.assertTrue(len(result.tests) >= 1, f"No tests. stdout: {result.stdout}\nstderr: {result.stderr}")
        self.assertTrue(result.tests[0]["passed"])

    def test_multi_cell_persistence(self):
        result = self._execute(
            cells=["a = 10", "b = a + 5"],
            test_code="""
@test(name="sum ok", points=1)
def check():
    assert b == 15
""",
        )
        self.assertTrue(len(result.tests) >= 1)
        self.assertTrue(result.tests[0]["passed"])

    def test_failing_assertion(self):
        result = self._execute(
            cells=["val = 42"],
            test_code="""
@test(name="wrong", points=3)
def wrong():
    assert val == 99, "not 99"
""",
        )
        self.assertTrue(len(result.tests) >= 1, f"No tests. tests: {result.tests}")
        t = next(t for t in result.tests if t["name"] == "wrong")
        self.assertFalse(t["passed"])
        self.assertEqual(t["status"], "failed")

    def test_large_notebook_over_arg_max(self):
        """Regression: a notebook larger than the 128 KiB argv limit used to fail
        at container start with 'argument list too long'."""
        result = self._execute(
            cells=['big = "' + ("a" * 200_000) + '"', "n = len(big)"],
            test_code="""
@test(name="big cell ran", points=1)
def check():
    assert n == 200_000
""",
        )
        self.assertTrue(len(result.tests) >= 1, f"No tests. err: {result.err}\nstderr: {result.stderr[-500:]}")
        self.assertTrue(result.tests[0]["passed"], result.tests[0])


# ###################################################################
# Node.js Notebook Executor — Docker Tests
# ###################################################################


@skip_no_docker
class NodeNotebookDockerExecutionTests(SimpleTestCase):
    """End-to-end Node.js notebook executor tests using real Docker containers."""

    def _execute(self, cells: List[str], test_code: str) -> ExecutionResult:
        cell_dicts = [{"type": "code", "source": src} for src in cells]
        mock_file = _make_notebook_mock_file(cell_dicts, name="notebook.ipynb")
        executor = NodeNotebookExecutor(mock_file, test_code=test_code)
        return executor.execute()

    def test_passing_test(self):
        result = self._execute(
            cells=["var result = 42;"],
            test_code="""
test("check val", 5, function() {
    if (result !== 42) throw new Error("Expected 42");
});
""",
        )
        self.assertTrue(len(result.tests) >= 1, f"No tests. stdout: {result.stdout}\nstderr: {result.stderr}")
        self.assertTrue(result.tests[0]["passed"])

    def test_multi_cell_persistence(self):
        result = self._execute(
            cells=["var a = 10;", "var b = a + 5;"],
            test_code="""
test("sum ok", 1, function() {
    if (b !== 15) throw new Error("Expected 15");
});
""",
        )
        self.assertTrue(len(result.tests) >= 1)
        self.assertTrue(result.tests[0]["passed"])


# ###################################################################
# Java Notebook Executor — Docker Tests
# ###################################################################


@skip_no_docker
class JavaNotebookDockerExecutionTests(SimpleTestCase):
    """End-to-end Java notebook executor tests using real Docker containers."""

    def _execute(self, cells: List[str], test_code: str) -> ExecutionResult:
        cell_dicts = [{"type": "code", "source": src} for src in cells]
        mock_file = _make_notebook_mock_file(cell_dicts, name="notebook.ipynb")
        executor = JavaNotebookExecutor(mock_file, test_code=test_code)
        return executor.execute()

    def test_passing_test(self):
        result = self._execute(
            cells=["int result = 42;"],
            test_code="""
class Tests {
    @Test(name = "check val", points = 5)
    public void testCheck() {
        assertTrue(result == 42, "Expected 42");
    }
}
""",
        )
        self.assertTrue(len(result.tests) >= 1, f"No tests. stdout: {result.stdout}\nstderr: {result.stderr}")
        self.assertTrue(result.tests[0]["passed"])

    def test_runner_source_removed_after_compile(self):
        """NotebookRunner.java is rm'd before the class runs, so cells cannot read it."""
        result = self._execute(
            cells=["boolean runnerGone = !new java.io.File(\"/work/NotebookRunner.java\").exists();"],
            test_code="""
class Tests {
    @Test(name = "runner gone", points = 1)
    public void testRunnerGone() {
        assertTrue(runnerGone, "NotebookRunner.java still on disk");
    }
}
""",
        )
        self.assertTrue(len(result.tests) >= 1, f"No tests. stdout: {result.stdout}\nstderr: {result.stderr}")
        self.assertTrue(result.tests[0]["passed"], result.tests[0])


# ###################################################################
# R Notebook Executor — Docker Tests
# ###################################################################


@skip_no_docker
class RNotebookDockerExecutionTests(SimpleTestCase):
    """End-to-end R notebook executor tests using real Docker containers."""

    def _execute(self, cells: List[str], test_code: str) -> ExecutionResult:
        cell_dicts = [{"type": "code", "source": src} for src in cells]
        mock_file = _make_notebook_mock_file(cell_dicts, name="notebook.ipynb")
        executor = RNotebookExecutor(mock_file, test_code=test_code)
        return executor.execute()

    def test_passing_test(self):
        result = self._execute(
            cells=["result <- 42"],
            test_code="""
run_test("check val", 5, function() {
    stopifnot(result == 42)
})
""",
        )
        self.assertTrue(len(result.tests) >= 1, f"No tests. stdout: {result.stdout}\nstderr: {result.stderr}")
        self.assertTrue(result.tests[0]["passed"])

    def test_large_notebook_over_arg_max(self):
        """Regression: a notebook larger than the 128 KiB argv limit used to fail
        at container start with 'argument list too long'."""
        result = self._execute(
            cells=['big <- "' + ("a" * 200_000) + '"', "n <- nchar(big)"],
            test_code="""
run_test("big cell ran", 1, function() {
    stopifnot(n == 200000)
})
""",
        )
        self.assertTrue(len(result.tests) >= 1, f"No tests. err: {result.err}\nstderr: {result.stderr[-500:]}")
        self.assertTrue(result.tests[0]["passed"], result.tests[0])


# ###################################################################
# Ruby Notebook Executor — Docker Tests
# ###################################################################


@skip_no_docker
class RubyNotebookDockerExecutionTests(SimpleTestCase):
    """End-to-end Ruby notebook executor tests using real Docker containers."""

    def _execute(self, cells: List[str], test_code: str) -> ExecutionResult:
        cell_dicts = [{"type": "code", "source": src} for src in cells]
        mock_file = _make_notebook_mock_file(cell_dicts, name="notebook.ipynb")
        executor = RubyNotebookExecutor(mock_file, test_code=test_code)
        return executor.execute()

    def test_passing_test(self):
        result = self._execute(
            cells=["$result = 42"],
            test_code="""
run_test("check val", 5) do
    raise AssertionError, "not 42" unless $result == 42
end
""",
        )
        self.assertTrue(len(result.tests) >= 1, f"No tests. stdout: {result.stdout}\nstderr: {result.stderr}")
        self.assertTrue(result.tests[0]["passed"])


# ###################################################################
# PHP Notebook Executor — Docker Tests
# ###################################################################


@skip_no_docker
class PHPNotebookDockerExecutionTests(SimpleTestCase):
    """End-to-end PHP notebook executor tests using real Docker containers."""

    def _execute(self, cells: List[str], test_code: str) -> ExecutionResult:
        cell_dicts = [{"type": "code", "source": src} for src in cells]
        mock_file = _make_notebook_mock_file(cell_dicts, name="notebook.ipynb")
        executor = PHPNotebookExecutor(mock_file, test_code=test_code)
        return executor.execute()

    def test_passing_test(self):
        result = self._execute(
            cells=["$result = 42;"],
            test_code="""
Tester::test("check val", 5, function() {
    global $result;
    if ($result !== 42) throw new AssertionError("not 42");
});
""",
        )
        self.assertTrue(len(result.tests) >= 1, f"No tests. stdout: {result.stdout}\nstderr: {result.stderr}")
        self.assertTrue(result.tests[0]["passed"])
