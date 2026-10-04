# Copyright © 2026 Rutgers, the State University of New Jersey. All rights reserved except as defined by the Rutgers Non-Commercial License, included with this software.
import os
import logging
import base64
from datetime import datetime
from typing import Optional, List

from .base import Executor, NotebookExecutor, ExecutionResult

logger = logging.getLogger(__name__)

class NodeExecutor(Executor):
    LANGUAGE = "node-20"
    EXECUTABLE_EXTENSIONS = [".js", ".ts", ".mjs", ".cjs"]
    TEMPLATE = "template.js"
    DOCKER_IMAGE = "node:20-slim"
    
    NPM_CACHE_VOLUME_NAME = "codepost-npm-cache"
    
    INIT_DOCKER_VOLUME = {
        NPM_CACHE_VOLUME_NAME: {
            "bind": "/tmp/npm-cache",
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
        import re
        packages_to_install = set()
        
        try:
            # Regex for require() or import
            require_matches = re.findall(r'require\([\'"](.+?)[\'"]\)', code)
            import_matches = re.findall(r'import\s+.*?from\s+[\'"](.+?)[\'"]', code)
            candidates = set(require_matches + import_matches)
            
            for pkg in candidates:
                # Basic npm package name validation (no path)
                if not pkg.startswith('.') and not pkg.startswith('/'):
                     # Check if in whitelist
                     if pkg in self.NPM_PACKAGE_WHITELIST:
                         packages_to_install.add(pkg)
        except Exception:
            pass
            
        return list(packages_to_install)

            
        return list(packages_to_install)

    def _get_code_template(self, code: str, packages_to_install: List[str], test_code: str = "") -> Optional[str]:
        template = super()._get_code_template()
        if not template:
            return None
            
        # Replace packages
        import json
        pkgs_json = json.dumps(packages_to_install)
        template = template.replace("const packages_to_install = []; // REPLACED_BY_EXECUTOR", f"const packages_to_install = {pkgs_json};")
        
        # Replace filler
        template = template.replace("// FILLER_CODE", code)

        # Inject test code (base64) if provided
        test_code_b64 = base64.b64encode(test_code.encode('utf-8')).decode('utf-8') if test_code else ""
        template = template.replace('{test_code_b64}', test_code_b64)
        return template

    def execute(self) -> ExecutionResult:
        """Execute Node.js code in Docker container"""
        timeout = self.DEFAULT_TIMEOUT
        start_time = datetime.now()
        
        if not self.file.data:
            return ExecutionResult.error("No code to execute")
        
        code = self.file.data
        imports = self._detect_imports(code)
        
        # Get code template
        template = self._get_code_template(code, imports, self.test_code or "")
        if not template:
            return ExecutionResult.error("Failed to get code template")

        # The rendered template is staged into /work as a self-deleting runner
        # file (never through argv — Linux caps one argument at 128 KiB).
        runner_filename = ".codepost_runner.js"
        command = ["node", f"/work/{runner_filename}"]
        
        needs_network = bool(imports)
        
        container = self.get_container(
            image_name=self.image,
            command=command,
            env=self._get_docker_environment(),
            volumes=self._get_volume_mounts("" if not self.datasets else self._create_staging_directory()),
            needs_network=needs_network
        )
        
        if not container:
             return ExecutionResult.error("Failed to create container")
             
        self.add_additional_files(container)
        self._put_file(container, '/work', runner_filename, template)

        try:
            container.start()
            adjusted_timeout = timeout + (30 if imports else 0)
            result = container.wait(timeout=adjusted_timeout)
            stdout = container.logs(stdout=True, stderr=False).decode('utf-8', errors='replace')
            stderr = container.logs(stdout=False, stderr=True).decode('utf-8', errors='replace')
            
            # Parse template logs from stderr
            template_logs = ""
            if "<<<RESULT>>>" in stderr:
                parts = stderr.split("<<<RESULT>>>")
                template_logs = parts[0]
                stderr = parts[1]
                if stderr.startswith("\n"): stderr = stderr[1:]

            # Parse test results if present
            stdout, stderr, test_results = self.parse_test_results(stdout, stderr)

            execution_time = (datetime.now() - start_time).total_seconds()
            success = result.get('StatusCode', 1) == 0

            # Merge logs
            full_system_logs = self.executor_logs
            if template_logs:
                full_system_logs += "\n--- Template Logs ---\n" + template_logs

            return ExecutionResult(
                success=success,
                stdout=stdout,
                stderr=stderr,
                err=None if success else f"Exit Code: {result.get('StatusCode')}",
                execution_time=execution_time,
                system_logs=full_system_logs,
                tests=test_results
            )
        except Exception as e:
            container.kill()
            return ExecutionResult.error(f"Execution failed: {e}")
        finally:
            container.remove()

class NodeNotebookExecutor(NotebookExecutor):
    LANGUAGE = "node"
    TEMPLATE = "notebook_template.js"
    DOCKER_IMAGE = "node:20-slim"
    EXECUTABLE_EXTENSIONS = [".ipynb"]
    EXECUTION_COMMAND = ["node"]
    RUNNER_FILENAME = ".codepost_runner.js"
    BUILD_CACHE_DIRECTORIES = ['/tmp/npm-cache']
    
    @classmethod
    def is_executable(cls, file_name: Optional[str] = None, extension: Optional[str] = None, code: Optional[str] = None) -> bool:
        if file_name is not None:
            extension = os.path.splitext(file_name)[1]

        if extension is None or extension.lower() not in cls.EXECUTABLE_EXTENSIONS:
            return False

        return cls.notebook_matches_language(code, ['javascript', 'js', 'node', 'nodejs', 'typescript', 'ts'])

    def _get_code_template(self, code: str, packages_to_install: List[str], test_code: str = "") -> Optional[str]:
        template = super()._get_code_template()
        if not template:
            return None
        template = template.replace('{cells_b64}', code)
        # Inject test code if provided
        test_code_b64 = base64.b64encode(test_code.encode('utf-8')).decode('utf-8') if test_code else ""
        template = template.replace('{test_code_b64}', test_code_b64)
        return template
