# Copyright © 2026 Rutgers, the State University of New Jersey. All rights reserved except as defined by the Rutgers Non-Commercial License, included with this software.
# This is a template for running jupyter notebook code cells inside a Docker container.
# To use this template replace the placeholder {cells_b64} with a base64-encoded JSON array of cells, And packages_to_install with a list of packages to install.

# The executor stages this script as /work/.codepost_runner.py; unlink it as the
# very first statement so the harness/test code lives on disk only until Python
# has read it (Python compiles the whole file before executing line 1). Guarded
# by name so local/test runs of the template are unaffected.
import os
if os.path.basename(globals().get("__file__", "")).startswith(".codepost_runner"):
    try:
        os.unlink(__file__)
    except OSError:
        pass

# START OF PACKAGE INSTALLATION TEMPLATE
import subprocess
import sys
import os
import time
import site
import json
import traceback
import io
import contextlib
import base64
import ast
from typing import List, Dict, Any, Optional, Callable
import signal
import re
import shutil

# Set environment for pip
os.environ['PIP_ROOT_USER_ACTION'] = 'ignore'
if 'PIP_CACHE_DIR' not in os.environ:
    os.environ['PIP_CACHE_DIR'] = '/tmp/pip-cache'
os.environ['MPLBACKEND'] = 'Agg'  # For matplotlib headless

# Student-facing tracebacks: hide this template's own frames and show source
# lines for the notebook cells (they never exist on disk, so linecache has to
# be primed by hand). The instructor test script is deliberately NOT
# registered, so tracebacks never reveal its source.
import linecache
from traceback import StackSummary, TracebackException

_HIDDEN_TB_FILES = {globals().get("__file__", ""), "<string>"}


def _register_source(pseudo_file: str, source: str) -> None:
    # mtime=None entries survive linecache.checkcache()
    linecache.cache[pseudo_file] = (len(source), None, source.splitlines(True), pseudo_file)


def _format_student_traceback(exc: BaseException) -> str:
    te = TracebackException.from_exception(exc)
    node: Optional[TracebackException] = te
    while node is not None:
        node.stack = StackSummary.from_list(
            [f for f in node.stack if f.filename not in _HIDDEN_TB_FILES]
        )
        node = node.__cause__ or node.__context__
    return "".join(te.format())

MAX_CELLS = 500  # Maximum number of cells allowed to prevent abuse

def template_log(message: str, level:str) -> None:
    print(f"[{level}] {message}", file=sys.stderr)

# Packages to install
packages_to_install = []

script_start_time = time.time()

# Debug: Check if pip cache is mounted
pip_cache_path = os.environ.get('PIP_CACHE_DIR', '/tmp/pip-cache')
if os.path.exists(pip_cache_path):
    try:
        cache_size = sum(os.path.getsize(os.path.join(dirpath, filename))
                        for dirpath, dirnames, filenames in os.walk(pip_cache_path)
                        for filename in filenames)
        # template_log(f"Current cache size: {cache_size / 1024 / 1024:.2f} MB", "DEBUG")
    except Exception as e:
        template_log(f"Could not calculate cache size: {str(e)}", "DEBUG")

# Check which packages are already installed (from pre-built image or previous runs)
needs_install = []
for package in packages_to_install:
    try:
        __import__(package)
    except ImportError:
        needs_install.append(package)

failed_packages = []
if needs_install:
    template_log(f"Installing {len(needs_install)} package(s): {', '.join(needs_install)}", "INFO")
    
    # Install packages with progress feedback
    failed_packages = []
    for i, package in enumerate(needs_install, 1):
        template_log(f"[{i}/{len(needs_install)}] Installing {package}...", "INFO")
        start_time = time.time()
        
        try:
            result = subprocess.run(
                [sys.executable, '-m', 'pip', 'install', '--user', '-v', "--only-binary", ":all:", package],
                capture_output=True,
                text=True
            )
            elapsed = time.time() - start_time
            
            if result.returncode == 0:
                template_log(f"[{i}/{len(needs_install)}] ✓ {package} installed ({elapsed:.1f}s)", "INFO")
                print(f"CODEPOST_AUTO_INSTALL_SUCCESS: {package}", file=sys.stderr)
            else:
                template_log(f"Pip output: {result.stdout}\n{result.stderr}", "DEBUG")
                raise subprocess.CalledProcessError(result.returncode, result.args)
        except subprocess.CalledProcessError:
            elapsed = time.time() - start_time
            template_log(f"[{i}/{len(needs_install)}] ✗ {package} failed ({elapsed:.1f}s)", "ERROR")
            failed_packages.append(package)
        
        sys.stderr.flush()

    # Refresh sys.path
    try:
        from importlib import reload
        reload(site)
        if site.getusersitepackages() not in sys.path:
             site.addsitedir(site.getusersitepackages())
    except Exception as e:
        template_log(f"Failed to refresh sys.path: {e}", "WARNING")

