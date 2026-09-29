# Autograder Template System

## Overview

The autograder executes student code in isolated Docker containers, runs instructor-defined test suites against it, and records structured results. The entire flow is:

```
Submission → Celery Task → Executor (Docker) → Template Execution → Test Parsing → SubmissionTest (DB)
```

Each supported language has a **template file** in this directory that serves as a self-contained execution harness. The template:

1. Installs any required packages
2. Defines a test framework (`@test` decorator, `test()` function, macros, etc.)
3. Injects the student's code (base64-encoded)
4. Injects the instructor's test script
5. Runs tests and emits structured JSON results between known markers

---

## Architecture

### Key Components

| Component              | Path                                        | Role                                                                   |
| ---------------------- | ------------------------------------------- | ---------------------------------------------------------------------- |
| **Templates**          | `autograder/services/templates/`            | Language-specific execution harnesses (this directory)                 |
| **Executors**          | `autograder/services/executors/`            | Docker container management, one subclass per language                 |
| **Builder**            | `autograder/services/builder.py`            | Builds custom Docker images from `buildSpecs.json`                     |
| **TestService**        | `autograder/services/TestService.py`        | Orchestrates test execution and result persistence                     |
| **TestParsingService** | `autograder/services/TestParsingService.py` | Parses test scripts to extract test definitions (without running them) |
| **Autodetector**       | `autograder/services/autodetector.py`       | Detects language from file extensions                                  |
| **Converger**          | `autograder/services/converger.py`          | Auto-heals missing dependencies across submissions                     |
| **Image Manager**      | `autograder/services/image_manager.py`      | Versioned Docker image lifecycle                                       |
| **Build Specs**        | `autograder/testUtils/buildSpecs.json`      | Base Docker images and install commands per language                   |

### Execution Flow (e.g., Python)

1. `PythonExecutor.execute()` is called with a submission file + test category
2. Detects imports via `FileHandler.get_requirements()` (filters out local modules)
3. Loads `template.py`, substitutes placeholders:
    - `packages_to_install = [...]` — detected/required packages
    - `#{FILLER_CODE}` — base64-encoded student code
    - `#{TEST_CODE}` — base64-encoded instructor test script
    - `#{TARGET_TEST_FUNCTION}` — specific test function to run (or empty for all)
