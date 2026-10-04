# Copyright © 2026 Rutgers, the State University of New Jersey. All rights reserved except as defined by the Rutgers Non-Commercial License, included with this software.
# This is a template for running R Jupyter notebook code cells inside a Docker container.

# To use this template replace the placeholder {cells_b64} with a base64-encoded JSON array of cells, And packages_to_install with a list of packages to install.

# The executor stages this script as /work/.codepost_runner.R; remove it as the
# first expression. Rscript opened --file= once at startup and reads through that
# handle, so unlinking the path is safe. Guarded by name for local/test runs.
local({
    f <- sub("^--file=", "", grep("^--file=", commandArgs(), value = TRUE))
    if (length(f) == 1 && startsWith(basename(f), ".codepost_runner")) suppressWarnings(file.remove(f))
})

MAX_CELLS <- 500  # Maximum number of cells allowed to prevent abuse

packages_to_install <- list()

# template_log is a function that logs the template to a file.
template_log <- function(message) {
   cat(paste0(message, "\n"), file = stderr())
}

file_exists <- function(file_path) {
   file.info(file_path)$size > 0
}
dir_exists <- function(dir_path) {
   file.info(dir_path)$isdir
}

template_log("Template started.")
start_time <- Sys.time()
template_log(paste("Initial working directory: ", getwd()))
template_log(paste("/work exists: ", dir_exists("/work")))
template_log(paste("/root/shared exists: ", dir_exists("/root/shared")))

# if /work exists, set it as the working directory
if (dir_exists("/work")) {
   setwd("/work")
   template_log(paste("Working directory set to: ", getwd()))
} else {
   template_log("/work directory does not exist.")
   q(save="no", status=1)
}

# Install required packages if not present
required_pkgs <- c("base64enc", "jsonlite")
new_pkgs <- required_pkgs[!(required_pkgs %in% installed.packages()[,"Package"])]
if(length(new_pkgs)) {
    template_log(paste("Installing required packages:", paste(new_pkgs, collapse = ", ")))
    install.packages(new_pkgs, repos = "https://cloud.r-project.org")
}

library(base64enc)
library(jsonlite)


# install packages
if (length(packages_to_install) > 0) {
   install.packages(packages_to_install)
   template_log(paste("Packages installed: ", paste(packages_to_install, collapse = ", ")))
   # Log to stderr (using message or cat(file=stderr())) so Converger picks it up
   for (pkg in packages_to_install) {
       message(paste0("CODEPOST_AUTO_INSTALL_SUCCESS: ", pkg))
   }
} else {
   template_log("No packages to install.")
}

# Security: Make cache read-only after installation to prevent notebook code from tampering
if (length(packages_to_install) > 0) {
   template_log("Making cache read-only after installation.")
   # Attempt to lock standard cache locations if they exist and are writable
   cache_dirs <- c(Sys.getenv("R_LIBS_USER"), "/tmp/pip-cache", "/tmp/npm-cache") 
   for (cache_dir in cache_dirs) {
       if (dir.exists(cache_dir)) {
           tryCatch({
               Sys.chmod(cache_dir, mode = "0555", use_umask = FALSE)
           }, error = function(e) {
               template_log(paste("Could not lock cache dir:", cache_dir))
           })
       }
   }
}

# flush console
flush.console()

# decode cells
decoded_raw <- base64decode('{cells_b64}')
decoded <- rawToChar(decoded_raw)

# parse cells - use simplifyVector=FALSE to get proper list structure
notebook_json <- fromJSON(decoded, simplifyVector = FALSE)

# check if cells are valid - use 'type' key as that's what prepare_notebook uses
if (!is.list(notebook_json)) {
    template_log("notebook_json is not a list")
    q(save="no", status=1)
}
results <- list()
STUDENT_CODE_SYNTAX_INVALID <- FALSE
STUDENT_CODE_SYNTAX_ERROR_MSG <- ""

# Use global environment for cell execution (like Jupyter R kernels do)
cell_env <- globalenv()

