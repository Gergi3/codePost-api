# Copyright © 2026 Rutgers, the State University of New Jersey. All rights reserved except as defined by the Rutgers Non-Commercial License, included with this software.
import os
import re
import logging
import base64
import json
from datetime import datetime
from typing import Dict, Optional, List

from .base import Executor, NotebookExecutor, ExecutionResult

logger = logging.getLogger(__name__)

# `%timeit …` / `%%file …` are magics; `% 3;` (a wrapped modulo expression) is C++.
_CLING_MAGIC_RE = re.compile(r'%+[A-Za-z]')


def _strip_cling_magics(cell_source: str) -> str:
    """
    xeus-cling magics (`%timeit …`, `%%file …`) are not C++: comment them out so
    the concatenated notebook still compiles. Line numbers are preserved.
    """
    lines = cell_source.split('\n')
    first = next((i for i, l in enumerate(lines) if l.strip()), None)
    if first is not None and lines[first].lstrip().startswith('%%') and _CLING_MAGIC_RE.match(lines[first].lstrip()):
        if lines[first].split()[0] != '%%timeit':
            return '\n'.join('// [codepost] skipped cell magic: ' + l for l in lines)
    return '\n'.join(
        '// [codepost] ignored magic: ' + l.strip() if _CLING_MAGIC_RE.match(l.lstrip()) else l
        for l in lines
    )

