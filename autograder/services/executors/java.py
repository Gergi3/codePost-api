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

    def _get_source_relative_path(self, filename: str, code: str) -> str:
        """
        Resolve where the main source file should be written inside /work.

        Priority:
        1) explicit file.path from DB (preserve uploaded relative structure)
        2) package declaration-derived path when file.path is absent
        3) filename in working directory
        """
        if getattr(self.file, "path", None):
            return os.path.join(self.file.path, filename)

        package_name = self._extract_package_name(code)
        if package_name:
            package_path = package_name.replace(".", "/")
            return os.path.join(package_path, filename)

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

    # Shell that moves every package-declared .java file into its package dir so
    # javac resolves cross-file references regardless of upload layout. Shared by
    # the legacy and JUnit paths. JUnitPlatformRunner has no package → stays at root.
    _NORMALIZE_SOURCES_CMD = (
        "for f in $(find . -type f -name '*.java'); do "
        "pkg=$(sed -n \"s/^[[:space:]]*package[[:space:]]\\+\\([A-Za-z_][A-Za-z0-9_\\.]*\\)[[:space:]]*;.*/\\1/p\" \"$f\" | head -n 1); "
        "if [ -n \"$pkg\" ]; then "
        "pkg_path=$(printf '%s' \"$pkg\" | tr '.' '/'); "
        "target=./$pkg_path/$(basename \"$f\"); "
        "if [ \"$f\" != \"$target\" ]; then "
        "mkdir -p \"$(dirname \"$target\")\" && mv \"$f\" \"$target\"; "
        "fi; "
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

        # Build Java statements that populate the points map. Keys are method names.
        put_lines = "\n        ".join(
            f'm.put({json.dumps(str(fn))}, {float(pts)});'
            for fn, pts in points_by_function.items()
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
                try:
                    for t in TestParsingService._parse_java(tf['content']):
                        if t.get('functionName'):
                            points_by_function[t['functionName']] = t.get('points', 1.0)
                except Exception:
                    pass

        # 3. Co-compilation guard: a student file must not redefine a test class or
        #    shadow framework packages. Checked against every test file.
        for tf in test_files:
            guard_err = self._junit_cocompile_guard(code, self.file.name or "Main.java", tf['content'])
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

        filename = self.file.name or "Main.java"
        if not filename.endswith(".java"):
            filename += ".java"
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
            f"javac -cp '{libs}' -d out $(find . -type f -name '*.java') && "
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
        filename = self.file.name or "Main.java"
        if not filename.endswith(".java"):
            filename += ".java"

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
        normalize_sources_cmd = (
            "for f in $(find . -type f -name '*.java'); do "
            "pkg=$(sed -n \"s/^[[:space:]]*package[[:space:]]\\+\\([A-Za-z_][A-Za-z0-9_\\.]*\\)[[:space:]]*;.*/\\1/p\" \"$f\" | head -n 1); "
            "if [ -n \"$pkg\" ]; then "
            "pkg_path=$(printf '%s' \"$pkg\" | tr '.' '/'); "
            "target=./$pkg_path/$(basename \"$f\"); "
            "if [ \"$f\" != \"$target\" ]; then "
            "mkdir -p \"$(dirname \"$target\")\" && mv \"$f\" \"$target\"; "
            "fi; "
            "fi; "
            "done"
        )

        compile_all_cmd = "javac -d . $(find . -type f -name '*.java')"
        
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