sys.stderr.flush()
print("", file=sys.stderr)

# END OF PACKAGE INSTALLATION TEMPLATE

# ==========================================
# TEST FRAMEWORK IMPLEMENTATION
# ==========================================

_CAPTURED_PLOTS: List[str] = []

class TestResult:
    def __init__(self, name: str, max_score: float = 1.0, description: Optional[str] = None):
        self.name = name
        self.max_score = max_score
        self.description = description
        self.score = 0.0
        self.passed = False
        self.error: Optional[str] = None
        self.message: Optional[str] = None
        self.output: str = ""
        self.status: str = "failed" # passed, failed, error

    def to_dict(self) -> Dict[str, Any]:
        return {
            "name": self.name,
            "max_score": self.max_score,
            "description": self.description,
            "score": self.score,
            "passed": self.passed,
            "error": self.error,
            "message": self.message,
            "output": self.output,
            "status": self.status
        }
    
    def to_json(self) -> str:
        """Output result with JSON markers for frontend parsing."""
        data = self.to_dict()
        return f"<<<TEST_RESULT_JSON_START>>>{json.dumps(data)}<<<TEST_RESULT_JSON_END>>>"

class TestCase:
    def __init__(self, func: Callable, name: Optional[str] = None, points: float = 1.0, description: Optional[str] = None, timeout: Optional[int] = None):
        self.func = func
        self.name = name or func.__name__
        self.points = points
        self.description = description
        
        # Determine timeout
        global_timeouts = globals().get('CODEPOST_TEST_TIMEOUTS', {})
        # If not in globals, check the namespace where code was executed
        if not global_timeouts and 'namespace' in globals():
             global_timeouts = globals()['namespace'].get('CODEPOST_TEST_TIMEOUTS', {})
             
        if self.name in global_timeouts:
            self.timeout = global_timeouts[self.name]
        elif timeout is not None:
             self.timeout = timeout
        else:
             self.timeout = 30 # Default 30s

    def run(self) -> TestResult:
        result = TestResult(self.name, self.points, self.description)

        stdout_capture = io.StringIO()
        stderr_capture = io.StringIO()
        
        try:
            with contextlib.redirect_stdout(stdout_capture), contextlib.redirect_stderr(stderr_capture):
                # Setup timeout handler
                def handler(signum, frame):
                    raise TimeoutError(f"Test timed out after {self.timeout} seconds")
                
                signal.signal(signal.SIGALRM, handler)
                signal.alarm(self.timeout)
                
                try:
                    return_value = self.func()
                finally:
                    signal.alarm(0)

            
            # Check if return value is a number (partial credit)
            if isinstance(return_value, (int, float)):
                # Clamp score to [0, max_score]
                result.score = max(0, min(return_value, self.points))
                result.passed = result.score == self.points
                result.status = "passed" if result.passed else "partial"
            elif isinstance(return_value, (tuple, list)) and len(return_value) >= 2:
                # Handle [score, message]
                score_val = return_value[0]
                msg_val = str(return_value[1])
                
                if isinstance(score_val, (int, float)):
                    result.score = max(0, min(score_val, self.points))
                    result.passed = result.score == self.points
                    result.status = "passed" if result.passed else "partial"
                
                if msg_val:
                    result.message = msg_val
            else:
                # No return value or non-numeric = full credit
                # assume assertions passed
                result.passed = True
                result.score = self.points
                result.status = "passed"
            
            result.output = stdout_capture.getvalue()
            
        except AssertionError as e:
            result.passed = False
            result.score = 0
            result.status = "failed"
            result.error = str(e)
            result.output = stdout_capture.getvalue()
        except TimeoutError as e:
            result.passed = False
            result.score = 0
            result.status = "error"
            result.error = str(e)
            result.message = "Test timed out"
            result.output = stdout_capture.getvalue()
        except Exception as e:
            result.passed = False
            result.score = 0
            result.status = "error"
            result.error = f"{type(e).__name__}: {str(e)}\n{_format_student_traceback(e)}"
            result.output = stdout_capture.getvalue()


            
        return result

