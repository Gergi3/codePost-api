# Copyright © 2026 Rutgers, the State University of New Jersey. All rights reserved except as defined by the Rutgers Non-Commercial License, included with this software.
"""
Executors must never pass the rendered template through argv: Linux caps a
single argument at 128 KiB (MAX_ARG_STRLEN), so a large notebook used to fail
at container start with "exec /usr/local/bin/python: argument list too long",
surfacing as "Failed to extract results: missing markers".

These tests mock Docker and assert that every executor stages the template
into /work via put_archive and hands the container a short command. The
self-delete guard and the student-facing traceback shape are checked by
running the real Python templates locally (no Docker).
"""
import base64
import io
import json
import os
import posixpath
import shutil
import subprocess
import sys
import tarfile
import tempfile
from typing import Dict, List, Tuple
from unittest import mock

import nbformat
from django.test import SimpleTestCase

from autograder.services.executors import base as base_module
from autograder.services.executors.base import Executor
from autograder.services.executors.cpp import CPPExecutor, CPPNotebookExecutor
from autograder.services.executors.java import JavaExecutor, JavaNotebookExecutor
from autograder.services.executors.mock_file import MockFile
from autograder.services.executors.node import NodeExecutor, NodeNotebookExecutor
from autograder.services.executors.php import PHPExecutor, PHPNotebookExecutor
from autograder.services.executors.python import PythonExecutor, PythonNotebookExecutor
from autograder.services.executors.r import RExecutor, RNotebookExecutor
from autograder.services.executors.ruby import RubyExecutor, RubyNotebookExecutor

TEMPLATES_DIR = os.path.join(os.path.dirname(base_module.__file__), "..", "templates")

# Never compiled in the mocked runs, so one string serves every language.
SENTINEL = "CODEPOST_STAGING_SENTINEL_TEST"
SENTINEL_B64 = base64.b64encode(SENTINEL.encode()).decode()

MAX_TOKEN = 512


def _mock_file(data: str, name: str, extension: str) -> MockFile:
    mf = MockFile(data=data, name=name, extension=extension)
    mf.get_course = lambda: None  # type: ignore[attr-defined]
    return mf


def _notebook_file(sources: List[str], language: str) -> MockFile:
    kernels = {
        "python": ("python3", "python"),
        "R": ("ir", "R"),
        "javascript": ("javascript", "javascript"),
        "php": ("php", "php"),
        "ruby": ("ruby", "ruby"),
        "java": ("java", "java"),
        "cpp": ("xcpp17", "C++17"),
    }
    kernel_name, lang_name = kernels[language]
    nb = nbformat.v4.new_notebook()
    nb.metadata["kernelspec"] = {"name": kernel_name, "language": lang_name, "display_name": lang_name}
    nb.metadata["language_info"] = {"name": lang_name}
    for src in sources:
        nb.cells.append(nbformat.v4.new_code_cell(src))
    return _mock_file(nbformat.writes(nb), "notebook.ipynb", ".ipynb")


def _run_with_fake_docker(executor: Executor) -> Tuple[List[str], Dict[str, Tuple[str, int]]]:
    """Run execute() against a mocked Docker client.

    Returns the command handed to containers.create and every file staged via
    put_archive as {"/dest/name": (content, mode)}.
    """
    container = mock.Mock()
    container.wait.return_value = {"StatusCode": 0}
    container.logs.return_value = b""
    container.status = "exited"
    client = mock.Mock()
    client.containers.create.return_value = container

    with mock.patch.object(Executor, "_get_docker_client", return_value=client), \
            mock.patch.object(Executor, "_ensure_image", return_value=True):
        executor.execute()

    command = client.containers.create.call_args.kwargs["command"]
    staged: Dict[str, Tuple[str, int]] = {}
    for call in container.put_archive.call_args_list:
        dest, data = call.args
        with tarfile.open(fileobj=io.BytesIO(data)) as tar:
            for member in tar.getmembers():
                extracted = tar.extractfile(member)
                assert extracted is not None
                staged[posixpath.join(dest, member.name)] = (extracted.read().decode("utf-8"), member.mode)
    return command, staged


