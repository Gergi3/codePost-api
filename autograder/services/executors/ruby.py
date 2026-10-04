# Copyright © 2026 Rutgers, the State University of New Jersey. All rights reserved except as defined by the Rutgers Non-Commercial License, included with this software.
import os
import logging
import base64
from datetime import datetime
from typing import Optional, List

from .base import Executor, NotebookExecutor, ExecutionResult

logger = logging.getLogger(__name__)

class RubyExecutor(Executor):
    LANGUAGE = "ruby"
    EXECUTABLE_EXTENSIONS = [".rb"]
    TEMPLATE = "template.rb"
    DOCKER_IMAGE = "ruby:3.2-slim"
    BUILD_CACHE_DIRECTORIES = ['/tmp/gems']

    INIT_DOCKER_VOLUME = {
         "codepost-gem-cache": {
             "bind": "/tmp/gems",
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
        # Minimal gem detection?
        return []

        return []

    def _get_code_template(self, code: str, packages_to_install: List[str]) -> Optional[str]:
        template = super()._get_code_template()
        if not template:
            return None
            
        # Replace packages
        # Ruby array format: ['a', 'b']
        pkgs_str = "[" + ", ".join([f"'{p}'" for p in packages_to_install]) + "]"
        template = template.replace("packages_to_install = [] # REPLACED_BY_EXECUTOR", f"packages_to_install = {pkgs_str}")
        
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
        
        # Use simple template or wrap command
        # Since imports are currently empty, we skip complex template
        
        # We can just run the code directly via file
        # But for consistency via our interface, we just use a filename
        filename = "script.rb"
        
        # Staged into /work via _put_file before start (never through argv —
        # Linux caps one argument at 128 KiB)
        command = ["ruby", f"/work/{filename}"]
        
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
        self._put_file(container, '/work', filename, code)

        try:
            container.start()
            adjusted_timeout = timeout
            result = container.wait(timeout=adjusted_timeout)
            
            stdout = container.logs(stdout=True, stderr=False).decode('utf-8', errors='replace')
            stderr = container.logs(stdout=False, stderr=True).decode('utf-8', errors='replace')
            
            execution_time = (datetime.now() - start_time).total_seconds()
            success = result.get('StatusCode', 1) == 0
            
            return ExecutionResult(
                success=success,
                stdout=stdout,
                stderr=stderr,
                err=None if success else f"Exit Code: {result.get('StatusCode')}",
                execution_time=execution_time,
                system_logs=None
            )
        except Exception as e:
            container.kill()
            return ExecutionResult.error(f"Execution failed: {e}")
        finally:
            container.remove()


class RubyNotebookExecutor(NotebookExecutor):
    LANGUAGE = "ruby"
    TEMPLATE = "notebook_template.rb"
    DOCKER_IMAGE = "ruby:3.2-slim"
    EXECUTABLE_EXTENSIONS = ['.ipynb']
    EXECUTION_COMMAND = ["ruby"]
    RUNNER_FILENAME = ".codepost_runner.rb"
    
    @classmethod
    def is_executable(cls, file_name: Optional[str] = None, extension: Optional[str] = None, code: Optional[str] = None) -> bool:
        if file_name is not None:
            extension = os.path.splitext(file_name)[1]

        if extension is None or extension.lower() not in cls.EXECUTABLE_EXTENSIONS:
            return False

        return cls.notebook_matches_language(code, ['ruby', 'iruby'])

    def _get_code_template(self, code: str, packages_to_install: List[str], test_code: str = "") -> Optional[str]:
        template = super()._get_code_template()
        if not template:
            return None
        template = template.replace('{cells_b64}', code)
        
        # Inject test code
        if test_code:
            test_code_b64 = base64.b64encode(test_code.encode('utf-8')).decode('ascii')
            template = template.replace('{test_code_b64}', test_code_b64)
        else:
            template = template.replace('{test_code_b64}', '')
        
        return template

