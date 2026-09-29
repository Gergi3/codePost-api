# Copyright © 2026 Rutgers, the State University of New Jersey. All rights reserved except as defined by the Rutgers Non-Commercial License, included with this software.
import os
import logging
from datetime import datetime
import base64
from typing import List, Optional

from .base import Executor, NotebookExecutor, ExecutionResult

logger = logging.getLogger(__name__)

class RExecutor(Executor):
    """
    Executor for R scripts.
    """
    LANGUAGE = "r"
    TEMPLATE = "template.r"
    DOCKER_IMAGE = "r-base:latest"
    EXECUTABLE_EXTENSIONS = [".r", ".R"]
    EXECUTION_COMMAND = ["Rscript", "-e"]
    BUILD_CACHE_DIRECTORIES = ['/tmp/R/library']
    
    INIT_DOCKER_VOLUME = {
         "codepost-r-library-v2": {
             "bind": "/tmp/R/library",
             "mode": "rw"
         }
    }

    def _get_docker_environment(self):
        env = super()._get_docker_environment()
        env['R_LIBS_USER'] = '/tmp/R/library'
        # Set tmpdir to avoid Rtmp issues
        env['TMPDIR'] = '~/.tmp'
        return env

    
    @classmethod
    def is_executable(cls, file_name: Optional[str] = None, extension: Optional[str] = None, code: Optional[str] = None) -> bool:
        logger.info(f"Checking if {file_name} is executable")   

        if file_name is not None:
            extension = os.path.splitext(file_name)[1]
        if extension is not None and extension.lower() in cls.EXECUTABLE_EXTENSIONS:
            return True
        return False


    def _detect_imports(self, code: str) -> List[str]:
        # Simple detection for library(package)
        import re
        packages = []
        matches = re.findall(r'library\(([^)]+)\)', code)
        for m in matches:
            # Remove quotes if present
            pkg = m.strip().strip("'").strip('"')
            packages.append(pkg)
        return list(set(packages))

    def _get_code_template(self, code: str, packages_to_install: List[str], test_code: str = "") -> Optional[str]:
        template = super()._get_code_template()
        if not template:
            return None
        
        # Replace packages list if needed (template.r should have a placeholder if we support it)
        # But template.r currently doesn't have package installation logic, only filler code!
        # So we just inject code.
        
        template = template.replace("#{FILLER_CODE}", code)
        template = template.replace("#{TEST_CODE}", test_code or "")
        return template

    def execute(self) -> ExecutionResult:
        """Execute R code in Docker container"""
        timeout = self.DEFAULT_TIMEOUT
        start_time = datetime.now()
        
        if not self.file.data:
            return ExecutionResult.error("No code to execute")
        
        code = self.file.data
        packages_to_install = self._detect_imports(code) 
        
        template = self._get_code_template(code, packages_to_install, self.test_code or "")
        if not template:
            return ExecutionResult.error("Failed to get code template")

        client = self._get_docker_client()
        if not client:
            return ExecutionResult.error("Docker is not available")

        # Reuse volume/env logic
        docker_env = self._get_docker_environment()
        
        # Strategy:
        # 1. Stage user code as 'student.R' (template.r sources it; enables source(print.eval=TRUE))
        # 2. Stage the wrapper template as '.codepost_runner.R' (self-deleting)
        # 3. Install packages
        # 4. Run wrapper
        # Both files go in via _put_file before start — never through argv
        # (Linux caps one argument at 128 KiB).

        student_filename = "student.R"
        runner_filename = ".codepost_runner.R"
        # Template should be raw (no code injection needed as it sources student.R)
        # _get_code_template's replace("#{FILLER_CODE}", "") is a no-op for template.r.
        template_content = self._get_code_template("", packages_to_install, self.test_code or "")
        if not template_content:
             return ExecutionResult.error("Failed to get code template")

        run_cmd = f"Rscript /work/{runner_filename}"
        if packages_to_install:
            # Construct R installation script
            pkgs_list = ", ".join([f"'{p}'" for p in packages_to_install])
            install_script = (
                f"options(repos = c(CRAN = 'https://cloud.r-project.org')); "
                f"pkgs <- c({pkgs_list}); "
                f"new_pkgs <- pkgs[!(pkgs %in% installed.packages()[,'Package'])]; "
                f"if(length(new_pkgs)) install.packages(new_pkgs)"
            )
            command = ["sh", "-c", f"Rscript -e \"{install_script}\" && {run_cmd}"]
        else:
            command = ["Rscript", f"/work/{runner_filename}"]
        
        # Volumes
        import shutil
        if self.datasets:
            temp_staging_dir = self._create_staging_directory()
        else:
            temp_staging_dir = ""
            
        volumes = self._get_volume_mounts(temp_staging_dir if self.datasets else "")

        container = self.get_container(
            image_name=self.image,
            command=command,
            env=docker_env,
            volumes=volumes,
            needs_network=len(packages_to_install) > 0
        )
        
        if not container:
            if self.datasets:
                shutil.rmtree(temp_staging_dir, ignore_errors=True)
            return ExecutionResult.error("Failed to create Docker container")
            
        self.add_additional_files(container)
        self._put_file(container, '/work', student_filename, code)
        self._put_file(container, '/work', runner_filename, template_content)

        try:
            container.start()
            adjusted_timeout = timeout + (30 * len(packages_to_install))
            result = container.wait(timeout=adjusted_timeout)
            
            # Logs - Standard capture
            stdout = container.logs(stdout=True, stderr=False).decode('utf-8', errors='replace')
            stderr = container.logs(stdout=False, stderr=True).decode('utf-8', errors='replace')
            
            # Parse plots
            import re
            # Use DOTALL to match newlines inside the base64 string
            img_regex = re.compile(r'<<<CODEPOST_PLOT:\s*(.*?)\s*>>>', re.DOTALL)
            images = []
            
            def replace_and_capture(match):
                # Capture and strip whitespace/newlines from the base64 data
                images.append(match.group(1).strip().replace('\n', '').replace('\r', ''))
                return "" # Remove from stdout
            
            stdout = img_regex.sub(replace_and_capture, stdout)

            # Parse standardized test markers from stdout/stderr and remove them from output.
            stdout, stderr, test_results = self.parse_test_results(stdout, stderr)
            
            output_data = {}
            if images:
                # Clean up base64 strings and store all of them
                cleaned_images = [img.strip().replace('\n', '').replace('\r', '') for img in images]
                output_data['image/png'] = cleaned_images[-1] # Primary image (last one)
                output_data['images'] = cleaned_images # All images

            success = result.get('StatusCode', 1) == 0
            
            end_time = datetime.now()
            execution_time = (end_time - start_time).total_seconds()
            
            return ExecutionResult(
                success=success,
                stdout=stdout,
                stderr=stderr,
                err=None if success else f"Exit Code: {result.get('StatusCode')}",
                execution_time=execution_time,
                output_data=output_data,
                system_logs=None,
                tests=test_results,
            )

        except Exception as e:
            container.kill()
            return ExecutionResult.error(f"Execution failed: {e}")
        finally:
            container.remove()
            if self.datasets:
                shutil.rmtree(temp_staging_dir, ignore_errors=True)