def _assert_no_payload_in_argv(test: SimpleTestCase, command: List[str]) -> None:
    joined = " ".join(command)
    test.assertLessEqual(max(len(tok) for tok in command), MAX_TOKEN, msg=f"oversized argv token in {command!r}")
    test.assertNotIn(SENTINEL, joined)
    test.assertNotIn(SENTINEL_B64, joined)
    test.assertNotIn("base64 -d", joined)


def _assert_contains_test_code(test: SimpleTestCase, content: str) -> None:
    test.assertTrue(SENTINEL in content or SENTINEL_B64 in content, msg="test code missing from staged runner")


class FileExecutorStagingTests(SimpleTestCase):
    """The per-language file executors (single source file + test script)."""

    def test_python(self):
        command, staged = _run_with_fake_docker(PythonExecutor(_mock_file("x = 1\n", "student.py", ".py"), test_code=SENTINEL))
        self.assertEqual(command, ["python", "/work/.codepost_runner.py"])
        _assert_no_payload_in_argv(self, command)
        content, _ = staged["/work/.codepost_runner.py"]
        _assert_contains_test_code(self, content)
        self.assertIn(base64.b64encode(b"x = 1\n").decode(), content)
        self.assertIn(".codepost_runner", content)  # self-delete guard

    def test_r(self):
        command, staged = _run_with_fake_docker(RExecutor(_mock_file("x <- 1\n", "student.R", ".R"), test_code=SENTINEL))
        self.assertEqual(command, ["Rscript", "/work/.codepost_runner.R"])
        _assert_no_payload_in_argv(self, command)
        _assert_contains_test_code(self, staged["/work/.codepost_runner.R"][0])
        self.assertEqual(staged["/work/student.R"][0], "x <- 1\n")

    def test_node(self):
        command, staged = _run_with_fake_docker(NodeExecutor(_mock_file("const x = 1;\n", "student.js", ".js"), test_code=SENTINEL))
        self.assertEqual(command, ["node", "/work/.codepost_runner.js"])
        _assert_no_payload_in_argv(self, command)
        _assert_contains_test_code(self, staged["/work/.codepost_runner.js"][0])

    def test_php(self):
        command, staged = _run_with_fake_docker(PHPExecutor(_mock_file("<?php $x = 1;\n", "student.php", ".php"), test_code=SENTINEL))
        self.assertEqual(command, ["php", "/work/.codepost_runner.php"])
        _assert_no_payload_in_argv(self, command)
        self.assertIn("/work/.codepost_runner.php", staged)

    def test_ruby(self):
        # No template is involved: the student's own file is the runner.
        command, staged = _run_with_fake_docker(RubyExecutor(_mock_file("x = 1\n", "student.rb", ".rb"), test_code=SENTINEL))
        self.assertEqual(command, ["ruby", "/work/script.rb"])
        _assert_no_payload_in_argv(self, command)
        self.assertEqual(staged["/work/script.rb"][0], "x = 1\n")

    def test_java(self):
        code = "public class Main { public static int f() { return 1; } }\n"
        command, staged = _run_with_fake_docker(JavaExecutor(_mock_file(code, "Main.java", ".java"), test_code=SENTINEL))
        self.assertEqual(command[:2], ["sh", "-c"])
        _assert_no_payload_in_argv(self, command)
        self.assertIn("rm -f TestRunner.java && java -ea TestRunner", command[2])
        self.assertEqual(staged["/work/Main.java"][0], code)
        _assert_contains_test_code(self, staged["/work/TestRunner.java"][0])

    def test_cpp(self):
        code = "int main() { return 0; }\n"
        command, staged = _run_with_fake_docker(CPPExecutor(_mock_file(code, "main.cpp", ".cpp"), test_code=SENTINEL))
        self.assertEqual(command[:2], ["sh", "-c"])
        _assert_no_payload_in_argv(self, command)
        self.assertIn("g++ -c runner.cpp -o runner.o && rm -f runner.cpp && ", command[2])
        self.assertEqual(staged["/work/source.cpp"][0], code)
        _assert_contains_test_code(self, staged["/work/runner.cpp"][0])


