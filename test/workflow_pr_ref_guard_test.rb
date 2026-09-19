# frozen_string_literal: true

require "minitest/autorun"
require "yaml"

class WorkflowPrRefGuardTest < Minitest::Test
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
