# Copyright © 2026 Rutgers, the State University of New Jersey. All rights reserved except as defined by the Rutgers Non-Commercial License, included with this software.
"""
Python executor for running Python code in a Docker container.

The following includes Python and Python Notebook executors.
"""

import base64
import os
import re
from datetime import datetime
import shutil
import logging
from typing import Any, List, Optional, cast
import nbformat
from requests.exceptions import ReadTimeout

from .base import Executor, NotebookExecutor, ExecutionResult

logger = logging.getLogger(__name__)


def _collect_local_python_modules(file_obj: Any, additional_files: dict[str, str]) -> set[str]:
    """
    Collect module/package names that are available from submission/assignment files.

    This lets us avoid attempting `pip install` for local modules (e.g. `math_utils.py`).
    """
    local_modules: set[str] = set()

    candidate_paths: List[str] = []

    main_name = getattr(file_obj, "name", None)
    main_path = getattr(file_obj, "path", None)
    if isinstance(main_name, str) and main_name:
        if isinstance(main_path, str) and main_path:
            candidate_paths.append(os.path.join(main_path, main_name))
        else:
            candidate_paths.append(main_name)

    candidate_paths.extend(additional_files.keys())

    for rel_path in candidate_paths:
        if not isinstance(rel_path, str) or not rel_path or rel_path.startswith("/"):
            # Absolute paths are usually system files and not importable user modules.
            continue

        normalized = rel_path.replace("\\", "/")
        parts = [p for p in normalized.split("/") if p]
        if not parts:
            continue

        filename = parts[-1]
        if filename.endswith(".py"):
            module_name = filename[:-3]
            if module_name and module_name != "__init__":
                local_modules.add(module_name)

        # Track top-level package directory so imports like `from lib.x import y`
        # do not trigger `pip install lib`.
        if len(parts) > 1:
            local_modules.add(parts[0])

        # If file is package initializer, include package folder name.
        if filename == "__init__.py" and len(parts) > 1:
            local_modules.add(parts[-2])

    return {m for m in local_modules if m}


def _filter_local_requirements(requirements_text: Optional[str], local_modules: set[str]) -> List[str]:
    """Filter out empty and local-module requirements while preserving order."""
    if not requirements_text:
        return []

    filtered: List[str] = []
    seen: set[str] = set()

    for raw_req in requirements_text.split("\n"):
        req = raw_req.strip()
        if not req:
            continue

        # Keep only simple package token for matching local modules.
        # Handles lightweight forms like `package==1.2.3` if ever present.
        req_name = re.split(r"[<>=!~\[]", req, 1)[0].strip()
        if req_name in local_modules:
            continue

        if req not in seen:
            seen.add(req)
            filtered.append(req)

    return filtered