class NotebookExecutorStagingTests(SimpleTestCase):
    """The shared NotebookExecutor.execute() flow, per language."""

    CASES = [
        (PythonNotebookExecutor, "python", ["python", "/work/.codepost_runner.py"], "/work/.codepost_runner.py"),
        (RNotebookExecutor, "R", ["Rscript", "/work/.codepost_runner.R"], "/work/.codepost_runner.R"),
        (NodeNotebookExecutor, "javascript", ["node", "/work/.codepost_runner.js"], "/work/.codepost_runner.js"),
        (PHPNotebookExecutor, "php", ["php", "/work/.codepost_runner.php"], "/work/.codepost_runner.php"),
        (RubyNotebookExecutor, "ruby", ["ruby", "/work/.codepost_runner.rb"], "/work/.codepost_runner.rb"),
    ]

    def test_interpreted_languages(self):
        for cls, language, expected_command, runner_path in self.CASES:
            with self.subTest(executor=cls.__name__):
                command, staged = _run_with_fake_docker(cls(_notebook_file(["x = 1"], language), test_code=SENTINEL))
                self.assertEqual(command, expected_command)
                _assert_no_payload_in_argv(self, command)
                content, _ = staged[runner_path]
                _assert_contains_test_code(self, content)
                self.assertIn(".codepost_runner", content)  # self-delete guard

    def test_java(self):
        command, staged = _run_with_fake_docker(JavaNotebookExecutor(_notebook_file(["int x = 1;"], "java"), test_code=SENTINEL))
        self.assertEqual(command, ["sh", "-c", "javac NotebookRunner.java && rm -f NotebookRunner.java && java NotebookRunner"])
        content, _ = staged["/work/NotebookRunner.java"]
        _assert_contains_test_code(self, content)
        self.assertIn("class NotebookRunner", content)

    def test_cpp_test_mode(self):
        command, staged = _run_with_fake_docker(CPPNotebookExecutor(_notebook_file(["int x = 1;"], "cpp"), test_code=SENTINEL))
        self.assertEqual(command[:2], ["sh", "-c"])
        _assert_no_payload_in_argv(self, command)
        self.assertIn("g++ -c runner.cpp -o runner.o && rm -f runner.cpp && ", command[2])
        self.assertIn("int x = 1;", staged["/work/student.cpp"][0])
        _assert_contains_test_code(self, staged["/work/runner.cpp"][0])

    def test_cpp_standard_mode(self):
        command, staged = _run_with_fake_docker(CPPNotebookExecutor(_notebook_file(["int x = 1;"], "cpp")))
        self.assertEqual(command, ["sh", "-c", "g++ -o notebook notebook.cpp && ./notebook"])
        self.assertIn("int x = 1;", staged["/work/notebook.cpp"][0])

    def test_notebook_over_arg_max_is_staged_not_passed(self):
        """The regression: ~200 KB of cell source must not end up in argv."""
        big_cell = 'big = "' + ("a" * 200_000) + '"'
        for cls, language, runner_path in [
            (PythonNotebookExecutor, "python", "/work/.codepost_runner.py"),
            (RNotebookExecutor, "R", "/work/.codepost_runner.R"),
        ]:
            with self.subTest(executor=cls.__name__):
                command, staged = _run_with_fake_docker(cls(_notebook_file([big_cell], language), test_code=SENTINEL))
                self.assertLessEqual(max(len(tok) for tok in command), MAX_TOKEN)
                self.assertGreater(len(staged[runner_path][0]), 200_000)


