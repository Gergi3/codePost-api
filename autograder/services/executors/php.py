# Copyright © 2026 Rutgers, the State University of New Jersey. All rights reserved except as defined by the Rutgers Non-Commercial License, included with this software.
import os
import logging
import base64
from datetime import datetime
from typing import Optional, List

from .base import Executor, NotebookExecutor, ExecutionResult

logger = logging.getLogger(__name__)

class PHPExecutor(Executor):
    LANGUAGE = "php"
    EXECUTABLE_EXTENSIONS = [".php"]
    TEMPLATE = "template.php"
    DOCKER_IMAGE = "php:8.2-cli"
    BUILD_CACHE_DIRECTORIES = ['/tmp/composer-cache']
    
    INIT_DOCKER_VOLUME = {
         "codepost-composer-cache": {
             "bind": "/tmp/composer-cache",
             "mode": "rw"
         }
    }
    
    @classmethod
    def is_executable(cls, file_name: Optional[str] = None, extension: Optional[str] = None, code: Optional[str] = None) -> bool:
        if file_name is not None:
            extension = os.path.splitext(file_name)[1]
        if extension and extension.lower() in cls.EXECUTABLE_EXTENSIONS:
            return True
        return False
        
    def _detect_imports(self, code: str) -> List[str]:
        return []

        return []

    def _get_code_template(self, code: str, packages_to_install: List[str]) -> Optional[str]:
        template = super()._get_code_template()
        if not template:
            return None
            
        # Replace packages
        # PHP array format: ['a', 'b']
        pkgs_str = "[" + ", ".join([f"'{p}'" for p in packages_to_install]) + "]"
        template = template.replace("$packages_to_install = []; // REPLACED_BY_EXECUTOR", f"$packages_to_install = {pkgs_str};")
        
        # Replace filler
        template = template.replace("# FILLER_CODE", code)
        return template

    def execute(self) -> ExecutionResult:
        timeout = self.DEFAULT_TIMEOUT
        start_time = datetime.now()
        
        if not self.file.data:
            return ExecutionResult.error("No code to execute")
        
        code = self.file.data
        packages = self._detect_imports(code) 
        
        template = self._get_code_template(code, packages)
        if not template:
            return ExecutionResult.error("Failed to get code template")

        # The rendered template is staged into /work as a self-deleting runner
        # file (never through argv — Linux caps one argument at 128 KiB).
        runner_filename = ".codepost_runner.php"
        command = ["php", f"/work/{runner_filename}"]
        
        container = self.get_container(
            image_name=self.image,
            command=command,
            env=self._get_docker_environment(),
            volumes=self._get_volume_mounts("" if not self.datasets else self._create_staging_directory()),
            needs_network=bool(packages)
        )
        
        if not container:
             return ExecutionResult.error("Failed to create container")
             
        self.add_additional_files(container)
        self._put_file(container, '/work', runner_filename, template)

        try:
            container.start()
            adjusted_timeout = timeout + (30 if packages else 0)
            result = container.wait(timeout=adjusted_timeout)
            stdout = container.logs(stdout=True, stderr=False).decode('utf-8', errors='replace')
            stderr = container.logs(stdout=False, stderr=True).decode('utf-8', errors='replace')
            
            template_logs = ""
            if "<<<RESULT>>>" in stderr:
                parts = stderr.split("<<<RESULT>>>")
                template_logs = parts[0]
                stderr = parts[1]
                if stderr.startswith("\n"): stderr = stderr[1:]
            
            execution_time = (datetime.now() - start_time).total_seconds()
            success = result.get('StatusCode', 1) == 0
            
            full_logs = self.executor_logs
            if template_logs:
                full_logs += "\n--- Template Logs ---\n" + template_logs
            
            return ExecutionResult(
                success=success,
                stdout=stdout,
                stderr=stderr,
                err=None if success else f"Exit Code: {result.get('StatusCode')}",
                execution_time=execution_time,
                system_logs=full_logs
            )
        except Exception as e:
            container.kill()
            return ExecutionResult.error(f"Execution failed: {e}")
        finally:
            container.remove()

class PHPNotebookExecutor(NotebookExecutor):
    LANGUAGE = "php"
    TEMPLATE = "notebook_template.php"
    DOCKER_IMAGE = "php:8.2-cli"
    EXECUTABLE_EXTENSIONS = ['.ipynb']
    EXECUTION_COMMAND = ["php"]
    RUNNER_FILENAME = ".codepost_runner.php"
    
    @classmethod
    def is_executable(cls, file_name: Optional[str] = None, extension: Optional[str] = None, code: Optional[str] = None) -> bool:
        if file_name is not None:
            extension = os.path.splitext(file_name)[1]

        if extension is None or extension.lower() not in cls.EXECUTABLE_EXTENSIONS:
            return False

        return cls.notebook_matches_language(code, ['php'])

    def _get_code_template(self, code: str, packages_to_install: List[str], test_code: str = "") -> Optional[str]:
        template = super()._get_code_template()
        if not template:
            return None
        template = template.replace('{cells_b64}', code)
        # Inject test code if provided
        test_code_b64 = base64.b64encode(test_code.encode('utf-8')).decode('utf-8') if test_code else ""
        template = template.replace('{test_code_b64}', test_code_b64)
        return template
