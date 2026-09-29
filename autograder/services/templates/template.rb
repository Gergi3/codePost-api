# Copyright © 2026 Rutgers, the State University of New Jersey. All rights reserved except as defined by the Rutgers Non-Commercial License, included with this software.
# Ruby Execution Template

# The executor stages this script as /work/.codepost_runner.rb; delete it first
# thing (Ruby has already read the whole file). Guarded by name for local/test runs.
begin
  File.delete(__FILE__) if File.basename(__FILE__).start_with?(".codepost_runner")
rescue StandardError
end

require 'open3'

packages_to_install = [] # REPLACED_BY_EXECUTOR

def log(msg, level)
  $stderr.puts "[#{level}] #{msg}"
end

unless packages_to_install.empty?
  log("Installing specific gems: #{packages_to_install.join(', ')}", "INFO")
  packages_to_install.each do |gem_name|
    begin
      if system("gem install --no-document #{gem_name}")
         log("✓ #{gem_name} installed", "INFO")
         $stderr.puts "CODEPOST_AUTO_INSTALL_SUCCESS: #{gem_name}"
      else
         log("Failed to install #{gem_name}", "ERROR")
      end
    rescue => e
      log("Error installing #{gem_name}: #{e}", "ERROR")
    end
  end
end

$stderr.puts "<<<RESULT>>>"

# USER CODE BEGINS
begin
  # FILLER_CODE
rescue => e
  $stderr.puts "#{e.class}: #{e.message}"
  $stderr.puts e.backtrace.join("\n")
  exit(1)
end