class CommandWrapperTests(SimpleTestCase):

    def test_pre_script_wraps_python_runner(self):
        executor = PythonExecutor(_mock_file("x = 1\n", "student.py", ".py"), test_code=SENTINEL)
        executor.pre_script = "echo hi"
        command, staged = _run_with_fake_docker(executor)
        self.assertEqual(command, ["sh", "-c", "sh ./.pre_script.sh && rm .pre_script.sh && python /work/.codepost_runner.py"])
        content, mode = staged["/work/.pre_script.sh"]
        self.assertEqual(content, "#!/bin/sh\necho hi")
        self.assertEqual(mode, 0o777)
        self.assertIn("/work/.codepost_runner.py", staged)

    def test_pre_script_with_non_python_notebook(self):
        """Previously mangled: the sh -c payload was joined unquoted."""
        executor = RNotebookExecutor(_notebook_file(["x <- 1"], "R"), test_code=SENTINEL)
        executor.pre_script = "echo hi"
        command, _ = _run_with_fake_docker(executor)
        self.assertEqual(command[:2], ["sh", "-c"])
        self.assertTrue(command[2].endswith("Rscript /work/.codepost_runner.R"), command[2])

    def test_stdin_redirect(self):
        executor = PythonExecutor(_mock_file("x = input()\n", "student.py", ".py"), test_code=SENTINEL, input_data="1 2")
        command, _ = _run_with_fake_docker(executor)
        self.assertEqual(command, ["sh", "-c", "python /work/.codepost_runner.py < /tmp/stdin.txt"])

    def test_stdin_and_pre_script_nest_correctly(self):
        executor = PythonExecutor(_mock_file("x = input()\n", "student.py", ".py"), test_code=SENTINEL, input_data="1 2")
        executor.pre_script = "echo hi"
        command, _ = _run_with_fake_docker(executor)
        self.assertEqual(command, ["sh", "-c",
                                   "sh ./.pre_script.sh && rm .pre_script.sh && sh -c 'python /work/.codepost_runner.py < /tmp/stdin.txt'"])

    def test_wrapper_quotes_nested_shell_command(self):
        executor = PythonExecutor(_mock_file("x = 1\n", "student.py", ".py"))
        executor.pre_script = "echo hi"
        wrapped = executor._wrap_command_with_pre_script(["sh", "-c", "a && b"])
        self.assertEqual(wrapped[:2], ["sh", "-c"])
        self.assertTrue(wrapped[2].endswith("sh -c 'a && b'"), wrapped[2])


def _fill_python_template(student: str, test_code: str) -> str:
    with open(os.path.join(TEMPLATES_DIR, "template.py")) as f:
        template = f.read()
    return (template
            .replace("#{FILLER_CODE}", base64.b64encode(student.encode()).decode())
            .replace("#{TEST_CODE}", base64.b64encode(test_code.encode()).decode())
            .replace("#{TARGET_TEST_FUNCTION}", "")
            .replace("#{STUDENT_FILE_PATH}", "/work/hw1.py"))


def _fill_python_notebook_template(sources: List[str]) -> str:
    with open(os.path.join(TEMPLATES_DIR, "notebook_template.py")) as f:
        template = f.read()
    cells = [{"idx": i, "type": "code", "source": src} for i, src in enumerate(sources)]
    return (template
            .replace("{cells_b64}", base64.b64encode(json.dumps(cells).encode()).decode())
            .replace("{test_code_b64}", "")
            .replace("#{TARGET_TEST_FUNCTION}", ""))


def _run_script(interpreter: List[str], directory: str, name: str, content: str) -> subprocess.CompletedProcess:
    path = os.path.join(directory, name)
    with open(path, "w") as f:
        f.write(content)
    # errors="replace": raw templates with unfilled placeholders may emit non-UTF-8 bytes
    return subprocess.run(interpreter + [path], capture_output=True, text=True, errors="replace", cwd=directory, timeout=120)