# check if cells are too many
if (length(notebook_json) > MAX_CELLS) {
   template_log(paste("Too many cells: ", length(notebook_json)))
   results <- list(
    type='markdown',
    source=paste("**Error:** Too many cells. Maximum allowed: ", MAX_CELLS, "\n\n", "Execution stopped.")
   )
} else {
    NBS_OUTPUT_LIMIT <- 10000  # Max characters in output 10kb
    execution_count <- 0
    
    for (cell in notebook_json) {
        if (cell$type == "markdown") {
            results <- c(results, list(list(
                cell_type = 'markdown',
                source = cell$source
            )))
        }
        if (cell$type == "code") {
            execution_count <- execution_count + 1
            # Handle source as array or string
            cell_source <- if (is.list(cell$source) || is.character(cell$source) && length(cell$source) > 1) {
                paste(unlist(cell$source), collapse = "")
            } else {
                cell$source
            }
            
            outputs <- list()
            success <- TRUE
            error_msg <- NULL
            
            # Capture stdout
            stdout_file <- tempfile(fileext = ".txt")
            stderr_file <- tempfile(fileext = ".txt")
            stderr_con <- NULL
            output_sink_active <- FALSE
            message_sink_active <- FALSE
            
            # Create a temp file for potential plot output
            plot_file <- tempfile(fileext = ".png")
            
            # Parse and eval the code BEFORE any output capture
            # R's sink() interferes with eval() when capturing output
            parsed <- tryCatch({
                parse(text = cell_source)
            }, error = function(e) {
                success <<- FALSE
                error_msg <<- conditionMessage(e)
                STUDENT_CODE_SYNTAX_INVALID <<- TRUE
                STUDENT_CODE_SYNTAX_ERROR_MSG <<- conditionMessage(e)
                NULL
            })
            
            if (!is.null(parsed) && length(parsed) > 0) {
                for (expr in parsed) {
                    tryCatch({
                        eval(expr, envir = globalenv())
                    }, error = function(e) {
                        success <<- FALSE
                        error_msg <<- conditionMessage(e)
                    })
                }
            }
            
            tryCatch({
                # Open graphics device to capture plots
                png(plot_file, width = 800, height = 600, res = 100)
                
                # Capture stdout and stderr
                sink(stdout_file, type = "output")
                output_sink_active <- TRUE
                stderr_con <- file(stderr_file, open = "wt")
                sink(stderr_con, type = "message")
                message_sink_active <- TRUE
                
                withCallingHandlers({
                    # Parse and evaluate the code - already done above, just need output
                    if (!is.null(parsed) && length(parsed) > 0) {
                        last_value <- NULL
                        # Directly evaluate the parsed code in global environment
                        for (i in seq_along(parsed)) {
                            expr <- parsed[[i]]
                            last_value <- tryCatch({
                                eval(expr)
                            }, error = function(e) {
                                success <<- FALSE
                                error_msg <<- conditionMessage(e)
                                NULL
                            })
                        }
                    }
                }, warning = function(w) {
                    warning_con <- if (!is.null(stderr_con) && isOpen(stderr_con)) stderr_con else stderr()
                    cat(paste("Warning:", conditionMessage(w), "\n"), file = warning_con)
                    invokeRestart("muffleWarning")
                })
                
                
            }, error = function(e) {
                success <<- FALSE
                error_msg <<- conditionMessage(e)
            }, finally = {
                # Close sinks
                if (message_sink_active) {
                    try(sink(type = "message"), silent = TRUE)
                }
                if (!is.null(stderr_con) && isOpen(stderr_con)) {
                    close(stderr_con)
                }
                if (output_sink_active) {
                    try(sink(type = "output"), silent = TRUE)
                }
                
                # Close graphics device
                if (length(dev.list()) > 0) {
                    dev.off()
                }
            })
            
            # Read captured stdout
            stdout_text <- ""
            if (file.exists(stdout_file) && file.info(stdout_file)$size > 0) {
                stdout_text <- paste(readLines(stdout_file, warn = FALSE), collapse = "\n")
            }
            
            # Read captured stderr
            stderr_text <- ""
            if (file.exists(stderr_file) && file.info(stderr_file)$size > 0) {
                stderr_text <- paste(readLines(stderr_file, warn = FALSE), collapse = "\n")
            }
            
            # Check if a plot was created
            # Note: Empty PNG files from R are typically ~100-200 bytes
            # A real plot will be at least 1KB, so we use 1000 bytes as threshold
            EMPTY_PLOT_THRESHOLD <- 1000
            if (file.exists(plot_file) && file.info(plot_file)$size > EMPTY_PLOT_THRESHOLD) {
                # File is large enough to contain actual plot content
                tryCatch({
                    # Read plot and encode as base64
                    plot_data <- readBin(plot_file, "raw", file.info(plot_file)$size)
                    plot_base64 <- base64encode(plot_data)
                    
                    outputs <- c(outputs, list(list(
                        output_type = 'display_data',
                        data = list(
                            `image/png` = plot_base64
                        ),
                        metadata = list()
                    )))
                }, error = function(e) {
                    # If plot capture fails, add to stderr
                    stderr_text <- paste0(stderr_text, "\nPlot capture error: ", conditionMessage(e), "\n")
                })
            }
            
            # Clean up temp files
            unlink(c(stdout_file, stderr_file, plot_file))
            
            # Truncate stdout if too long
            if (nchar(stdout_text) > NBS_OUTPUT_LIMIT) {
                stdout_text <- paste0(substr(stdout_text, 1, NBS_OUTPUT_LIMIT), "\n...[ERROR output truncated]\n")
            }
            
            # Truncate stderr if too long
            if (nchar(stderr_text) > NBS_OUTPUT_LIMIT) {
                stderr_text <- paste0(substr(stderr_text, 1, NBS_OUTPUT_LIMIT), "\n...[ERROR output truncated]\n")
            }
            
            # Build outputs list
            if (nchar(stdout_text) > 0) {
                outputs <- c(outputs, list(list(
                    output_type = 'stream',
                    name = 'stdout',
                    text = stdout_text
                )))
            }
            
            if (nchar(stderr_text) > 0) {
                outputs <- c(outputs, list(list(
                    output_type = 'stream',
                    name = 'stderr',
                    text = stderr_text
                )))
            }
            
            if (!success) {
                # Truncate error message if too long
                if (!is.null(error_msg) && nchar(error_msg) > NBS_OUTPUT_LIMIT) {
                    error_msg <- paste0(substr(error_msg, 1, NBS_OUTPUT_LIMIT), "\n...[ERROR message truncated]\n")
                }
                
                outputs <- c(outputs, list(list(
                    output_type = 'error',
                    ename = 'ExecutionError',
                    evalue = if (!is.null(error_msg)) error_msg else stderr_text,
                    traceback = list(stderr_text)
                )))
            }
            
            results <- c(results, list(list(
                cell_type = 'code',
                source = cell_source,
                outputs = outputs,
                execution_count = execution_count
            )))
        }
    }
}

