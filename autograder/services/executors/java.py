# Copyright © 2026 Rutgers, the State University of New Jersey. All rights reserved except as defined by the Rutgers Non-Commercial License, included with this software.
import os
import logging
import base64
import json
import re
import shlex
from datetime import datetime
from typing import List, Optional
import shutil

from .base import Executor, NotebookExecutor, ExecutionResult

logger = logging.getLogger(__name__)

# A pasted JUnit (Jupiter) test script imports org.junit.*; the legacy custom
# harness (@Test(name=,points=)) never does. Used to pick the execution path.
_JUNIT_IMPORT_RE = re.compile(r'^\s*import\s+org\.junit', re.MULTILINE)

# TestService appends this Python sentinel to every test script (TestService.py).
# It is valid Python but breaks javac, so the JUnit path strips it off.
_TIMEOUTS_SENTINEL_RE = re.compile(
    r'\n*CODEPOST_TEST_TIMEOUTS\s*=\s*\{.*?\}\s*$', re.DOTALL
)

# First public top-level class in a .java source — used to name the test file so
# javac accepts it (a public class must live in <ClassName>.java).
_PUBLIC_CLASS_RE = re.compile(
    r'\bpublic\s+(?:final\s+|abstract\s+)*class\s+([A-Za-z_$][A-Za-z0-9_$]*)'
)
_ANY_CLASS_RE = re.compile(
    r'\bclass\s+([A-Za-z_$][A-Za-z0-9_$]*)'
)