class PythonExecutor(Executor):
    LANGUAGE = "python-3.12"
    TEMPLATE = "template.py"
    DOCKER_IMAGE = "python:3.12-slim"

    EXECUTABLE_EXTENSIONS = [".py"]
        
    PIP_CACHE_VOLUME_NAME = "codepost-pip-cache"
    
    INIT_DOCKER_VOLUME = {
        PIP_CACHE_VOLUME_NAME: {
            "bind": "/tmp/pip-cache",
            "mode": "rw"
        }
    }
   
    PIP_MODULE_TO_PACKAGE = {
        "sklearn": "scikit-learn",
        "cv2": "opencv-python",
        "PIL": "Pillow",
        "bs4": "beautifulsoup4",
        "yaml": "pyyaml",
        "dateutil": "python-dateutil",
    }
    
    @classmethod
    def is_executable(cls, file_name: Optional[str] = None, extension: Optional[str] = None, code: Optional[str] = None) -> bool:
        logger.info(f"Checking if {file_name} is executable")   

        if file_name is not None:
            extension = os.path.splitext(file_name)[1]
        if extension is not None and extension.lower() in cls.EXECUTABLE_EXTENSIONS:
            return True
        return False
    
    def _detect_imports(self, code) -> List[str]:
        # Refactored to use File Handler logic
        file_obj = cast(Any, self.file)
        if hasattr(file_obj, 'handler'):
            reqs = file_obj.handler.get_requirements()
            if reqs:
                local_modules = _collect_local_python_modules(self.file, self.additional_files)
                filtered = _filter_local_requirements(reqs, local_modules)
                removed = sorted(set(reqs.split('\n')) - set(filtered))
                if removed:
                    self.log(
                        f"Filtered local python modules from auto-install: {', '.join([r for r in removed if r])}",
                        "debug",
                    )
                return filtered
        
        # Fallback if handler not available or not working
        return []

    def _get_code_template(self, code: str, packages_to_install: List[str], test_code: str = "") -> Optional[str]:
        template = super()._get_code_template()
        if not template:
            return None
        
        if template and packages_to_install:
            # Modify the template to include package installation
            template = template.replace("packages_to_install = []", f"packages_to_install = {repr(packages_to_install)}")

        code_base64 = base64.b64encode(code.encode("utf-8")).decode("utf-8")
        test_code_base64 = base64.b64encode(test_code.encode("utf-8")).decode("utf-8")

        rel_path = self.file.name or "student.py"
        file_path = getattr(self.file, "path", None)
        if isinstance(file_path, str) and file_path:
            rel_path = os.path.join(file_path, rel_path)
        student_file_path = os.path.join("/work", rel_path).replace("\\", "/")

        template = template.replace("#{FILLER_CODE}", code_base64)
        template = template.replace("#{TEST_CODE}", test_code_base64)
        template = template.replace("#{TARGET_TEST_FUNCTION}", self.test_function if self.test_function else "")
        template = template.replace("#{STUDENT_FILE_PATH}", student_file_path)
        return template

    def execute(self) -> ExecutionResult:
        """Execute Python code in Docker container"""
        # Implementation of Python code execution
        timeout = self.DEFAULT_TIMEOUT
        start_time = datetime.now()
        
        self.log(f"[{self.image}] [{self.file.name}] [{self.LANGUAGE}] Starting execution")
        self.log(f"DEBUG_VERIFICATION: Volume definition: {self.INIT_DOCKER_VOLUME}", "info")
        
        if not self.file.data:
            return ExecutionResult.error("No code to execute")
        
        code = self.file.data
        
        # Detect imports
        packages_to_install = self._detect_imports(code)

        # Get code template
        template = self._get_code_template(code, packages_to_install, self.test_code or "")
        if not template:
            return ExecutionResult.error("Failed to get code template")

        # Execute code in Docker container
        
        client = self._get_docker_client()
        if not client:
            return ExecutionResult.error("Docker is not available")

        if not self._ensure_image(self.image):
            return ExecutionResult.error("Docker image is not available")


        if self.datasets or self.input_data:
            temp_staging_dir = self._create_staging_directory()
        else:
            temp_staging_dir = "" 
            
        volumes = self._get_volume_mounts(temp_staging_dir if (self.datasets or self.input_data) else "")
        
        # Add input staging
        if self.input_data:
            input_mounts = self._prepare_input_staging(temp_staging_dir)
            for container_path, host_path in input_mounts.items():
                volumes[host_path] = {'bind': container_path, 'mode': 'ro'}
        
        # The rendered template is staged into /work as a self-deleting runner
        # file (never through argv — Linux caps one argument at 128 KiB).
        runner_filename = ".codepost_runner.py"
        base_command = ["python", f"/work/{runner_filename}"]
        
        # Wrap with stdin first (logic: cmd < input)
        command_with_stdin = self._wrap_command_with_stdin(base_command)
        
        # Then wrap with pre-script (logic: setup && cmd)
        command = self._wrap_command_with_pre_script(command_with_stdin)
        
        docker_env = self._get_docker_environment()

        needs_network = True if len(packages_to_install) > 0 else False

        container = self.get_container(
            image_name=self.image, # Use self.image property
            command=command,
            env=docker_env,
            volumes=volumes,
            needs_network=needs_network
        )
        if not container:
            return ExecutionResult.error("Failed to create Docker container")
        
        self.add_additional_files(container)
        self._put_file(container, '/work', runner_filename, template)
        self.add_pre_script(container)  # Inject the pre-script file
        try:
            container.start()
            self.log("Starting code execution in Docker container")
            adjusted_timeout = timeout + (30 * len(packages_to_install))
            result = container.wait(timeout=adjusted_timeout)
            stdout = container.logs(stdout=True, stderr=False).decode('utf-8', errors='replace')
            stderr = container.logs(stdout=False, stderr=True).decode('utf-8', errors='replace')

            # Parse template logs from stderr if marker exists
            template_logs = ""
            if "<<<RESULT>>>" in stderr:
                parts = stderr.split("<<<RESULT>>>")
                template_logs = parts[0]
                stderr = parts[1]
                if stderr.startswith("\n"):
                    stderr = stderr[1:]
                elif stderr.startswith("\r\n"):
                    stderr = stderr[2:]

            # Parse plots (like R executor)
            import re

            # Move template-prefixed messages from stderr into template logs so
            # student-facing stderr only contains student/test errors.
            template_line_regex = re.compile(r'^\[CODEPOST_TEMPLATE\]\[[A-Z]+\]\s.*(?:\r?\n)?', re.MULTILINE)
            template_line_matches = template_line_regex.findall(stderr)
            if template_line_matches:
                extracted_template_logs = "".join(template_line_matches).strip()
                if extracted_template_logs:
                    if template_logs:
                        template_logs = f"{template_logs.rstrip()}\n{extracted_template_logs}"
                    else:
                        template_logs = extracted_template_logs
                stderr = template_line_regex.sub("", stderr).lstrip("\r\n")

            img_regex = re.compile(r'<<<CODEPOST_PLOT:(.*?)>>>', re.DOTALL)
            images = []
            
            def replace_and_capture(match):
                images.append(match.group(1).strip().replace('\n', '').replace('\r', ''))
                return "" # Remove from stdout
            
            stdout = img_regex.sub(replace_and_capture, stdout)
            
            # Parse Test Results using base class method
            # This handles both stderr (where tests are usually printed) and stdout
            stdout, stderr, test_results = self.parse_test_results(stdout, stderr)
            
            output_data = {}
            if images:
                output_data['image/png'] = images[-1] # Backward compat
                output_data['images'] = images

            end_time = datetime.now()
            execution_time = (end_time - start_time).total_seconds()
            success = result.get('StatusCode', 1) == 0
            self.log(f"Execution completed in {execution_time:.2f}s with exit code {result.get('StatusCode', 1)}")

            # Truncate output if too large
            if len(stdout) > self.MAX_OUTPUT_SIZE:
                self.log("Truncating stdout due to size limit")
                stdout = "ERROR: Output truncated due to large output size\n" + stdout[:self.MAX_OUTPUT_SIZE] + "\n...[truncated stdout over limit]..."
            if len(stderr) > self.MAX_OUTPUT_SIZE:
                self.log("Truncating stderr due to size limit")
                stderr = "ERROR: Output truncated due to large output size\n" + stderr[:self.MAX_OUTPUT_SIZE] + "\n...[truncated stderr over limit]..."

            # Merge template logs into system_logs
            full_system_logs = list(self.executor_logs)
            if template_logs:
                full_system_logs.append("--- Template Logs ---\n" + template_logs)

            result = ExecutionResult(
                success=success,
                stdout=stdout,
                stderr=stderr,
                err=None if success else f"Non-zero exit code: {result.get('StatusCode', 1)}",
                execution_time=execution_time,
                output_data=output_data,
                system_logs=full_system_logs,
                tests=test_results
            )

            return result
        except ReadTimeout:
            container.kill()
            return ExecutionResult.error("Execution timed out")
        except Exception as e:
            container.kill()
            return ExecutionResult.error(f"Execution failed: {e}")
        finally:
            self.log("Cleaning up Docker container")
            container.remove()
            if self.datasets:
                shutil.rmtree(temp_staging_dir, ignore_errors=True)

