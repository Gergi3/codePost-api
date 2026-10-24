// Copyright © 2026 Rutgers, the State University of New Jersey. All rights reserved except as defined by the Rutgers Non-Commercial License, included with this software.
//
// codePost JUnit 5/6 harness.
//
// Discovers and runs the instructor's UNMODIFIED JUnit (Jupiter) tests that were
// compiled alongside the student's code into ./out, then emits one JSON array of
// per-test results between the markers the autograder parses
// (base.py: parse_test_results). Each @Test method maps to one result; `name` is
// the plain method name so TestService can match it to TestCase.functionName.
//
// Points: the executor fills in a per-method points map (method name -> points,
// default 1.0); the runner emits score=points on pass and 0 on failure/error.
//
// This file has NO package so it sits at the classpath root and is launched as
// `java ... JUnitPlatformRunner`. It is compiled with the student + test sources
// but never belongs to their packages.

import java.io.ByteArrayOutputStream;
import java.io.PrintStream;
import java.nio.file.Paths;
import java.util.ArrayList;
import java.util.HashMap;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;

import org.junit.platform.engine.TestExecutionResult;
import org.junit.platform.engine.discovery.DiscoverySelectors;
import org.junit.platform.engine.support.descriptor.MethodSource;
import org.junit.platform.launcher.Launcher;
import org.junit.platform.launcher.LauncherDiscoveryRequest;
import org.junit.platform.launcher.TestExecutionListener;
import org.junit.platform.launcher.TestIdentifier;
import org.junit.platform.launcher.core.LauncherDiscoveryRequestBuilder;
import org.junit.platform.launcher.core.LauncherFactory;

public class JUnitPlatformRunner {

    // Compiled student + test classes live here (javac -d out).
    private static final String CLASSPATH_ROOT = "out";

    // Default per-test timeout applied via JUnit's configuration parameter so one
    // hung test is aborted on its own instead of letting the whole container time
    // out (which would lose every result). The executor can override this value.
    private static final String DEFAULT_TEST_TIMEOUT = "#{DEFAULT_TIMEOUT}";

    // Bound each result's error/output text so a huge stacktrace or a flood of
    // student prints can never push the closing JSON marker past the 1 MB stdout
    // cap (which would make parse_test_results find nothing).
    private static final int MAX_FIELD_CHARS = 2000;

    // Injected by the executor: {"methodName": points, ...}. Default 1.0 per test.
    private static Map<String, Double> pointsMap() {
        Map<String, Double> m = new HashMap<>();
        #{POINTS_JSON}
        return m;
    }

    static final class Result {
        String name;
        String className;
        String description;
        double score;
        double maxScore;
        boolean passed;
        String status;   // "passed", "failed", "error"
        String error;
        String output;
    }