class JavaExecutor(Executor):
    LANGUAGE = "java-17"
    EXECUTABLE_EXTENSIONS = [".java"]
    DOCKER_IMAGE = "eclipse-temurin:17-jdk"
    TEMPLATE = "TestRunner.java"

    @classmethod
    def is_executable(cls, file_name: Optional[str] = None, extension: Optional[str] = None, code: Optional[str] = None) -> bool:
        if file_name is not None:
            extension = os.path.splitext(file_name)[1]
        if extension and extension.lower() in cls.EXECUTABLE_EXTENSIONS:
            return True
        return False
        
    def _detect_imports(self, code: str) -> List[str]:
        return []

    def _extract_package_name(self, code: str) -> Optional[str]:
        match = re.search(r"^\s*package\s+([A-Za-z_][A-Za-z0-9_\.]*)\s*;", code, flags=re.MULTILINE)
        return match.group(1) if match else None

    @staticmethod
    def _check_java_filename(filename: str, code: str) -> Optional[str]:
        """Return an error message if the filename doesn't match the declared public class.

        javac requires a public class Foo to live in Foo.java exactly. Students
        sometimes upload 'HashtagExtractor - Copy.java' or 'Main (2).java' from
        Windows; this produces a cryptic compile error. Surface it clearly instead.
        Returns None if the filename is acceptable.
        """
        stem = os.path.splitext(os.path.basename(filename))[0]
        m = _PUBLIC_CLASS_RE.search(code)
        if m and m.group(1) != stem:
            return (
                f"File is named '{filename}' but declares 'public class {m.group(1)}'. "
                f"Rename the file to '{m.group(1)}.java' and re-upload."
            )
        return None

    def _get_source_relative_path(self, filename: str, code: str) -> str:
        """
        Resolve where the main source file should be written inside /work.

        Priority:
        1) explicit file.path from DB (preserve uploaded relative structure)
        2) filename in working directory — let _NORMALIZE_SOURCES_CMD move it
           to the right package path at runtime (avoids creating root-owned
           package dirs via put_archive that would block the normalizer's mv)
        """
        if getattr(self.file, "path", None):
            return os.path.join(self.file.path, filename)

        return filename

    def _get_code_template(self, test_code: str = "") -> Optional[str]:
        """Get the Java template and inject test code."""
        # We manually load the template here because base.py _get_code_template assumes substitutions
        template_file = self.TEMPLATE
        template_path = os.path.join(os.path.dirname(__file__), "../templates", template_file)
        try:
            with open(template_path, 'r') as f:
                template = f.read()
            return template.replace("#{TEST_CODE}", test_code)
        except Exception as e:
            self.log(f"Failed to load template: {e}", "error")
            return None

    # --- JUnit (Jupiter) execution path -------------------------------------

    # Shell that places a package-declared .java file into its package dir ONLY when
    # it isn't already correctly located. A file is already correct when its directory
    # ends with the package path (e.g. a strict `src/bg/.../Car.java` layout, or any
    # source root prefix) — those are left untouched. Only a genuinely flat upload
    # (`./Car.java` declaring `package bg...;`) is moved so javac + classpath resolve.
    # JUnitPlatformRunner has no package → never matches, stays at root.
    # Uses `find | while read` (not `for f in $(find ...)`) so filenames with spaces
    # (e.g. "Main - Copy.java") don't split into multiple tokens.
    _NORMALIZE_SOURCES_CMD = (
        "find . -type f -name '*.java' | while IFS= read -r f; do "
        "pkg=$(sed -n \"s/^[[:space:]]*package[[:space:]][[:space:]]*\\([A-Za-z_][A-Za-z0-9_.]*\\)[[:space:]]*;.*/\\1/p\" \"$f\" | head -n 1); "
        "if [ -n \"$pkg\" ]; then "
        "pkg_path=$(printf '%s' \"$pkg\" | tr '.' '/'); "
        "dir=$(dirname \"$f\"); "
        # Already correct if the file's directory ends with its package path — i.e.
        # it lives under some source root (./src/bg/..., ./bg/..., etc.). Leave it.
        "case \"$dir/\" in "
        "*\"/$pkg_path/\") : ;; "
        "*) mkdir -p \"./$pkg_path\" && mv -f \"$f\" \"./$pkg_path/$(basename \"$f\")\"; ;; "
        "esac; "
        "fi; "
        "done"
    )

    @staticmethod
    def _strip_timeouts_sentinel(test_code: str) -> str:
        """Remove the Python CODEPOST_TEST_TIMEOUTS sentinel TestService appends.
        It is valid Python but would break javac if left in a .java file."""
        return _TIMEOUTS_SENTINEL_RE.sub('', test_code).rstrip()

    @staticmethod
    def _extract_timeout_map(test_code: str) -> dict:
        """Parse the appended `CODEPOST_TEST_TIMEOUTS = {...}` map (functionName -> seconds)."""
        m = re.search(r'CODEPOST_TEST_TIMEOUTS\s*=\s*(\{.*?\})\s*$', test_code, re.DOTALL)
        if not m:
            return {}
        try:
            return json.loads(m.group(1))
        except Exception:
            return {}

    @staticmethod
    def _test_class_filename(test_code: str) -> str:
        """Filename a public test class must live in (<ClassName>.java)."""
        m = _PUBLIC_CLASS_RE.search(test_code) or _ANY_CLASS_RE.search(test_code)
        return f"{m.group(1)}.java" if m else "SubmittedTest.java"

    def _render_junit_runner(self, points_by_function: dict, default_timeout_s: int) -> Optional[str]:
        """Load JUnitPlatformRunner.java and inject the points map + default timeout."""
        template_path = os.path.join(os.path.dirname(__file__), "../templates", "JUnitPlatformRunner.java")
        try:
            with open(template_path, 'r') as f:
                template = f.read()
        except Exception as e:
            self.log(f"Failed to load JUnitPlatformRunner template: {e}", "error")
            return None

        # Build Java statements that populate the points map. Keys are
        # "ClassName.methodName"; the runner looks up the composite key first and
        # falls back to the bare method name. We also emit bare-method keys, but ONLY
        # for method names that are unambiguous across the category (exactly one test
        # with that method name) — so an unexpected className mismatch degrades to the
        # correct points instead of silently defaulting to 1.0, while a genuinely
        # duplicated method name never resolves to the wrong test's points.
        entries = dict(points_by_function)
        bare_counts: dict = {}
        for fn in entries:
            bare = fn.split('.', 1)[1] if '.' in fn else fn
            bare_counts[bare] = bare_counts.get(bare, 0) + 1
        for fn, pts in list(entries.items()):
            bare = fn.split('.', 1)[1] if '.' in fn else fn
            if bare != fn and bare_counts.get(bare) == 1 and bare not in entries:
                entries[bare] = pts
        put_lines = "\n        ".join(
            f'm.put({json.dumps(str(fn))}, {float(pts)});'
            for fn, pts in entries.items()
        )
        template = template.replace("#{POINTS_JSON}", put_lines)
        template = template.replace("#{DEFAULT_TIMEOUT}", f"{int(default_timeout_s)}s")
        return template

    def _execute_junit5(self, start_time: datetime) -> ExecutionResult:
        code = self.file.data
        raw_test_code = self.test_code or ""

        # 1. Clean the test code and pull per-test timeouts out of the sentinel.
        timeout_map = self._extract_timeout_map(raw_test_code)
        test_code = self._strip_timeouts_sentinel(raw_test_code)

        # Resolve the set of test files to stage. Prefer the category's test files
        # (multi-file categories) passed via self.test_files; fall back to the
        # single stripped test_code for legacy single-script categories.
        if self.test_files:
            test_files = [
                {'name': f['name'], 'content': f['content']}
                for f in self.test_files if (f.get('content') or '').strip()
            ]
        elif test_code.strip():
            test_files = [{'name': self._test_class_filename(test_code), 'content': test_code}]
        else:
            test_files = []
        if not test_files:
            return ExecutionResult.error("JUnit test script is empty")

        # 2. Points: prefer the DB-owned map (functionName -> points) supplied by
        #    TestService; fall back to re-parsing when absent (e.g. ad-hoc runs).
        if self.points_by_function:
            points_by_function = dict(self.points_by_function)
        else:
            from autograder.services.TestParsingService import TestParsingService
            points_by_function = {}
            for tf in test_files:
                # Derive className from the class declared in the source (matching the
                # runtime getClassName()), not the filename stem.
                class_name = TestParsingService._derive_class_name(
                    tf.get('name') or "", tf.get('content') or "", 'java')
                try:
                    for t in TestParsingService._parse_java(tf['content']):
                        if t.get('functionName'):
                            key = f"{class_name}.{t['functionName']}" if class_name else t['functionName']
                            points_by_function[key] = t.get('points', 1.0)
                except Exception:
                    pass

        # 3. Co-compilation guard: a student file must not redefine a test class or
        #    shadow framework packages. Checked against every test file.
        filename = self.file.name
        if not filename.endswith(".java"):
            filename += ".java"

        # Validate the filename of EVERY student .java file (the representative plus
        # every additional submission file), not just the representative: a public
        # class must live in <ClassName>.java or javac fails with a cryptic error.
        # self.additional_files is {full_path: source}; the representative is self.file.
        student_files = {filename: code}
        for full_path, data in (self.additional_files or {}).items():
            if full_path.endswith(".java"):
                student_files[os.path.basename(full_path)] = data
        for fname, src in student_files.items():
            filename_err = self._check_java_filename(fname, src)
            if filename_err:
                return ExecutionResult.error(filename_err)

        for tf in test_files:
            guard_err = self._junit_cocompile_guard(code, filename, tf['content'])
            if guard_err:
                return ExecutionResult.error(guard_err)

        default_timeout_s = max([30] + [int(v) for v in timeout_map.values() if v])
        runner = self._render_junit_runner(points_by_function, default_timeout_s)
        if not runner:
            return ExecutionResult.error("Failed to load JUnit runner template")

        client = self._get_docker_client()
        if not client:
            return ExecutionResult.error("Docker is not available")
        if not self._ensure_image(self.DOCKER_IMAGE):
            return ExecutionResult.error("Docker image not available")

        # 4. Guard against an image with no baked libs (JUnit/Mockito on classpath).
        if self.image == self.DOCKER_IMAGE:
            return ExecutionResult.error(
                "This Java environment has no JUnit/Mockito libraries baked in. "
                "Rebuild the environment (java-27) so /opt/codepost/libs is populated."
            )

        if self.datasets:
            temp_staging_dir = self._create_staging_directory()
        else:
            temp_staging_dir = ""
        volumes = self._get_volume_mounts(temp_staging_dir if self.datasets else "")
        docker_env = self._get_docker_environment()

        source_relative_path = self._get_source_relative_path(filename, code)

        runner_files = {
            source_relative_path: code,
            "JUnitPlatformRunner.java": runner,
        }
        for tf in test_files:
            runner_files[tf['name']] = tf['content']

        libs = "/opt/codepost/libs/*"
        cmd_str = (
            f"{self._NORMALIZE_SOURCES_CMD} && "
            f"mkdir -p out && "
            f"find . -type f -name '*.java' -print0 | xargs -0 javac -cp '{libs}' -d out && "
            f"agent=$(ls /opt/codepost/libs/byte-buddy-agent-*.jar 2>/dev/null | head -n 1); "
            f"java -ea ${{agent:+-javaagent:$agent}} -Xmx512m -XX:MaxMetaspaceSize=128m "
            f"-cp '{libs}:out' JUnitPlatformRunner"
        )

        base_command = ["sh", "-c", cmd_str]
        command = self._wrap_command_with_pre_script(base_command)

        container = self.get_container(
            image_name=self.image,
            command=command,
            env=docker_env,
            volumes=volumes,
            needs_network=False,
        )
        if not container:
            return ExecutionResult.error("Failed to create container")

        self.add_additional_files(container)
        self.add_pre_script(container)
        for name, content in runner_files.items():
            self._put_file(container, '/work', name, content)

        try:
            container.start()
            result = container.wait(timeout=self.DEFAULT_TIMEOUT)
            stdout = container.logs(stdout=True, stderr=False).decode('utf-8', errors='replace')
            stderr = container.logs(stdout=False, stderr=True).decode('utf-8', errors='replace')

            stdout, stderr, test_results = self.parse_test_results(stdout, stderr)
            execution_time = (datetime.now() - start_time).total_seconds()
            success = result.get('StatusCode', 1) == 0

            return ExecutionResult(
                success=success,
                stdout=stdout,
                stderr=stderr,
                err=None if success else f"Exit Code: {result.get('StatusCode')}",
                execution_time=execution_time,
                tests=test_results,
            )
        except Exception as e:
            container.kill()
            return ExecutionResult.error(f"Execution failed: {e}")
        finally:
            container.remove()
            if self.datasets:
                shutil.rmtree(temp_staging_dir, ignore_errors=True)

    def _junit_cocompile_guard(self, student_code: str, student_name: str, test_code: str) -> Optional[str]:
        """Reject student files that would shadow the test class or framework packages."""
        student_pkg = self._extract_package_name(student_code) or ""
        # Forbid declaring framework packages.
        for forbidden in ("org.junit", "org.mockito", "org.opentest4j", "net.bytebuddy", "java.", "javax."):
            if student_pkg == forbidden.rstrip('.') or student_pkg.startswith(forbidden):
                return (
                    f"Submission file declares a reserved package '{student_pkg}'. "
                    "Student code may not live in framework packages."
                )
        # Forbid redefining a test class name.
        test_classes = set(_ANY_CLASS_RE.findall(test_code))
        student_base = os.path.splitext(os.path.basename(student_name))[0]
        if student_base in test_classes:
            return (
                f"Submission file '{student_name}' collides with a test class name. "
                "Rename the submission so it cannot shadow the instructor's tests."
            )
        return None

    def execute(self) -> ExecutionResult:
        start_time = datetime.now()
        
        if not self.file.data:
             return ExecutionResult.error("No code to execute")

        # Real JUnit (Jupiter) tests run through a dedicated path that compiles the
        # student code + the unmodified test file against the baked JUnit/Mockito
        # jars and drives them with JUnitPlatformRunner. The legacy custom-harness
        # path below (TestRunner.java) is unchanged for non-JUnit test scripts.
        if self.test_code and _JUNIT_IMPORT_RE.search(self.test_code):
            return self._execute_junit5(start_time)

        code = self.file.data
        filename = self.file.name
        if not filename.endswith(".java"):
            filename += ".java"
        filename_err = self._check_java_filename(filename, code)
        if filename_err:
            return ExecutionResult.error(filename_err)

        source_relative_path = self._get_source_relative_path(filename, code)
        package_name = self._extract_package_name(code)
        classname = os.path.splitext(filename)[0]
        run_classname = f"{package_name}.{classname}" if package_name else classname
        
        client = self._get_docker_client()
        if not client:
            return ExecutionResult.error("Docker is not available")
            
        if not self._ensure_image(self.DOCKER_IMAGE):
             return ExecutionResult.error("Docker image not available")
             
        if self.datasets:
            temp_staging_dir = self._create_staging_directory()
        else:
            temp_staging_dir = ""
            
        volumes = self._get_volume_mounts(temp_staging_dir if self.datasets else "")
        docker_env = self._get_docker_environment()
        
        # Sources are staged into /work via _put_file before start (never
        # through argv — Linux caps one argument at 128 KiB). put_archive
        # creates missing parent directories.
        runner_files = {source_relative_path: code}

        # Normalize package-declared Java sources into package paths so javac can
        # resolve cross-file references (e.g., Main.java -> Helper.java) even when
        # files were uploaded at the workspace root.
        normalize_sources_cmd = self._NORMALIZE_SOURCES_CMD

        compile_all_cmd = "find . -type f -name '*.java' -print0 | xargs -0 javac -d ."
        
        if self.test_code:
            # --- Testing Mode ---
            template = self._get_code_template(self.test_code)
            if not template:
                return ExecutionResult.error("Failed to load Java test template")
                
            runner_files["TestRunner.java"] = template

            # Command: Compile Both -> drop the TestRunner source -> Run TestRunner
            # We assume the student class is "Main" or whatever filename is, and TestRunner calls it.
            # NOTE: Student code must be public or compatible.

            cmd_str = (
                f"{normalize_sources_cmd} && "
                f"{compile_all_cmd} && "
                f"rm -f TestRunner.java && "
                f"java -ea TestRunner"
            )
        else:
            # --- Standard Execution Mode ---
            cmd_str = (
                f"{normalize_sources_cmd} && "
                f"{compile_all_cmd} && "
                f"java -ea -cp . {shlex.quote(run_classname)}"
            )
            
        base_command = ["sh", "-c", cmd_str]
        command = self._wrap_command_with_pre_script(base_command)

        container = self.get_container(
            image_name=self.image,
            command=command,
            env=docker_env,
            volumes=volumes,
            needs_network=False
        )
        
        if not container:
             return ExecutionResult.error("Failed to create container")
             
        self.add_additional_files(container)
        self.add_pre_script(container)
        for name, content in runner_files.items():
            self._put_file(container, '/work', name, content)

        try:
            container.start()
            result = container.wait(timeout=self.DEFAULT_TIMEOUT)
            stdout = container.logs(stdout=True, stderr=False).decode('utf-8', errors='replace')
            stderr = container.logs(stdout=False, stderr=True).decode('utf-8', errors='replace')

            # Parse Test Results (if any)
            stdout, stderr, test_results = self.parse_test_results(stdout, stderr)
            
            execution_time = (datetime.now() - start_time).total_seconds()
            success = result.get('StatusCode', 1) == 0
            
            return ExecutionResult(
                success=success,
                stdout=stdout,
                stderr=stderr,
                err=None if success else f"Exit Code: {result.get('StatusCode')}",
                execution_time=execution_time,
                tests=test_results
            )
        except Exception as e:
            container.kill()
            return ExecutionResult.error(f"Execution failed: {e}")
        finally:
            container.remove()
            if self.datasets:
                shutil.rmtree(temp_staging_dir, ignore_errors=True)

