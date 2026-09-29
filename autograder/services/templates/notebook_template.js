// Copyright © 2026 Rutgers, the State University of New Jersey. All rights reserved except as defined by the Rutgers Non-Commercial License, included with this software.

// The executor stages this script as /work/.codepost_runner.js; unlink it first
// thing (Node has already read the whole file). Guarded by name for local/test runs.
try { if (require("path").basename(__filename).startsWith(".codepost_runner")) require("fs").unlinkSync(__filename); } catch (e) {}

const fs = require("fs");
const vm = require("vm");

// Placeholder for base64 encoded cells
const cellsB64 = "{cells_b64}";
const testCodeB64 = "{test_code_b64}";

// Decode cells
const cellsJson = Buffer.from(cellsB64, "base64").toString("utf-8");
let cells;
try {
    cells = JSON.parse(cellsJson);
} catch (e) {
    console.error("Failed to parse cells JSON");
    process.exit(1);
}

// ============= Tester Framework =============
const tests = [];
const testResults = [];
let STUDENT_CODE_SYNTAX_INVALID = false;
let STUDENT_CODE_SYNTAX_ERROR_MSG = "";

function looksLikeSyntaxInvalid(err) {
    if (!err) return false;
    const msg = (err.message || String(err) || "").toLowerCase();
    return err.name === "SyntaxError" || msg.includes("syntax") || msg.includes("unexpected token");
}

function test(name, points, description, fn, timeout) {
    if (typeof description === 'function') {
        timeout = fn;
        fn = description;
        description = null;
    }
    if (typeof timeout === 'undefined' || timeout === null) {
        timeout = 30;
    }

    tests.push({ name, points, description, fn, timeout });
}

async function runTestCase(testCase) {
    const result = {
        name: testCase.name,
        max_score: testCase.points,
        description: testCase.description,
        message: "",
        score: 0,
        passed: false,
        status: "failed",
        error: "",
        output: ""
    };

    const timeoutMs = Math.max(0, Number(testCase.timeout) || 30) * 1000;
    const start = Date.now();

    try {
        const maybePromise = testCase.fn();
        const outcome = (maybePromise && typeof maybePromise.then === "function")
            ? await Promise.race([
                maybePromise,
                new Promise((_, reject) => setTimeout(() => reject(new Error(`Test timed out after ${timeoutMs / 1000}s`)), timeoutMs))
            ])
            : maybePromise;

        if (Array.isArray(outcome) && outcome.length >= 2 && typeof outcome[0] === "number") {
            result.score = Math.max(0, Math.min(outcome[0], testCase.points));
            result.message = outcome[1] != null ? String(outcome[1]) : "";
            result.passed = result.score === testCase.points;
            result.status = result.passed ? "passed" : "partial";
        } else if (outcome && typeof outcome === "object" && typeof outcome.score === "number") {
            result.score = Math.max(0, Math.min(outcome.score, testCase.points));
            result.message = outcome.message != null ? String(outcome.message) : "";
            result.passed = result.score === testCase.points;
            result.status = result.passed ? "passed" : "partial";
        } else if (typeof outcome === "number") {
            result.score = Math.max(0, Math.min(outcome, testCase.points));
            result.passed = result.score === testCase.points;
            result.status = result.passed ? "passed" : "partial";
        } else if (typeof outcome === "string") {
            result.message = outcome;
            result.passed = true;
            result.score = testCase.points;
            result.status = "passed";
        } else {
            result.passed = true;
            result.score = testCase.points;
            result.status = "passed";
        }
    } catch (e) {
        result.error = e.message || String(e);
        if (e && e.code === 'ERR_ASSERTION') {
            result.status = "failed";
        } else {
            result.status = "error";
            if (result.error && result.error.includes("timed out")) {
                result.message = "Test timed out";
            }
        }
    }

    testResults.push(result);
}

async function runAllTests() {
    for (const t of tests) {
        await runTestCase(t);
    }
}