class SelfDeleteGuardTests(SimpleTestCase):
    """Templates unlink themselves only when staged under the runner name."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _check(self, interpreter: List[str], template_name: str, ext: str, content: str | None = None):
        if content is None:
            with open(os.path.join(TEMPLATES_DIR, template_name)) as f:
                content = f.read()
        # Raw templates with unfilled placeholders may exit non-zero; the
        # guard is the first statement, so that does not matter here.
        _run_script(interpreter, self.tmp, f".codepost_runner{ext}", content)
        self.assertFalse(os.path.exists(os.path.join(self.tmp, f".codepost_runner{ext}")), "runner was not unlinked")
        _run_script(interpreter, self.tmp, f"other{ext}", content)
        self.assertTrue(os.path.exists(os.path.join(self.tmp, f"other{ext}")), "guard deleted a non-runner file")

    def test_python_file_template(self):
        self._check([sys.executable], "template.py", ".py", _fill_python_template("x = 1\n", ""))

    def test_python_notebook_template(self):
        self._check([sys.executable], "notebook_template.py", ".py", _fill_python_notebook_template(["x = 1"]))

    def test_node_templates(self):
        node = shutil.which("node")
        if not node:
            self.skipTest("node not installed")
        self._check([node], "template.js", ".js")
        self._check([node], "notebook_template.js", ".js")

    def test_ruby_templates(self):
        ruby = shutil.which("ruby")
        if not ruby:
            self.skipTest("ruby not installed")
        self._check([ruby], "template.rb", ".rb")
        self._check([ruby], "notebook_template.rb", ".rb")

    def test_php_templates(self):
        php = shutil.which("php")
        if not php:
            self.skipTest("php not installed")
        self._check([php], "template.php", ".php")
        self._check([php], "notebook_template.php", ".php")

    def test_r_templates(self):
        rscript = shutil.which("Rscript")
        if not rscript:
            self.skipTest("Rscript not installed")
        self._check([rscript], "template.r", ".R")
        self._check([rscript], "notebook_template.r", ".R")


class StudentTracebackTests(SimpleTestCase):
    """Student-facing tracebacks hide template frames and show student source."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _run_file_template(self, student: str, test_code: str) -> subprocess.CompletedProcess:
        return _run_script([sys.executable], self.tmp, ".codepost_runner.py", _fill_python_template(student, test_code))

    def test_failing_test_shows_student_line_not_template_frames(self):
        student = 'def boom():\n    x = {}\n    return x["missing"]\n'
        test_code = '@test(name="boom test", points=2)\ndef test_boom():\n    boom()\n'
        proc = self._run_file_template(student, test_code)
        block = proc.stderr.split("<<<TEST_RESULT_JSON_START>>>")[1].split("<<<TEST_RESULT_JSON_END>>>")[0]
        error = json.loads(block)["error"]
        self.assertIn('File "/work/hw1.py", line 3, in boom', error)
        self.assertIn('return x["missing"]', error)            # student source line
        # The test-script frame is listed but its source line is not revealed:
        # the student frame follows it directly.
        self.assertIn('File "/work/test_script.py", line 3, in test_boom\n  File "/work/hw1.py"', error)
        self.assertNotIn(".codepost_runner", error)
        self.assertNotIn('"<string>"', error)

    def test_top_level_crash_shows_student_source(self):
        proc = self._run_file_template("def helper():\n    return 1/0\n\nvalue = helper()\n", "")
        tail = proc.stderr.split("<<<RESULT>>>")[-1]
        self.assertIn("Student Code Runtime Error:", tail)
        self.assertIn('File "/work/hw1.py", line 2, in helper', tail)
        self.assertIn("return 1/0", tail)
        self.assertNotIn(".codepost_runner", tail)
        self.assertNotIn('"<string>"', tail)

    def test_syntax_error_is_clean(self):
        proc = self._run_file_template("def f(:\n    pass\n", "")
        tail = proc.stderr.split("<<<RESULT>>>")[-1]
        self.assertIn("Student Code Syntax Error:", tail)
        self.assertIn('File "/work/hw1.py", line 1', tail)
        self.assertIn("SyntaxError", tail)
        self.assertNotIn(".codepost_runner", tail)

    def test_notebook_cell_traceback_names_cell_and_shows_source(self):
        proc = _run_script([sys.executable], self.tmp, ".codepost_runner.py",
                           _fill_python_notebook_template(["x = 1", "def boom():\n    return {}['k']\nboom()"]))
        result = json.loads(proc.stdout.split("<<<RESULTS_START>>>")[1].split("<<<RESULTS_END>>>")[0])
        tracebacks: List[str] = []
        for cell in result["output_data"]["cells"]:
            for output in cell.get("outputs", []):
                if output.get("output_type") == "error":
                    tracebacks.extend(output.get("traceback", []))
        self.assertEqual(len(tracebacks), 1, result)
        tb = tracebacks[0]
        self.assertIn('File "<cell 1>", line 2, in boom', tb)
        self.assertIn("return {}['k']", tb)
        self.assertNotIn(".codepost_runner", tb)