class JavaNotebookExecutor(NotebookExecutor):
    """
    Executor for Java Jupyter notebooks.
    
    Uses JShell to execute the Java notebook template.
    """
    LANGUAGE = "java"
    TEMPLATE = "notebook_template.java"
    DOCKER_IMAGE = "eclipse-temurin:21-jdk"
    EXECUTABLE_EXTENSIONS = ['.ipynb']
    EXECUTION_COMMAND = ["java"]  # Will be overridden in _get_execution_command
    RUNNER_FILENAME = "NotebookRunner.java"  # class name is forced to NotebookRunner in _get_code_template

    @classmethod
    def is_executable(cls, file_name: Optional[str] = None, extension: Optional[str] = None, code: Optional[str] = None) -> bool:
        """
        Check if this is a Java notebook.
        
        A Java notebook must have a Java kernel and .ipynb extension.
        """
        if file_name is not None:
            extension = os.path.splitext(file_name)[1]

        if extension is None or extension.lower() not in cls.EXECUTABLE_EXTENSIONS:
            return False

        return cls.notebook_matches_language(code, ['java', 'ijava'])

    def _get_execution_command(self) -> List[str]:
        """
        Compile the staged NotebookRunner.java, drop the source, run the class.
        (The base execute() stages RUNNER_FILENAME into /work before start.)
        """
        return ["sh", "-c", "javac NotebookRunner.java && rm -f NotebookRunner.java && java NotebookRunner"]

    def _get_code_template(self, code: str, packages_to_install: List[str], test_code: str = "") -> Optional[str]:
        """Get the Java notebook template with cells substituted."""
        # We don't call super() because we want custom replacement for Java
        # But we need the template content.
        # Actually super()._get_code_template() loads self.TEMPLATE file.
        template_file = self.TEMPLATE
        if not template_file:
             return None
        
        template_path = os.path.join(os.path.dirname(__file__), "../templates", template_file)
        try:
            with open(template_path, 'r') as f:
                template = f.read()
        except Exception as e:
            self.log(f"Failed to load template: {e}", "error")
            return None

        template = template.replace('{cells_b64}', code)
        template = template.replace('{test_code_b64}', base64.b64encode(test_code.encode('utf-8')).decode('utf-8') if test_code else "")
        
        # Remove package declaration if present, since we run from root
        template = template.replace("package autograder.services.templates;", "")
        
        # Ensure class name matches `NotebookRunner` if I forced it in command
        template = template.replace("public class notebook_template", "public class NotebookRunner")
        template = template.replace("class notebook_template", "class NotebookRunner")
        
        return template