class PythonNotebookExecutor(NotebookExecutor):
    LANGUAGE = "python"

    TEMPLATE = "notebook_template.py"
    DOCKER_IMAGE = "python:3.12-slim"
    EXECUTABLE_EXTENSIONS = ['.ipynb']
    EXECUTION_COMMAND = ["python"]
    RUNNER_FILENAME = ".codepost_runner.py"
       
    PIP_CACHE_VOLUME_NAME = PythonExecutor.PIP_CACHE_VOLUME_NAME
    
    INIT_DOCKER_VOLUME = PythonExecutor.INIT_DOCKER_VOLUME.copy()

    PIP_MODULE_TO_PACKAGE = PythonExecutor.PIP_MODULE_TO_PACKAGE.copy()

    @classmethod
    def is_executable(cls, file_name: Optional[str] = None, extension: Optional[str] = None, code: Optional[str] = None) -> bool:
        """
        A notebook is converted into a executable python file which is parsed out by the notebook_template.py

        For a notebook to be executable it must have a python kernel in the metadata
        and a .ipynb extension
        """
        if file_name is not None:
            extension = os.path.splitext(file_name)[1]

        if extension is None or extension.lower() not in cls.EXECUTABLE_EXTENSIONS:
            return False

        try:
            # Strictly match Python notebooks when metadata is available.
            if cls.notebook_matches_language(code, ['python', 'python3', 'py']):
                return True
            detected_language = cls.detect_notebook_language(code)
            if detected_language and detected_language != 'python':
                return False
        except Exception:
            pass  # If we can't infer metadata, keep extension fallback for backwards compatibility

        if extension.lower() in cls.EXECUTABLE_EXTENSIONS:
            return True

        return False

    def _detect_imports(self, nb: nbformat.NotebookNode) -> List[str]:
        """
        Detect imported packages from all notebook cells.
        Refactored to use File Handler logic.
        """
        file_obj = cast(Any, self.file)
        if hasattr(file_obj, 'handler'):
            reqs = file_obj.handler.get_requirements()
            if reqs:
                local_modules = _collect_local_python_modules(self.file, self.additional_files)
                filtered = _filter_local_requirements(reqs, local_modules)
                self.log(f"Detected packages via Handler: {reqs}", "debug")
                if filtered != reqs.split('\n'):
                    self.log(f"Filtered install packages: {filtered}", "debug")
                return filtered

        # Fallback for now if needed, but redundant with Handlers
        return []

    def _get_code_template(self, code: str, packages_to_install: List[str], test_code: str = "") -> Optional[str]:
        """Get the Python notebook template with cells and packages substituted."""
        template = super()._get_code_template() # Corrected: Base Executor takes no args
        if not template:
            return None
        
        
        # Replace installation packages
        if packages_to_install:
            template = template.replace("packages_to_install = []", f"packages_to_install = {repr(packages_to_install)}")

        template = template.replace('{cells_b64}', code)
        
        # Base64 encode test code
        import base64
        test_code_b64 = base64.b64encode(test_code.encode('utf-8')).decode('utf-8')
        template = template.replace("{test_code_b64}", test_code_b64)
        template = template.replace("#{TARGET_TEST_FUNCTION}", self.test_function if self.test_function else "")
        
        return template