    public static void main(String[] args) {
        Map<String, Double> points = pointsMap();
        List<Result> results = new ArrayList<>();

        // Capture everything the tests print globally. Per-test capture is racy
        // under the Launcher, and students mainly want to see the full run output.
        PrintStream originalOut = System.out;
        PrintStream originalErr = System.err;
        ByteArrayOutputStream captured = new ByteArrayOutputStream();
        PrintStream capturing = new PrintStream(captured, true);

        CollectingListener listener = new CollectingListener(results, points);
        try {
            LauncherDiscoveryRequest request = LauncherDiscoveryRequestBuilder.request()
                    .selectors(DiscoverySelectors.selectClasspathRoots(
                            java.util.Collections.singleton(Paths.get(CLASSPATH_ROOT))))
                    .configurationParameter(
                            "junit.jupiter.execution.timeout.testable.method.default",
                            DEFAULT_TEST_TIMEOUT)
                    // SEPARATE_THREAD runs each test on its own thread so the timeout can
                    // actually abort a test stuck in an infinite loop. The default SAME_THREAD
                    // mode only checks the deadline AFTER the test method returns, so an
                    // infinite loop hangs until the whole container times out — losing every
                    // result. With SEPARATE_THREAD the hung test reports a TimeoutException
                    // and the rest of the suite still runs.
                    .configurationParameter(
                            "junit.jupiter.execution.timeout.thread.mode.default",
                            "SEPARATE_THREAD")
                    .build();

            Launcher launcher = LauncherFactory.create();
            launcher.registerTestExecutionListeners(listener);

            System.setOut(capturing);
            System.setErr(capturing);
            launcher.execute(request);

            // Any test still in listener.getStarted() fired executionStarted but never
            // executionFinished — the JVM killed the worker thread (OOM, etc.) before
            // the engine could record the result. Synthesize an error entry for each so
            // the autograder reports it rather than silently dropping it.
            for (TestIdentifier tid : listener.getStarted().values()) {
                String mn = tid.getSource()
                        .filter(s -> s instanceof MethodSource)
                        .map(s -> ((MethodSource) s).getMethodName())
                        .orElse(tid.getDisplayName());
                String sc = tid.getSource()
                        .filter(s -> s instanceof MethodSource)
                        .map(s -> ((MethodSource) s).getClassName())
                        .map(cn -> cn.contains(".") ? cn.substring(cn.lastIndexOf('.') + 1) : cn)
                        .orElse("");
                Result r = new Result();
                r.name = mn;
                r.className = sc;
                r.description = "";
                r.passed = false;
                r.score = 0;
                Double pts = points.get(sc + "." + mn);
                if (pts == null) pts = points.getOrDefault(mn, 1.0);
                r.maxScore = pts;
                r.status = "error";
                r.error = "Test did not complete — the JVM likely ran out of memory or was killed "
                        + "while this test was running. Check for infinite loops, excessive memory "
                        + "allocation, or very large data structures.";
                r.output = "";
                results.add(r);
            }
        } catch (Throwable t) {
            // Discovery/launch failed (e.g. nothing compiled). Emit one synthetic
            // error so the autograder reports a real problem instead of silence.
            Result crash = new Result();
            crash.name = "Test Execution";
            crash.description = "";
            crash.status = "error";
            crash.passed = false;
            crash.score = 0;
            crash.maxScore = 0;
            crash.error = truncate("JUnit launch failed: " + t);
            crash.output = "";
            results.add(crash);
        } finally {
            System.setOut(originalOut);
            System.setErr(originalErr);
        }

        String globalOutput = truncate(captured.toString());
        for (Result r : results) {
            if (r.output == null || r.output.isEmpty()) {
                r.output = globalOutput;
            }
        }

        System.out.println("<<<TEST_RESULT_JSON_START>>>" + toJson(results) + "<<<TEST_RESULT_JSON_END>>>");
        System.out.flush();

        // Force exit: a test aborted by a SEPARATE_THREAD timeout leaves its original
        // thread still spinning (JUnit cannot kill an uninterruptible loop). Those
        // non-daemon threads would otherwise keep the JVM alive forever, so the
        // container would hit its hard timeout and the results above would be lost.
        // We have already emitted every result, so exit immediately.
        System.exit(0);
    }

    static final class CollectingListener implements TestExecutionListener {
        private final List<Result> results;
        private final Map<String, Double> points;
        // Tracks tests that fired executionStarted but not yet executionFinished.
        // After launcher.execute() returns, any remaining entry was killed mid-run.
        private final Map<String, TestIdentifier> started = new LinkedHashMap<>();

        CollectingListener(List<Result> results, Map<String, Double> points) {
            this.results = results;
            this.points = points;
        }

        Map<String, TestIdentifier> getStarted() {
            return started;
        }

        @Override
        public void executionStarted(TestIdentifier id) {
            if (id.isTest()) {
                started.put(id.getUniqueId(), id);
            }
        }