class TestRunner:
    _instance = None
    
    def __init__(self):
        self.tests: List[TestCase] = []
        self.results: List[TestResult] = []
    
    @classmethod
    def get_instance(cls):
        if not cls._instance:
            cls._instance = TestRunner()
        return cls._instance
    
    def add_test(self, test: TestCase):
        self.tests.append(test)
        
    def run_all(self) -> List[Dict[str, Any]]:
        for test in self.tests:
            result = test.run()
            self.results.append(result)
            # Output JSON markers for frontend to parse (like template.py)
            print(result.to_json(), file=sys.stderr)
            sys.stderr.flush()
        return [r.to_dict() for r in self.results]

def test(name: Optional[str] = None, points: float = 1.0, description: Optional[str] = None, timeout: Optional[int] = None, hidden: bool = False, objectives: Optional[List[str]] = None):
    def decorator(func):
        test_case = TestCase(func, name=name, points=points, description=description, timeout=timeout)
        TestRunner.get_instance().add_test(test_case)
        return func
    return decorator


def assert_plots_generated(count: int = 1):
    if len(_CAPTURED_PLOTS) < count:
        raise AssertionError(f"Expected {count} plots, but found {len(_CAPTURED_PLOTS)}")

def get_plots() -> List[str]:
    return _CAPTURED_PLOTS

# ==========================================
# END TEST FRAMEWORK
# ==========================================


from contextlib import redirect_stdout, redirect_stderr

# Change to /work directory
if os.path.exists('/work'):
    os.chdir('/work')
    template_log(f"Changed working directory to /work", "DEBUG")
else:
    template_log(f"/work does not exist, staying in {os.getcwd()}", "DEBUG")

# ==========================================
# IPYTHON MAGICS AND SHELL ESCAPES
# ==========================================
# Cells run through plain exec(), not IPython, so `%magic`, `%%cellmagic` and
# `!shell` lines are rewritten here before execution. The original cell source
# is still echoed untouched in the results.

_SHELL_TIMEOUT = 60
# `!pip install …` / `%pip install …` are skipped: packages are already installed
# from the notebook's imports, and the container usually has no network anyway.
_INSTALLER_RE = re.compile(r'^\s*(?:python3?\s+-m\s+)?(?:pip3?|conda|mamba|apt(?:-get)?)\b')
# Line magics that are meaningless outside Jupyter; dropped without a note.
_QUIET_LINE_MAGICS = {'matplotlib', 'load_ext', 'reload_ext', 'unload_ext', 'autoreload', 'config'}
# Cell magics whose body is ordinary Python: drop the magic line, run the body.
_BODY_ONLY_CELL_MAGICS = {'time', 'timeit', 'capture', 'prun'}


def display(*objs, **kwargs):
    """Stand-in for IPython.display.display, which Jupyter injects implicitly."""
    for obj in objs:
        print(repr(obj))


def _run_shell(cmd: str) -> None:
    """Run a `!cmd` line or `%%bash` body inside the sandbox and echo its output."""
    if _INSTALLER_RE.match(cmd):
        print(f"[codepost] Skipped package install command (packages are installed from the notebook's imports): {cmd.strip()}", file=sys.stderr)
        return
    try:
        proc = subprocess.run(cmd, shell=True, executable=shutil.which('bash'),
                              capture_output=True, text=True, timeout=_SHELL_TIMEOUT)
    except subprocess.TimeoutExpired:
        print(f"[codepost] Shell command timed out after {_SHELL_TIMEOUT}s: {cmd.strip()}", file=sys.stderr)
        return
    if proc.stdout:
        print(proc.stdout, end='')
    if proc.stderr:
        print(proc.stderr, end='', file=sys.stderr)


def _write_cell_file(args: str, body: str) -> None:
    """%%writefile [-a] <path>"""
    parts = args.split()
    append = '-a' in parts
    names = [p for p in parts if p != '-a']
    if not names:
        print("[codepost] %%writefile needs a filename", file=sys.stderr)
        return
    path = names[0]
    existed = os.path.exists(path)
    with open(path, 'a' if append else 'w') as f:
        f.write(body)
    print(("Appending to " if append else "Overwriting " if existed else "Writing ") + path)


def _split_magic(text: str):
    """'%%time foo bar' -> ('time', 'foo bar')"""
    parts = text.lstrip('%').split(None, 1)
    return (parts[0] if parts else ''), (parts[1].strip() if len(parts) > 1 else '')


