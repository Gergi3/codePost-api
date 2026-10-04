// Copyright © 2026 Rutgers, the State University of New Jersey. All rights reserved except as defined by the Rutgers Non-Commercial License, included with this software.
// Node.js Execution Template

// The executor stages this script as /work/.codepost_runner.js; unlink it first
// thing (Node has already read the whole file). Guarded by name for local/test runs.
try { if (require("path").basename(__filename).startsWith(".codepost_runner")) require("fs").unlinkSync(__filename); } catch (e) {}

const fs = require("fs");
const { execSync } = require("child_process");

const packages_to_install = []; // REPLACED_BY_EXECUTOR

function log(msg, level) {
    console.error(`[${level}] ${msg}`);
}

if (packages_to_install.length > 0) {
    log(`Installing packages: ${packages_to_install.join(", ")}`, "INFO");
    try {
        // Create package.json if needed or just install
        // Using --no-save to avoid package-lock updates, --no-audit for speed
        execSync(`npm install --no-save --no-audit ${packages_to_install.join(" ")}`, { stdio: "inherit" });
        packages_to_install.forEach((p) => console.error(`CODEPOST_AUTO_INSTALL_SUCCESS: ${p}`));
        log("✓ Packages installed", "INFO");
    } catch (e) {
        log(`Failed to install packages: ${e.message}`, "ERROR");
    }
}

log("<<<RESULT>>>", "SYSTEM");

// ============= Test Framework =============
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
    if (typeof description === "function") {
        timeout = fn;
        fn = description;
        description = null;
    }
    if (typeof timeout === "undefined" || timeout === null) {
        timeout = 30;
    }
    // hidden and objectives are parsed from @codepost directives at sync time,
    // not used at runtime — they only affect DB state.

    tests.push({
        name,
        points,
        description,
        fn,
        timeout
    });
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

    if (STUDENT_CODE_SYNTAX_INVALID) {
        const baseMsg = "Student code syntax was invalid. Fix syntax errors before running tests.";
        result.message = baseMsg;
        result.error = STUDENT_CODE_SYNTAX_ERROR_MSG ? `${baseMsg}\n${STUDENT_CODE_SYNTAX_ERROR_MSG}` : baseMsg;
        result.status = "error";
        result.passed = false;
        result.score = 0;
        testResults.push(result);
        return;
    }

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
        result.error = e && (e.message || String(e));
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
    process.stdout.write("<<<TEST_RESULT_JSON_START>>>");
    process.stdout.write(JSON.stringify(testResults));
    process.stdout.write("<<<TEST_RESULT_JSON_END>>>");
}
// ============================================

(async () => {
    // USER CODE BEGINS
    try {
        // We wrap in a block or just execute.
        // Since we are concatenating, variables declared with const/let at top level might conflict if we were inside a function,
        // but we are top level.
        // FILLER_CODE
    } catch (e) {
        console.error(e);
        if (looksLikeSyntaxInvalid(e)) {
            STUDENT_CODE_SYNTAX_INVALID = true;
            STUDENT_CODE_SYNTAX_ERROR_MSG = e && (e.stack || e.message || String(e));
            console.error("Student code syntax was invalid. Tests will be marked with syntax error context.");
        }
    }

    // Execute test code if provided
    const testCodeB64 = "{test_code_b64}";
    if (testCodeB64 && testCodeB64.length > 0) {
        try {
            const testCode = Buffer.from(testCodeB64, "base64").toString("utf-8");
            if (testCode.trim().length > 0) {
                eval(testCode);
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
    if (testResults.length > 0) {
        outputTestResults();
    }
})();
