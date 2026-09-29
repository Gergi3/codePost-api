// Copyright © 2026 Rutgers, the State University of New Jersey. All rights reserved except as defined by the Rutgers Non-Commercial License, included with this software.
<?php
// PHP Execution Template

// The executor stages this script as /work/.codepost_runner.php; unlink it first
// thing (PHP has already read the whole file). Guarded by name for local/test runs.
if (str_starts_with(basename(__FILE__), '.codepost_runner')) { @unlink(__FILE__); }

$packages_to_install = []; // REPLACED_BY_EXECUTOR

function template_log($msg, $level) {
    fwrite(STDERR, "[$level] $msg\n");
}

if (!empty($packages_to_install)) {
    template_log("Installing packages: " . implode(', ', $packages_to_install), "INFO");
    // Composer require
    // Assuming composer is available globally
    foreach ($packages_to_install as $pkg) {
         exec("composer require --no-interaction " . escapeshellarg($pkg), $output, $return_var);
         if ($return_var === 0) {
             template_log("✓ $pkg installed", "INFO");
             fwrite(STDERR, "CODEPOST_AUTO_INSTALL_SUCCESS: $pkg\n");
         } else {
             template_log("Failed to install $pkg", "ERROR");
         }
    }
}

fwrite(STDERR, "<<<RESULT>>>\n");

// USER CODE BEGINS
// We use include logic or just paste code.
// Since PHP needs <?php tag, but we are inside one.
// The user code presumably starts with <?php ? or implies it?
// Usually user scripts have <?php. If we assume they do, we must handle it.
// If we paste it here, we should strip the opening tag or close ours.

// OPTION: Close PHP tag, then paste code (which opens it)
?>
# FILLER_CODE