class RNotebookExecutor(NotebookExecutor):
    """
    Executor for R Jupyter notebooks.
    
    Uses Rscript to execute the R notebook template.
    """
    LANGUAGE = "r"
    TEMPLATE = "notebook_template.r"
    DOCKER_IMAGE = "r-base:latest"
    EXECUTABLE_EXTENSIONS = ['.ipynb']
    EXECUTABLE_EXTENSIONS = ['.ipynb']
    EXECUTION_COMMAND = ["Rscript"]
    RUNNER_FILENAME = ".codepost_runner.R"
    
    INIT_DOCKER_VOLUME = {
         "codepost-r-library": {
             "bind": "/tmp/R/library",
             "mode": "rw"
         }
    }
    
    @classmethod
    def is_executable(cls, file_name: Optional[str] = None, extension: Optional[str] = None, code: Optional[str] = None) -> bool:
        """
        Check if this is an R notebook.
        
        An R notebook must have an R kernel and .ipynb extension.
        """
        if file_name is not None:
            extension = os.path.splitext(file_name)[1]

        if extension is None or extension.lower() not in cls.EXECUTABLE_EXTENSIONS:
            return False

        return cls.notebook_matches_language(code, ['r', 'ir'])

    def _get_code_template(self, code: str, packages_to_install: List[str], test_code: str = "") -> Optional[str]:
        """Get the R notebook template with cells substituted."""
        template = super()._get_code_template()
        if not template:
            return None
        
        # Replace packages list if needed
        if packages_to_install:
            packages_str = ', '.join(f"'{p}'" for p in packages_to_install)
            template = template.replace(
                "packages_to_install <- list()", 
                f"packages_to_install <- list({packages_str})"
            )

        template = template.replace('{cells_b64}', code)
        
        # Inject test code if provided
        test_code_b64 = base64.b64encode(test_code.encode('utf-8')).decode('utf-8') if test_code else ""
        template = template.replace('{test_code_b64}', test_code_b64)
        return template

    def _needs_network(self, packages_to_install: List[str]) -> bool:
        # R always needs network for base64enc/jsonlite packages
        return True
