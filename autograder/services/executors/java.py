# Copyright © 2026 Rutgers, the State University of New Jersey. All rights reserved except as defined by the Rutgers Non-Commercial License, included with this software.
import os
import logging
import base64
import re
import shlex
from datetime import datetime
from typing import List, Optional
import shutil

from .base import Executor, NotebookExecutor, ExecutionResult

logger = logging.getLogger(__name__)

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

    def execute(self) -> ExecutionResult:
        start_time = datetime.now()
        
        if not self.file.data:
             return ExecutionResult.error("No code to execute")
             
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
            
        command = ["sh", "-c", cmd_str]
        
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