# Output results as JSON
end_time <- Sys.time()
execution_time <- as.numeric(difftime(end_time, start_time, units = "secs"))

# ============= Tester Framework =============
assertion_error <- function(message) {
    cond <- simpleCondition(message, call = sys.call(-1))
    class(cond) <- c("assertion_error", "error", "condition")
    stop(cond)
}

test_results <- list()

run_test <- function(name, points, description, fn = NULL, timeout = 30) {
    if (is.function(description)) {
        timeout <- fn
        fn <- description
        description <- NULL
    }

    result <- list(
        name = name,
        max_score = points,
        description = description,
        message = "",
        score = 0,
        passed = FALSE,
        status = "failed",
        error = "",
        output = ""
    )

    tryCatch({
        # Re-assign fn's environment to cell_env so it can access notebook functions
        environment(fn) <- cell_env
        setTimeLimit(elapsed = timeout, transient = TRUE)
        on.exit(setTimeLimit(elapsed = Inf, transient = TRUE), add = TRUE)

        ret <- fn()
        if (is.numeric(ret) && length(ret) >= 1) {
            score <- as.numeric(ret[[1]])
            result$score <- max(0, min(score, points))
            result$passed <- result$score == points
            result$status <- if (result$passed) "passed" else "partial"
        } else if (is.list(ret) && length(ret) >= 2 && is.numeric(ret[[1]])) {
            score <- as.numeric(ret[[1]])
            result$score <- max(0, min(score, points))
            result$message <- if (!is.null(ret[[2]])) as.character(ret[[2]]) else ""
            result$passed <- result$score == points
            result$status <- if (result$passed) "passed" else "partial"
        } else if (is.list(ret) && !is.null(ret$score) && is.numeric(ret$score)) {
            score <- as.numeric(ret$score)
            result$score <- max(0, min(score, points))
            result$message <- if (!is.null(ret$message)) as.character(ret$message) else ""
            result$passed <- result$score == points
            result$status <- if (result$passed) "passed" else "partial"
        } else if (is.logical(ret) && length(ret) >= 1) {
            result$passed <- as.logical(ret[[1]])
            result$score <- if (result$passed) points else 0
            result$status <- if (result$passed) "passed" else "failed"
        } else if (is.character(ret) && length(ret) >= 1) {
            result$message <- as.character(ret[[1]])
            result$passed <- TRUE
            result$score <- points
            result$status <- "passed"
        } else {
            result$passed <- TRUE
            result$score <- points
            result$status <- "passed"
        }
    }, error = function(e) {
        result$error <<- conditionMessage(e)
        if (inherits(e, "assertion_error")) {
            result$status <<- "failed"
        } else if (grepl("time limit|timed out|timeout", tolower(conditionMessage(e)))) {
            result$status <<- "error"
            result$message <<- "Test timed out"
        } else {
            result$status <<- "error"
        }
    })

    test_results <<- c(test_results, list(result))
}

output_test_results <- function() {
    cat('<<<TEST_RESULT_JSON_START>>>')
    cat(toJSON(test_results, auto_unbox = TRUE))
    cat('<<<TEST_RESULT_JSON_END>>>')
}
# ============================================

# Execute test code if provided
test_code_b64 <- '{test_code_b64}'
if (nchar(test_code_b64) > 0) {
    tryCatch({
        test_code <- rawToChar(base64decode(test_code_b64))
        if (nchar(trimws(test_code)) > 0) {
            eval(parse(text = test_code), envir = cell_env)
        }
    }, error = function(e) {
        test_results <<- c(test_results, list(list(
            name = "Test Script Execution",
            max_score = 0,
            description = NULL,
            message = "",
            score = 0,
            passed = FALSE,
            status = "error",
            error = paste("Failed to run test script:", conditionMessage(e)),
            output = ""
        )))
    })
}

# Output test results
output_test_results()

final_result <- list(
    success = TRUE, # If we got here without crashing, we consider it a success at the notebook level
    stdout = "",
    stderr = "",
    error = NULL,
    execution_time = execution_time,
    output_data = list(
        cells = results
    )
)

cat('<<<RESULTS_START>>>\n')
cat(toJSON(final_result, auto_unbox = TRUE, null = "null"))
cat('\n<<<RESULTS_END>>>\n')