def _rewrite_line(line: str) -> str:
    """Rewrite one `%magic` / `!shell` line into plain Python, keeping its indentation."""
    stripped = line.lstrip()
    indent = line[:len(line) - len(stripped)]
    if stripped.startswith('!'):
        return f"{indent}_codepost_shell({stripped[1:].strip()!r})"
    if stripped.startswith('%'):
        name, rest = _split_magic(stripped)
        if name in ('time', 'timeit') and rest:
            return indent + rest
        if name in ('pip', 'conda', 'mamba'):
            print(f"[codepost] Skipped package install magic (packages are installed from the notebook's imports): {stripped.strip()}", file=sys.stderr)
        elif name not in _QUIET_LINE_MAGICS:
            print(f"[codepost] Ignored IPython magic (not supported outside Jupyter): {stripped.strip()}", file=sys.stderr)
        return indent + 'pass'
    return line


def _prepare_cell_source(source: str) -> str:
    """
    Return the Python to execute for a cell. Plain Python comes back unchanged;
    IPython syntax is rewritten line by line (line numbers are preserved), and
    cells handled entirely here (%%writefile, %%bash, unsupported %%magics)
    return '' so nothing further runs.
    """
    try:
        ast.parse(source, mode='exec')
        return source
    except SyntaxError:
        pass
    lines = source.split('\n')
    first = next((i for i, l in enumerate(lines) if l.strip()), None)
    if first is not None and lines[first].lstrip().startswith('%%'):
        name, args = _split_magic(lines[first].strip())
        body = '\n'.join(lines[first + 1:])
        if name in _BODY_ONLY_CELL_MAGICS:
            lines[first] = ''
        elif name == 'writefile':
            _write_cell_file(args, body)
            return ''
        elif name in ('bash', 'sh'):
            _run_shell(body)
            return ''
        else:
            print(f"[codepost] Skipped cell: %%{name} is not supported outside Jupyter", file=sys.stderr)
            return ''
    return '\n'.join(_rewrite_line(l) for l in lines)


# Decode cells
cells_json = base64.b64decode('{cells_b64}').decode('utf-8')
cells = json.loads(cells_json)

# Shared namespace for all cells
# We inject the Test Framework into the namespace so test code can use it
namespace = {
    '__name__': '__main__', 
    '__builtins__': __builtins__,
    'TestResult': TestResult,
    'TestCase': TestCase,
    'TestRunner': TestRunner,
    'test': test,
    'assert_plots_generated': assert_plots_generated,
    'get_plots': get_plots,
    'display': display,
    '_codepost_shell': _run_shell,
    '_CAPTURED_PLOTS': _CAPTURED_PLOTS,
    'codepost_cells': cells # Provide access to all cells if needed
}

results = []
STUDENT_CODE_SYNTAX_INVALID = False
STUDENT_CODE_SYNTAX_ERROR_MSG = ""

if len(cells) > MAX_CELLS:
    template_log(f"Number of cells ({len(cells)}) exceeds maximum allowed ({MAX_CELLS})", "ERROR")
    results.append({
        'cell_type': 'markdown',
        'source': f"**Error:** Number of cells ({len(cells)}) exceeds maximum allowed ({MAX_CELLS}). Execution aborted."
    })
