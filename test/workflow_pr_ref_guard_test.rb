# frozen_string_literal: true

require "minitest/autorun"
require "fileutils"
require "open3"
require "tmpdir"
require "yaml"

class WorkflowPrRefGuardTest < Minitest::Test
  def test_codex_rails_validates_a_fresh_checkout_when_the_runner_store_is_corrupt
    workflow = YAML.safe_load(
      File.read(File.join(root, ".github", "workflows", "codex-rails-check.yml")),
      aliases: true,
    )
    job = workflow.fetch("jobs").fetch("validate")
    checkout = job.fetch("steps").find { |step| step["uses"].to_s.start_with?("actions/checkout@") }
    template = checkout.fetch("with").fetch("path")
    assert_includes template, "${{ github.run_id }}"
    assert_includes template, "${{ github.run_attempt }}"
    assert_equal template, job.fetch("defaults").fetch("run").fetch("working-directory")
    job.fetch("steps").select { |step| step.key?("run") }.each do |step|
      assert_equal template, step.fetch("working-directory", template)
    end

    Dir.mktmpdir("codex-rails-checkout-test") do |temp|
      source = File.join(temp, "source")
      reused = File.join(temp, "runner", "mono")
      FileUtils.mkdir_p(File.join(source, ".github", "workflows"))
      File.write(File.join(source, ".github", "workflows", "fixture.yml"), "name: fixture\n")
      git!("init", source)
      git!("-C", source, "add", ".")
      git!("-C", source, "-c", "user.name=Fixture", "-c", "user.email=fixture@example.invalid", "commit", "-m", "fixture")
      git!("clone", "--no-hardlinks", source, reused)
      blob = git!("-C", reused, "rev-parse", "HEAD:.github/workflows/fixture.yml").strip
      File.delete(File.join(reused, ".git", "objects", blob[0, 2], blob[2..-1]))
      _, _, broken = Open3.capture3("git", "-C", reused, "fsck", "--full")
      refute broken.success?, "The disposable reused checkout must actually lack a Git object."
      sentinel = File.join(temp, "runner", "cache-sentinel")
      File.write(sentinel, "preserve me")

      relative = template.gsub("${{ github.run_id }}", "123").gsub("${{ github.run_attempt }}", "2")
      fresh = File.join(temp, "runner", relative)
      git!("clone", "--no-local", source, fresh)
      git!("-C", fresh, "fsck", "--full")
      git!("-C", fresh, "cat-file", "-e", "#{blob}^{blob}")
      env = { "RUNNER_TEMP" => temp, "RUNNER_NAME" => "fixture" }
      preflight = job.fetch("steps").find { |step| step["name"] == "Preflight the YAML toolchain" }.fetch("run")
      validate = job.fetch("steps").find { |step| step["name"] == "Validate workflow YAML" }.fetch("run")
      [preflight, validate].each do |script|
        stdout, stderr, status = Open3.capture3(env, "bash", "-c", script, chdir: fresh)
        assert status.success?, "Fresh-checkout validation failed: #{stdout}\n#{stderr}"
      end
      File.write(File.join(fresh, ".github", "workflows", "fixture.yml"), "name: [unclosed\n")
      _, _, invalid = Open3.capture3(env, "bash", "-c", validate, chdir: fresh)
      refute invalid.success?, "The unchanged validator must still reject invalid workflow YAML."
      assert_equal "preserve me", File.read(sentinel)
      assert File.directory?(File.join(reused, ".git")), "The reused runner store must not be deleted."
    end
  end

  def test_review_workflows_do_not_depend_on_synthetic_pull_request_merge_refs
    offenders = []
    workflow_paths.each do |path|
      File.readlines(path, chomp: true).each_with_index do |line, index|
        next unless line.match?(%r{refs/pull/.*/merge})

        offenders << "#{relative_path(path)}:#{index + 1}: #{line.strip}"
      end
    end

    assert_empty(
      offenders,
      "Synthetic PR merge refs disappear for open conflicting PRs. " \
      "Review automation should check out refs/pull/<n>/head, then fetch base/head SHAs for diffs.\n" \
      "#{offenders.join("\n")}"
    )
  end

  def test_upload_artifact_steps_set_retention_days
    offenders = []
    workflow_paths.each do |path|
      data = YAML.safe_load(File.read(path), aliases: true) || {}
      jobs = data.fetch("jobs", {}) || {}
      jobs.each do |job_name, job|
        Array(job && job["steps"]).each_with_index do |step, index|
          next unless step.is_a?(Hash) && step["uses"].to_s.include?("actions/upload-artifact")

          with = step["with"].is_a?(Hash) ? step["with"] : {}
          next if with.key?("retention-days")

          offenders << "#{relative_path(path)} #{job_name} step #{index + 1}"
        end
      end
    end

    assert_empty(
      offenders,
      "Every upload-artifact step must set retention-days so diagnostic artifacts do not silently keep the repo default.\n" \
      "#{offenders.join("\n")}"
    )
  end

  def test_agent_authorship_label_apply_is_best_effort_on_token_denial
    workflow = File.read(File.join(root, ".github", "workflows", "agent-authorship-label.yml"))

    assert_includes workflow, "Skipping authorship label apply"
    assert_match(/Bad credentials\|HTTP 401\|Resource not accessible\|HTTP 403/, workflow)
    assert_operator(
      workflow.index("Apply authorship label"),
      :<,
      workflow.index("Check required Maestro trailers"),
      "The required trailer gate should still run after best-effort label application.",
    )
  end

  def test_agent_authorship_classification_runs_on_python3_without_ruby
    workflow = YAML.safe_load(
      File.read(File.join(root, ".github", "workflows", "agent-authorship-label.yml")),
      aliases: true,
    )
    steps = workflow.fetch("jobs").fetch("label").fetch("steps")

    setup_ruby = steps.select { |step| step["uses"].to_s.include?("ruby/setup-ruby") }
    assert_empty(
      setup_ruby,
      "Ruby is absent from newer runner images; the classifier must run on python3, not a live Ruby install.",
    )

    preflight_index = steps.index { |step| step["name"] == "Preflight the classification toolchain" }
    classify_index = steps.index { |step| step["name"] == "Classify authorship" }

    refute_nil preflight_index, "The reusable workflow must assert python3 exists before relying on it."
    refute_nil classify_index
    assert_operator preflight_index, :<, classify_index

    preflight_run = steps.fetch(preflight_index).fetch("run")
    assert_includes preflight_run, "command -v python3"

    classify_run = steps.fetch(classify_index).fetch("run")
    assert_includes classify_run, "python3"
    assert_includes classify_run, "classify_agent_authorship.py"
    refute_includes classify_run, "ruby "
  end

  private

  def git!(*args)
    stdout, stderr, status = Open3.capture3("git", *args)
    assert status.success?, "git #{args.join(' ')} failed: #{stderr}"
    stdout
  end

  def root
    File.expand_path("..", __dir__)
  end

  def workflow_paths
    Dir.glob(File.join(root, ".github", "{workflows,workflow-templates}", "*.{yml,yaml}")).sort
  end

  def relative_path(path)
    path.delete_prefix("#{root}/")
  end
end