function outputTestResults() {
    console.log("<<<TEST_RESULT_JSON_START>>>");
    console.log(JSON.stringify(testResults));
    console.log("<<<TEST_RESULT_JSON_END>>>");
}
// ============================================

const results = [];
const context = vm.createContext({
    console: {
        log: (...args) => {
            if (currentStdout) currentStdout.push(args.map((a) => String(a)).join(" "));
            else process.stdout.write(args.map((a) => String(a)).join(" ") + "\n");
        },
        error: (...args) => {
            if (currentStderr) currentStderr.push(args.map((a) => String(a)).join(" "));
            else process.stderr.write(args.map((a) => String(a)).join(" ") + "\n");
        },
        warn: (...args) => {
            if (currentStderr) currentStderr.push("WARN: " + args.map((a) => String(a)).join(" "));
        },
    },
    require: require,
    process: process,
    Buffer: Buffer,
    test: test, // Expose test function to VM context
});

let currentStdout = null;
let currentStderr = null;
let executionCount = 0;

async function run() {
    for (let i = 0; i < cells.length; i++) {
        const cell = cells[i];
        if (cell.type === "markdown") {
            results.push({
                cell_type: "markdown",
                source: cell.source,
            });
        } else if (cell.type === "code") {
            executionCount++;
            currentStdout = [];
            currentStderr = [];

            const cellSource = cell.source;
            let success = true;
            let resultValue = null;
            let errorVal = null;

            try {
                // Use vm.runInContext to maintain state between cells
                resultValue = vm.runInContext(cellSource, context);
            } catch (e) {
                success = false;
                errorVal = e;
                if (!STUDENT_CODE_SYNTAX_INVALID && looksLikeSyntaxInvalid(e)) {
                    STUDENT_CODE_SYNTAX_INVALID = true;
                    STUDENT_CODE_SYNTAX_ERROR_MSG = e && (e.stack || e.message || String(e));
                }
            }

            const outputs = [];

            // Collect stdout
            if (currentStdout.length > 0) {
                outputs.push({
                    output_type: "stream",
                    name: "stdout",
                    text: currentStdout.join("\n") + "\n",
                });
            }

            // Collect stderr
            if (currentStderr.length > 0) {
                outputs.push({
                    output_type: "stream",
                    name: "stderr",
                    text: currentStderr.join("\n") + "\n",
                });
            }

            if (!success) {
                outputs.push({
                    output_type: "error",
                    ename: errorVal.name || "Error",
                    evalue: errorVal.message || String(errorVal),
                    traceback: errorVal.stack ? errorVal.stack.split("\n") : [],
                });
            } else if (resultValue !== undefined) {
                outputs.push({
                    output_type: "execute_result",
                    execution_count: executionCount,
                    data: {
                        "text/plain": String(resultValue),
                    },
                    metadata: {},
                });
            }

            results.push({
                cell_type: "code",
                source: cellSource,
                outputs: outputs,
                execution_count: executionCount,
            });
        }
    }

    // Execute test code if provided
    if (testCodeB64 && testCodeB64.length > 0) {
        try {
            const testCode = Buffer.from(testCodeB64, "base64").toString("utf-8");
            if (testCode.trim().length > 0) {
                vm.runInContext(testCode, context);
            }
        } catch (e) {
            testResults.push({
                name: "Test Script Execution",
                max_score: 0,
                description: "",
                message: "",
                score: 0,
                passed: false,
                status: "error",
                error: "Failed to run test script: " + (e.message || e),
                output: ""
            });
        }
    }

    await runAllTests();
    // Output test results
    outputTestResults();

    const finalResult = {
        success: true,
        stdout: "",
        stderr: "",
        execution_time: 0,
        output_data: {
            cells: results,
            notebook: "",
        },
    };

    console.log("<<<RESULTS_START>>>");
    console.log(JSON.stringify(finalResult));
    console.log("<<<RESULTS_END>>>");
}

run();