        @Override
        public void executionFinished(TestIdentifier id, TestExecutionResult result) {
            if (!id.isTest()) {
                return; // Only leaf tests, not containers/classes.
            }

            String methodName = id.getSource()
                    .filter(src -> src instanceof MethodSource)
                    .map(src -> ((MethodSource) src).getMethodName())
                    .orElse(id.getDisplayName());

            // Fully-qualified class, reduced to the simple name (JUnit convention:
            // the simple class name equals the source file stem). Used to keep
            // identical method names in different test files distinct.
            String simpleClass = id.getSource()
                    .filter(src -> src instanceof MethodSource)
                    .map(src -> ((MethodSource) src).getClassName())
                    .map(cn -> cn.contains(".") ? cn.substring(cn.lastIndexOf('.') + 1) : cn)
                    .orElse("");

            Result r = new Result();
            r.name = methodName;
            r.className = simpleClass;
            // Jupiter defaults displayName to "method()"; only surface it when the
            // author set a real @DisplayName (i.e. it differs from the raw method).
            String display = id.getDisplayName();
            r.description = (display != null && !display.equals(methodName) && !display.equals(methodName + "()"))
                    ? display : "";
            // Points map is keyed by "Class.method" (preferred) with a fallback to
            // the bare method name for legacy single-file categories.
            Double pts = points.get(simpleClass + "." + methodName);
            if (pts == null) {
                pts = points.getOrDefault(methodName, 1.0);
            }
            r.maxScore = pts;
            r.output = "";

            switch (result.getStatus()) {
                case SUCCESSFUL:
                    r.passed = true;
                    r.score = r.maxScore;
                    r.status = "passed";
                    break;
                case FAILED:
                case ABORTED:
                default:
                    r.passed = false;
                    r.score = 0;
                    Throwable cause = result.getThrowable().orElse(null);
                    if (cause == null) {
                        r.status = "failed";
                        r.error = "Test did not pass";
                    } else if (cause instanceof java.util.concurrent.TimeoutException) {
                        // JUnit's TimeoutExtension throws java.util.concurrent.TimeoutException
                        // with status FAILED (not ABORTED — that's reserved for assumeTrue failures).
                        r.status = "error";
                        r.error = "Test timed out. Check for infinite loops or slow/expensive operations.";
                    } else if (cause instanceof AssertionError) {
                        // opentest4j.AssertionFailedError extends java.lang.AssertionError —
                        // this check cleanly separates an assertion failure ("failed") from
                        // any other exception ("error"). Message-only is clear enough for assertions.
                        r.status = "failed";
                        r.error = truncate(messageOnly(cause));
                    } else {
                        // Runtime exception (NPE, OOM caught by engine, TestAbortedException
                        // from assumeTrue, etc.) — show type + first stack frames so students
                        // can see exactly where their code failed.
                        r.status = "error";
                        r.error = truncate(stackToString(cause));
                    }
                    break;
            }
            results.add(r);
            // Mark this test as finished so it is not re-reported as a JVM-killed test.
            started.remove(id.getUniqueId());
        }
    }

    private static String messageOnly(Throwable t) {
        String full = t.toString();
        int nl = full.indexOf('\n');
        return nl > 0 ? full.substring(0, nl) : full;
    }

    private static String stackToString(Throwable t) {
        StringBuilder sb = new StringBuilder(t.toString());
        StackTraceElement[] trace = t.getStackTrace();
        int limit = Math.min(trace.length, 12);
        for (int i = 0; i < limit; i++) {
            sb.append("\n\tat ").append(trace[i]);
        }
        return sb.toString();
    }

    private static String truncate(String s) {
        if (s == null) {
            return "";
        }
        if (s.length() <= MAX_FIELD_CHARS) {
            return s;
        }
        return s.substring(0, MAX_FIELD_CHARS) + "\n... (truncated)";
    }

    // --- Minimal, correct JSON serialization (handles backslashes/controls/unicode) ---

    private static String toJson(List<Result> results) {
        StringBuilder sb = new StringBuilder("[");
        for (int i = 0; i < results.size(); i++) {
            Result r = results.get(i);
            if (i > 0) {
                sb.append(",");
            }
            sb.append("{")
              .append("\"name\":").append(jsonStr(r.name)).append(",")
              .append("\"className\":").append(jsonStr(r.className)).append(",")
              .append("\"description\":").append(jsonStr(r.description)).append(",")
              .append("\"score\":").append(num(r.score)).append(",")
              .append("\"max_score\":").append(num(r.maxScore)).append(",")
              .append("\"passed\":").append(r.passed).append(",")
              .append("\"status\":").append(jsonStr(r.status)).append(",")
              .append("\"error\":").append(jsonStr(r.error)).append(",")
              .append("\"output\":").append(jsonStr(r.output))
              .append("}");
        }
        sb.append("]");
        return sb.toString();
    }

    private static String num(double d) {
        if (d == Math.rint(d) && !Double.isInfinite(d)) {
            return Long.toString((long) d);
        }
        return Double.toString(d);
    }

    private static String jsonStr(String s) {
        if (s == null) {
            return "null";
        }
        StringBuilder sb = new StringBuilder("\"");
        for (int i = 0; i < s.length(); i++) {
            char c = s.charAt(i);
            switch (c) {
                case '"':  sb.append("\\\""); break;
                case '\\': sb.append("\\\\"); break;
                case '\b': sb.append("\\b"); break;
                case '\f': sb.append("\\f"); break;
                case '\n': sb.append("\\n"); break;
                case '\r': sb.append("\\r"); break;
                case '\t': sb.append("\\t"); break;
                default:
                    if (c < 0x20) {
                        sb.append(String.format("\\u%04x", (int) c));
                    } else {
                        sb.append(c);
                    }
            }
        }
        sb.append("\"");
        return sb.toString();
    }
}