else:
    # Execute each code cell
    for cell_idx, cell in enumerate(cells):
        if cell['type'] == 'markdown':
            results.append({
            'cell_type': 'markdown',
            'source': cell['source'],
            'idx': cell['idx']
        })
        elif cell['type'] == 'code':
            cell_source = cell['source']
            stdout_capture = io.StringIO()
            stderr_capture = io.StringIO()
            outputs = []
            success = True
            error_msg = None
            
            try:
                with redirect_stdout(stdout_capture), redirect_stderr(stderr_capture):
                    exec_source = _prepare_cell_source(cell_source)
                    cell_filename = f"<cell {cell['idx']}>"
                    _register_source(cell_filename, exec_source)
                    try:
                        parsed = ast.parse(exec_source, mode='exec')
                        if parsed.body and isinstance(parsed.body[-1], ast.Expr):
                            setup_stmts = parsed.body[:-1]
                            last_expr = parsed.body[-1].value
                            if setup_stmts:
                                setup = ast.Module(body=setup_stmts, type_ignores=[])
                                exec(compile(setup, cell_filename, 'exec'), namespace)
                            result = eval(compile(ast.Expression(body=last_expr), cell_filename, 'eval'), namespace)
                            if result is not None:
                                print(repr(result))
                        else:
                            exec(compile(exec_source, cell_filename, 'exec'), namespace)
                    except SyntaxError:
                        exec(compile(exec_source, cell_filename, 'exec'), namespace)
            except Exception as e:
                success = False
                error_msg = str(e)
                stderr_capture.write(_format_student_traceback(e))
                if isinstance(e, (SyntaxError, IndentationError, TabError)):
                    STUDENT_CODE_SYNTAX_INVALID = True
                    STUDENT_CODE_SYNTAX_ERROR_MSG = _format_student_traceback(e)
            
            stdout_text = stdout_capture.getvalue()
            stderr_text = stderr_capture.getvalue()
            
            # Capture matplotlib plots
            try:
                if 'matplotlib' in sys.modules:
                    import matplotlib.pyplot as plt
                    figs = [plt.figure(n) for n in plt.get_fignums()]
                    if len(figs) > 0:
                        for fig in figs:
                            buf = io.BytesIO()
                            fig.savefig(buf, format='png', bbox_inches='tight', dpi=100)
                            buf.seek(0)
                            img_base64 = base64.b64encode(buf.read()).decode('utf-8')
                            buf.close()
                            
                            # Add to notebook output
                            outputs.append({
                                'output_type': 'display_data',
                                'data': {'image/png': img_base64},
                                'metadata': {}
                            })
                            
                            # Add to Test Framework capture
                            _CAPTURED_PLOTS.append(img_base64)
                        plt.close('all')
            except Exception:
                pass
            
            if stdout_text:
                outputs.append({'output_type': 'stream', 'name': 'stdout', 'text': stdout_text})
            if stderr_text:
                outputs.append({'output_type': 'stream', 'name': 'stderr', 'text': stderr_text})
            if not success:
                outputs.append({
                    'output_type': 'error',
                    'ename': 'ExecutionError',
                    'evalue': error_msg or stderr_text,
                    'traceback': [stderr_text]
                })

            results.append({
                'cell_type': 'code',
                'source': cell_source,
                'outputs': outputs,
                'execution_count': sum(1 for r in results if r.get('cell_type') == 'code') + 1,
                'idx': cell_idx
            })

# ==========================================
# RUN TEST SCRIPT
# ==========================================
test_results = []

try:
    test_code_b64 = "{test_code_b64}"
    test_code = ""
    if test_code_b64:
        test_code = base64.b64decode(test_code_b64).decode('utf-8')

    if test_code.strip():
        template_log("Running injected test script...", "INFO")
        template_log(f"SCRIPT_DEBUG: {test_code[:500]}", "DEBUG")
        # Execute test code in the SAME namespace as the notebook cells
        exec(test_code, namespace)
        
        # Run tests if any were registered
        runner = TestRunner.get_instance()
        
        # Filter tests if a specific test function is requested
        target_test_function = """#{TARGET_TEST_FUNCTION}"""
        if target_test_function and target_test_function.strip() and not target_test_function.startswith("#{"):
             target = target_test_function.strip()
             # Filter based on function name or test name
             runner.tests = [t for t in runner.tests if t.func.__name__ == target or t.name == target]

        if runner.tests:
            # Run all registered tests
            test_names = [t.name for t in runner.tests]
            template_log(f"Running {len(test_names)} Tests: {', '.join(test_names)}", "INFO")
            test_results = runner.run_all()
            template_log(f"Executed {len(test_results)} tests", "INFO")
        elif target_test_function and target_test_function.strip() and not target_test_function.startswith("#{"):
            template_log(f"No tests matched the requested function: {target_test_function}", "WARNING")
        else:
            template_log(f"No tests registered to run. (Target: '{target_test_function}')", "INFO")
except Exception as e:
    template_log(f"Test Script Error: {e}", "ERROR")
    # Add a synthetic error test result
    test_results.append({
        "name": "Test Script Execution",
        "passed": False,
        "score": 0,
        "max_score": 0,
        "error": f"Failed to run test script: {str(e)}\n{traceback.format_exc()}",
        "status": "error",
        "message": "",
        "output": "",
        "description": ""
    })

# Output results as JSON
end_time = time.time()
execution_time = end_time - script_start_time

# Construct final JSON
final_result = {
    "success": len(failed_packages) == 0,
    "stdout": "", 
    "stderr": "", 
    "error": None,
    "execution_time": execution_time,
    "output_data": {
        "cells": results
    },
    "tests": test_results
}

print('<<<RESULTS_START>>>')
print(json.dumps(final_result))
print('<<<RESULTS_END>>>')