class CPPExecutor(Executor):
    LANGUAGE = "c_cpp"
    EXECUTABLE_EXTENSIONS = [".c", ".cpp", ".cc"]
    TEMPLATE = "template.cpp"
    DOCKER_IMAGE = "gcc:13"
    
    @classmethod
    def is_executable(cls, file_name: Optional[str] = None, extension: Optional[str] = None, code: Optional[str] = None) -> bool:
        if file_name is not None:
            extension = os.path.splitext(file_name)[1]
        if extension and extension.lower() in cls.EXECUTABLE_EXTENSIONS:
            return True
        return False
        
    def _detect_imports(self, code: str) -> List[str]:
        return []

    def _get_code_template(self, test_code: str = "") -> Optional[str]:
        # If test_code is provided, we use the test runner template
        if test_code:
            template_file = self.TEMPLATE
            template_path = os.path.join(os.path.dirname(__file__), "../templates", template_file)
            try:
                with open(template_path, 'r') as f:
                    template = f.read()
                return template.replace("#{TEST_CODE}", test_code)
            except Exception as e:
                self.log(f"Failed to load template: {e}", "error")
                return None
                
        # Existing logic for normal execution (snippets vs main)
        # We don't want to use standard template.cpp for testing if we are using it for harness.
        # Actually, the file on disk is now the harness.
        # So for normal execution, we should NOT use this template anymore unless we want to wrap snippets.
        # But normal execution usually just runs provided code or minimal wrapper.
        return None

    def execute(self) -> ExecutionResult:
        _timeout = self.DEFAULT_TIMEOUT
        start_time = datetime.now()
        
        if not self.file.data:
            return ExecutionResult.error("No code to execute")
        
        code = self.file.data
        
        # Sources are staged into /work via _put_file before start (never
        # through argv — Linux caps one argument at 128 KiB).
        if self.test_code:
            # --- Testing Mode ---
            template = self._get_code_template(self.test_code)
            if not template:
                return ExecutionResult.error("Failed to load C++ test template")

            # Stage student code as source.cpp and the harness as runner.cpp.
            # Compile with -Dmain=__student_main to satisfy linker if student provided main.
            # The harness source is removed as soon as it is compiled.

            filename = "source.cpp"
            runner_files = {filename: code, "runner.cpp": template}

            cmd_str = (
                f"g++ -Dmain=__student_main -c {filename} -o source.o && "
                f"g++ -c runner.cpp -o runner.o && "
                f"rm -f runner.cpp && "
                f"g++ source.o runner.o -o program && "
                f"./program"
            )

        else:
             # --- Standard Execution Mode ---
            ext = self.file.extension or ".cpp"
            if ext and not ext.startswith("."):
                ext = f".{ext}"
            filename = f"source{ext}"
            output_bin = "program"
            compiler = "gcc" if ext == ".c" else "g++"
            runner_files = {filename: code}

            # Simple compile and run
            cmd_str = f"{compiler} {filename} -o {output_bin} && ./{output_bin}"

        command = ["sh", "-c", cmd_str]
        
        container = self.get_container(
            image_name=self.image,
            command=command,
            env=self._get_docker_environment(),
            volumes=self._get_volume_mounts("" if not self.datasets else self._create_staging_directory()),
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

            # Parse Test Results
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

class CPPNotebookExecutor(NotebookExecutor):
    LANGUAGE = "c/c++"
    TEMPLATE = "notebook_template.cpp"
    DOCKER_IMAGE = "gcc:latest"
    EXECUTABLE_EXTENSIONS = ['.ipynb']
    # Execution command generated dynamically via _get_execution_command
    
    @classmethod
    def is_executable(cls, file_name: Optional[str] = None, extension: Optional[str] = None, code: Optional[str] = None) -> bool:
        if file_name is not None:
            extension = os.path.splitext(file_name)[1]

        if extension is None or extension.lower() not in cls.EXECUTABLE_EXTENSIONS:
            return False

        return cls.notebook_matches_language(code, ['cpp', 'c++', 'c/c++', 'cling', 'xeus-cling'])
        
    def _get_code_template(self, code: str, packages_to_install: List[str], test_code: str = "") -> Optional[str]:
        template = super()._get_code_template()
        if not template:
            return None
        
        # Parse notebook from base64 code
        try:
             json_str = base64.b64decode(code).decode('utf-8')
             cells = json.loads(json_str)
             # If passed straight list of cells
             if isinstance(cells, dict):
                 cells = cells.get('cells', [])
             
             source_code = ""
             for i, cell in enumerate(cells):
                 if not isinstance(cell, dict):
                      logger.error(f"Cell {i} is not a dict: {type(cell)} {cell}")
                      continue
                 
                 # Prepare_notebook uses 'type', nbformat uses 'cell_type'
                 cell_type = cell.get('type') or cell.get('cell_type')
                 if not cell_type:
                      logger.error(f"Cell {i} missing type: {cell}")
                      continue
                      
                 if cell_type == 'code':
                     cell_source = "".join(cell.get('source', [])) if isinstance(cell.get('source'), list) else cell.get('source', "")
                     cell_source = _strip_cling_magics(cell_source)
                     source_code += f"\n// Cell\n{cell_source}\n"
             
             if not source_code:
                 logger.warning("No code cells found in notebook")
                 
             if test_code:
                 # If testing, return just the source code. _get_execution_command will handle the rest.
                 return source_code
             
             # Standard execution: wrap in notebook_template.cpp
             template = template.replace("// FILLER_CODE", source_code)
             return template
        except Exception as e:
             logger.error(f"Failed to parse notebook: {e} | Code len: {len(code)}")
             return None

    def _get_runner_files(self, template: str) -> Dict[str, str]:
        """
        Files staged into /work before start (never through argv).

        Test mode: `template` is the raw student source -> student.cpp, plus the
        test harness (template.cpp with the test code injected) -> runner.cpp.
        Standard mode: the rendered notebook template -> notebook.cpp.
        """
        if not self.test_code:
            return {"notebook.cpp": template}

        files = {"student.cpp": template}
        harness_path = os.path.join(os.path.dirname(__file__), "../templates", "template.cpp")
        try:
            with open(harness_path, 'r') as f:
                 harness_template = f.read()
            files["runner.cpp"] = harness_template.replace("#{TEST_CODE}", self.test_code)
        except Exception as e:
             # g++ then fails visibly on the missing runner.cpp
             logger.error(f"Failed to load harness: {e}")
        return files

    def _get_execution_command(self) -> List[str]:
        if self.test_code:
            # --- Testing Mode ---
            # 1. Compile student.cpp with -Dmain=__student_main (rename main)
            # 2. Compile harness, then drop its source
            # 3. Link and run
            cmd_str = (
                f"g++ -Dmain=__student_main -c student.cpp -o student.o && "
                f"g++ -c runner.cpp -o runner.o && "
                f"rm -f runner.cpp && "
                f"g++ student.o runner.o -o program && "
                f"./program"
            )
            return ["sh", "-c", cmd_str]

        else:
            # --- Standard Mode ---
            return ["sh", "-c", "g++ -o notebook notebook.cpp && ./notebook"]