4. Creates Docker container with resource limits and security constraints. The command is short (`python /work/.codepost_runner.py`) — the rendered template is **never** passed through argv, because Linux caps a single argument at 128 KiB (`MAX_ARG_STRLEN`) and a large notebook would fail at start with `argument list too long`.
5. Injects additional files (other submission files, assignment files, datasets) via tar, then stages the rendered template as `/work/.codepost_runner.py` with `Executor._put_file` (`put_archive` into the container's writable layer — never the image). The template unlinks itself as its first statement, so it is on disk only until the interpreter has read it. Compiled languages (Java, C++) `rm` their runner sources right after compiling.
6. Starts container, waits with timeout
7. Captures stdout/stderr, demultiplexes Docker stream
8. Parses output markers:
    - `<<<RESULT>>>` — separates template system logs from student output
    - `<<<TEST_RESULT_JSON_START>>>...<<<TEST_RESULT_JSON_END>>>` — test results
    - `<<<CODEPOST_PLOT:{base64_data}>>>` — captured plot images
9. Returns `ExecutionResult` with stdout, stderr, tests, images, system_logs

### Container Security

Every execution container runs with:

- `security_opt=["no-new-privileges"]`
- `cap_drop=["ALL"]`
- Network disabled (unless `allowNetworkAccess=True`)
- tmpfs for `/tmp`
- Non-root `codepost` user
- Resource limits: 1GB memory, 500 PIDs, 100k CPU quota
- Max output: 1MB stdout/stderr, 10MB file size

---

## Database Schema

### `Environment` (one per Assignment)

| Field                   | Type                  | Description                                                                                                                                                        |
| ----------------------- | --------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------ |
| `assignment`            | OneToOne → Assignment | The assignment this environment belongs to                                                                                                                         |
| `language`              | CharField             | One of: `python-3.12`, `python-3.11`, `python-3.10`, `python-3.7`, `python-2.7`, `java`, `java-17`, `java-11`, `c/c++`, `node-20`, `node-18`, `r-4`, `ruby`, `php` |
| `buildType`             | CharField             | `default`, `alpine`, `ubuntu`, `windows`                                                                                                                           |
| `dockerfile`            | TextField             | Custom Docker commands appended to base image                                                                                                                      |
| `dockerRunInstructions` | JSONField             | Extra RUN commands as a list                                                                                                                                       |
| `compileText`           | TextField             | Pre-script run before every execution                                                                                                                              |
| `requirements`          | TextField             | Package manifest content (requirements.txt, package.json, etc.)                                                                                                    |
| `allowNetworkAccess`    | Boolean               | Whether container gets network                                                                                                                                     |
| `image_name`            | CharField             | Built Docker image tag                                                                                                                                             |
| `build_status`          | Integer               | 0=Not Built, 1=Building, 2=Success, 3=Failed                                                                                                                       |
| `build_logs`            | TextField             | Output from `docker build`                                                                                                                                         |
| `auto_detect`           | Boolean               | Auto-detect language from uploaded files                                                                                                                           |
| `env_vars`              | JSONField             | `{KEY: value}` dict injected into container                                                                                                                        |
| `convergence_stats`     | JSONField             | `{module_name: count}` for auto-healing                                                                                                                            |
| `current_build_version` | Integer               | Current image version number                                                                                                                                       |
| `image_history`         | JSONField             | List of `{version, image_name, requirements, built_at, status}`                                                                                                    |

### `TestCategory` (groups related tests)

| Field            | Type            | Description                                                   |
| ---------------- | --------------- | ------------------------------------------------------------- |
| `assignment`     | FK → Assignment | Parent assignment                                             |
| `name`           | CharField(48)   | Display name                                                  |
| `testScript`     | TextField       | Instructor's test code containing `@test` decorated functions |
| `maxPoints`      | Decimal         | Maximum points for this category                              |
| `sortKey`        | Integer         | Ordering                                                      |
| `targetFileName` | CharField       | Which submission file this test targets (nullable)            |

### `TestCase` (individual test)

| Field                       | Type               | Description                                                   |
| --------------------------- | ------------------ | ------------------------------------------------------------- |
| `testCategory`              | FK → TestCategory  | Parent category                                               |
| `description`               | CharField(255)     | Human-readable description                                    |
| `type`                      | CharField          | `io`, `io_cli`, `unit`, `shell`, `file`, `external`, `script` |
| `pointsPass` / `pointsFail` | Decimal            | Points awarded on pass/fail                                   |
| `functionName`              | CharField(128)     | Maps to `@test` function name in the script                   |
| `timeout`                   | Integer            | Per-test timeout (default 30s)                                |
| `exposed`                   | Boolean            | If True, students see results on submit                       |
| `rubricItem`                | FK → RubricComment | Auto-apply rubric on failure (nullable)                       |

### `SubmissionTest` (result per submission × test)

| Field                | Type            | Description                                 |
| -------------------- | --------------- | ------------------------------------------- |
| `submission`         | FK → Submission | The student's submission                    |
| `testCase`           | FK → TestCase   | Which test was run                          |
| `passed`             | Boolean         | Did it pass?                                |
| `isError`            | Boolean         | Was there an error (vs. assertion failure)? |
| `logs`               | TextField       | Raw stdout/stderr                           |
| `results`            | JSONField       | Structured list of subtests                 |
| `score` / `maxScore` | Decimal         | Points earned/possible                      |

---

## Template Output Protocol

All templates emit test results in the same JSON format between markers:

```
<<<TEST_RESULT_JSON_START>>>
{
  "name": "test_function_name",
  "max_score": 5.0,
  "description": "Human readable description",
  "score": 5.0,
  "passed": true,
  "status": "passed",
  "error": "",
  "message": "",
  "output": "captured stdout during test"
}
<<<TEST_RESULT_JSON_END>>>
```

### Return Value Conventions (all languages)

What the test function returns determines the result:

| Return Value                             | Behavior                                     |
| ---------------------------------------- | -------------------------------------------- |
| `void` / `null` / `None`                 | Full credit (pass)                           |
| Number                                   | Partial credit (clamped to `[0, max_score]`) |
| `[score, message]` or `{score, message}` | Partial credit with explanation              |
| String                                   | Pass with message                            |
| **throw/raise AssertionError**           | "failed" status, 0 points                    |
| **throw/raise other Exception**          | "error" status, 0 points                     |
| **timeout**                              | "error" with "Test timed out" message        |

### Special Markers

| Marker                                                        | Purpose                                            |
| ------------------------------------------------------------- | -------------------------------------------------- |
| `<<<RESULT>>>`                                                | Separates template system logs from student output |
| `<<<TEST_RESULT_JSON_START>>>` / `<<<TEST_RESULT_JSON_END>>>` | Wraps each test result JSON                        |
| `<<<CODEPOST_PLOT:{base64}>>>`                                | Captured matplotlib/R plot image                   |

---

## Existing Templates

| File              | Language        | Test Syntax                                                     | Package Install                 |
| ----------------- | --------------- | --------------------------------------------------------------- | ------------------------------- |
| `template.py`     | Python          | `@test(name, points, description, timeout)` decorator           | `pip install --user` at runtime |
| `template.js`     | JavaScript/Node | `test("name", points, "desc", fn, timeout)`                     | `npm install` at runtime        |
| `template.cpp`    | C/C++           | `TEST(name, pts)`, `TEST_DESC(...)`, `TEST_TIMEOUT(...)` macros | N/A (compiled)                  |
| `TestRunner.java` | Java            | `@Test(name="...", points=5)` annotation on methods             | N/A (compiled)                  |
| `template.r`      | R               | `run_test("name", pts, "desc", function() {...})`               | `library()` detection           |
| `template.rb`     | Ruby            | `run_test("name", pts, "desc", timeout)`                        | `gem install` at runtime        |
| `template.php`    | PHP             | `Tester::test("name", pts, "desc", fn)`                         | `composer require` at runtime   |

### Notebook Templates

For `.ipynb` files, each language has a `notebook_template.*` that executes cells sequentially in a shared namespace:

| File                     | Execution Strategy                           |
| ------------------------ | -------------------------------------------- |
| `notebook_template.py`   | `exec()` per cell in shared `globals()`      |
| `notebook_template.js`   | `vm.createContext()` shared state            |
| `notebook_template.java` | JShell API for interactive evaluation        |
| `notebook_template.r`    | Evaluates in `globalenv()` with plot capture |
| `notebook_template.rb`   | `instance_eval` on shared context            |
| `notebook_template.php`  | `eval()` per cell                            |
| `notebook_template.cpp`  | Naive concatenation (no per-cell isolation)  |

---

## Adding a New Language

### Step-by-Step Checklist

#### 1. Add the language choice to the model

In `core/models.py`, add to the `Environment.language` choices tuple:

```python
language = models.CharField(max_length=25, choices=(
    ...
    ('your-lang', 'your-lang'),
), default='python-3.7')
```

Create a migration: `python manage.py makemigrations core`

#### 2. Add a build spec

In `autograder/testUtils/buildSpecs.json`, add an entry:

```json
"your-lang": {
    "install": "pkg-manager install",
    "base": "FROM your-image:tag\nRUN apt-get update && apt-get install -y bash\n",
    "useradd": "RUN useradd -m -d /home/codepost -s /bin/bash codepost",
    "manifest": "requirements.txt",
    "manifestInstall": "COPY requirements.txt /tmp/requirements.txt\nRUN your-pkg-manager install -r /tmp/requirements.txt\n"
}
```

Key fields:

- `base` — The Dockerfile FROM + system dependencies. **Must include bash.**
- `install` — The package manager command used for runtime installs
- `useradd` — Creates the non-root `codepost` user (use `useradd` for Debian, `adduser` for Alpine)
- `manifest` — The dependency file name (optional)
- `manifestInstall` — Dockerfile instructions to pre-install from manifest (optional)

#### 3. Create an execution template

Create `autograder/services/templates/template.yourlang`. The template must:

1. **Install packages** — Read `packages_to_install` and install them (set by executor)
2. **Define the test framework** — Provide a `test()` function/decorator/macro that:
    - Accepts: name, points, description, optional timeout
    - Captures stdout during execution
    - Handles all return value conventions (see table above)
    - Catches exceptions and classifies as "failed" vs "error"
    - Emits JSON between `<<<TEST_RESULT_JSON_START>>>` and `<<<TEST_RESULT_JSON_END>>>`
3. **Inject student code** — Replace `#{FILLER_CODE}` placeholder with base64-decoded student code
4. **Inject test code** — Replace `#{TEST_CODE}` placeholder with instructor's test script
5. **Handle `#{TARGET_TEST_FUNCTION}`** — If set, only run that specific test
6. **Print `<<<RESULT>>>`** before student-visible output begins

Use an existing template (e.g., `template.py` or `template.js`) as a reference.

#### 4. Create an Executor class

Create `autograder/services/executors/your_lang.py`:

```python
from autograder.services.executors.base import Executor, NotebookExecutor

class YourLangExecutor(Executor):
    TEMPLATE = "template.yourlang"
    EXECUTABLE_EXTENSIONS = [".yl"]  # file extensions this executor handles
    LANGUAGE = "your-lang"  # matches the Environment.language choice
    DOCKER_IMAGE = "yourlang:latest"

    def execute(self) -> ExecutionResult:
        template = self._get_code_template(...)
        # Short command only — never the template itself (128 KiB argv limit)
        runner_filename = ".codepost_runner.yl"
        command = ["yourlang", f"/work/{runner_filename}"]
        container = self.get_container(image_name=self.image, command=command, ...)
        self.add_additional_files(container)
        self._put_file(container, '/work', runner_filename, template)  # before start()
        container.start()
        ...

class YourLangNotebookExecutor(NotebookExecutor):
    TEMPLATE = "notebook_template.yourlang"
    EXECUTION_COMMAND = ["yourlang"]          # interpreter prefix
    RUNNER_FILENAME = ".codepost_runner.yl"   # staged into /work by the base execute()
    # Base execute() runs: EXECUTION_COMMAND + ["/work/" + RUNNER_FILENAME].
    # Override _get_runner_files()/_get_execution_command() for compiled languages.
```

Commands are either a plain argv list of short tokens or `["sh", "-c", "<short string>"]`; the pre-script and stdin wrappers join tokens with `shlex.join`, so both shapes nest safely.

Register it in `autograder/services/executors/__init__.py` so the factory can discover it.

#### 5. Add test script parsing

In `autograder/services/TestParsingService.py`, add a parser for your language's test syntax:

```python
def parse_yourlang_tests(script: str) -> list[dict]:
    """Extract test definitions from a your-lang test script."""
    # Use regex or AST parsing to find test declarations
    # Return list of {name, points, description, timeout}
```

Add a branch in `parse_test_script()` that dispatches to your parser based on language.

#### 6. Add file handler for autodetection

In `core/services/file_handlers/`, create or update a handler that maps your file extension to the language. This enables auto-detection when assignment files are uploaded.

#### 7. (Optional) Create a notebook template

If your language supports notebook execution, create `notebook_template.yourlang` that:

- Accepts cells as a JSON array (base64-encoded)
- Executes each cell in a shared context
- Captures per-cell output
- Emits results in the standard format

---

## Gotchas

### Template Gotchas

1. **Base64 encoding is critical** — Student code is base64-encoded *inside the template* to avoid shell injection and escaping issues. Your template must decode it before execution. Never pass raw student code through shell interpolation — and never put the rendered template itself into the container command: stage it with `Executor._put_file` (see Execution Flow).

2. **The `<<<RESULT>>>` marker matters** — Everything before this marker is treated as "system logs" (package install output, etc.). Everything after is "student output". If you forget this marker, all output gets mixed together.

3. **JSON markers must be exact** — `<<<TEST_RESULT_JSON_START>>>` and `<<<TEST_RESULT_JSON_END>>>` are parsed with exact string matching. Extra whitespace or newlines inside the markers will break parsing.

4. **Timeout handling is per-test AND per-container** — The template should implement per-test timeouts internally (so one test doesn't consume the entire budget). The container also has a global timeout (default 300s).

5. **stdout capture during tests** — The test framework must capture stdout while each test runs so it can be included in the `output` field. Otherwise students lose debugging output in results.

6. **Package install must not block** — If `pip install` or `npm install` fails, the template should continue and run tests anyway (they'll fail with import errors, which is more informative than a blank result).

### Executor Gotchas

7. **Cache volumes persist across runs** — Package cache volumes (`codepost-pip-cache`, `codepost-npm-cache`, etc.) are shared across all containers for that language. This speeds up installs but means corrupted caches can affect all assignments.

8. **The `codepost` user must own `/work`** — The builder verifies this. If your base image doesn't create the directory with correct ownership, execution will fail with permission errors.

9. **Alpine vs Debian** — Older language specs use Alpine (smaller but `adduser` syntax differs, musl libc can cause issues). Newer ones use Debian-slim. Prefer Debian for new languages.

10. **Network is disabled by default** — Package installation happens at the START of template execution (before network is cut). If your language needs network for imports at runtime, it won't work unless `allowNetworkAccess=True`.

### System Gotchas

11. **Auto-sync creates/deletes TestCases** — When a test script runs successfully and produces results, `TestService` automatically creates new `TestCase` records for any unknown test names and deletes stale ones. This means renaming a test function creates a new TestCase (losing history).

12. **Convergence auto-adds packages** — If 3+ submissions fail with the same missing module error, the Converger adds it to requirements and triggers a rebuild. Your template's error messages must be parseable for this to work. Add patterns to `converger.py`.

13. **`update_or_create` on Environment** — The `copy_assignment` flow uses `update_or_create` because the `AssignmentFile` `post_save` signal may have already auto-created an Environment via the Autodetector. Don't assume an Environment doesn't exist after creating an assignment.

14. **Image versioning** — Each build increments `current_build_version`. Max 3 versions are kept. If a convergence update breaks things, the system can roll back to a previous version.

15. **`compileText` runs before everything** — The Environment's `compileText` field is staged as `/work/.pre_script.sh` and the command becomes `sh ./.pre_script.sh && rm .pre_script.sh && <command>` (`_wrap_command_with_pre_script`, joined with `shlex.join` so any command shape survives). This is meant for compilation steps (e.g., `javac *.java`) but instructors put arbitrary shell commands here. Your executor must support this.

16. **1-second sleep in signals** — The `auto_execute_submission` signal includes a `time.sleep(1)` to avoid race conditions. Always mute signals in test factories.

17. **Runner files are named `.codepost_runner.*`** — The interpreted templates delete their own file as their first statement, guarded by that basename prefix (so running a template locally or from the test suite's temp files leaves it alone). Compiled languages `rm` the runner source in the shell chain right after compiling. Runner files are staged after additional files so they win any name collision.

---

## Test Script Syntax Reference

### Python

```python
@test(name="Add two numbers", points=5, description="Tests basic addition", timeout=10)
def test_add():
    from student import add
    assert add(1, 2) == 3

@test("Partial credit example", 10)
def test_partial():
    # Return a number for partial credit
    score = 0
    if condition1: score += 5
    if condition2: score += 5
    return score
```

### JavaScript (Node)

```javascript
test(
    "Add two numbers",
    5,
    "Tests basic addition",
    () => {
        const { add } = require("./student");
        assert(add(1, 2) === 3);
    },
    10,
);
```

### C++

```cpp
TEST(test_add, 5) {
    ASSERT_EQ(add(1, 2), 3);
}

TEST_DESC(test_add, 5, "Tests basic addition") {
    ASSERT_EQ(add(1, 2), 3);
}

TEST_TIMEOUT(test_add, 5, 10) {
    ASSERT_EQ(add(1, 2), 3);
}
```

### Java

```java
@Test(name="Add two numbers", points=5)
public void testAdd() {
    assertEquals(3, Student.add(1, 2));
}
```

### R

```r
run_test("Add two numbers", 5, "Tests basic addition", function() {
    result <- add(1, 2)
    stopifnot(result == 3)
})
```

### Ruby

```ruby
run_test("Add two numbers", 5, "Tests basic addition", 10) do
    result = add(1, 2)
    raise "Expected 3, got #{result}" unless result == 3
end
```

### PHP

```php
Tester::test("Add two numbers", 5, "Tests basic addition", function() {
    $result = add(1, 2);
    assert($result === 3);
});
```

---

## Celery Task Flow

```
┌─────────────────────────────────────────────────────────────────┐
│ RunSubmission (on student submit)                                │
├─────────────────────────────────────────────────────────────────┤
│ 1. Wait if environment is building (polls up to 5 min)          │
│ 2. Execute all submission files (cache results)                  │
│ 3. Converger: analyze failures → maybe trigger rebuild          │
│ 4. Autodetector: update language if cold start                  │
│ 5. If runTestsOnSubmit: TestService.run_suite()                 │
└─────────────────────────────────────────────────────────────────┘

┌─────────────────────────────────────────────────────────────────┐
│ BuildEnvironment                                                │
├─────────────────────────────────────────────────────────────────┤
│ 1. Optionally refresh auto-detection                            │
│ 2. Builder.build() → docker build with streaming logs           │
│ 3. On success: optionally re-run queued submissions             │
└─────────────────────────────────────────────────────────────────┘

┌─────────────────────────────────────────────────────────────────┐
│ RunAll (batch execution by instructor)                          │
├─────────────────────────────────────────────────────────────────┤
│ 1. Iterate all submissions                                      │
│ 2. TestService.run_suite() for each                             │
│ 3. Update progress, send completion email                       │
└─────────────────────────────────────────────────────────────────┘
```

---

## Directory Structure

```
autograder/
├── services/
│   ├── templates/          ← YOU ARE HERE
│   │   ├── template.py         # Python execution harness
│   │   ├── template.js         # Node.js execution harness
│   │   ├── template.cpp        # C++ execution harness
│   │   ├── template.r          # R execution harness
│   │   ├── template.rb         # Ruby execution harness
│   │   ├── template.php        # PHP execution harness
│   │   ├── TestRunner.java     # Java execution harness
│   │   ├── notebook_template.* # Notebook variants
│   │   └── README.md           # This file
│   ├── executors/
│   │   ├── base.py             # Base Executor class
│   │   ├── python.py           # PythonExecutor
│   │   ├── node.py             # NodeExecutor
│   │   ├── java.py             # JavaExecutor
│   │   ├── cpp.py              # CPPExecutor
│   │   ├── r.py                # RExecutor
│   │   ├── ruby.py             # RubyExecutor
│   │   ├── php.py              # PHPExecutor
│   │   └── shell.py            # ShellExecutor
│   ├── builder.py              # Docker image builder
│   ├── autodetector.py         # Language auto-detection
│   ├── converger.py            # Auto-dependency healing
│   ├── detection.py            # Detection entry points
│   ├── image_manager.py        # Image versioning/rollback
│   ├── TestService.py          # Test orchestration
│   └── TestParsingService.py   # Static test script analysis
├── testUtils/
│   └── buildSpecs.json         # Per-language Docker build specs
├── tasks.py                    # Celery task definitions
├── run.py                      # RunSubmission orchestrator
└── docker/                     # Additional Docker resources
```
