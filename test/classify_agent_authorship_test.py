import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SCRIPT = ROOT / ".github" / "scripts" / "classify_agent_authorship.py"


def parse_outputs(text):
    return dict(line.split("=", 1) for line in text.splitlines())


def classify(commits, github_output=None):
    input_data = "\n".join(json.dumps(commit) for commit in commits)
    args = [sys.executable, str(SCRIPT)]
    if github_output:
        args += ["--github-output", github_output]
    result = subprocess.run(
        args, input=input_data, capture_output=True, text=True
    )
    assert result.returncode == 0, result.stderr
    return parse_outputs(result.stdout)


class ClassifyAgentAuthorshipTest(unittest.TestCase):
    def test_untrailered_commits_are_agent_assisted(self):
        outputs = classify([{"sha": "abc", "message": "fix: regular change"}])

        self.assertEqual("agent-assisted", outputs["label"])
        self.assertEqual("1", outputs["total_commits"])
        self.assertEqual("0", outputs["agent_commits"])
        self.assertEqual("1", outputs["untrailered_commits"])
        self.assertEqual("0", outputs["incomplete_agent_commits"])

    def test_complete_maestro_trailers_are_agent_authored(self):
        outputs = classify(
            [
                {
                    "sha": "abc",
                    "message": (
                        "feat: ship change\n"
                        "\n"
                        "Co-Authored-By: Maestro <maestro@evalops.dev>\n"
                        "Maestro-Version: 2026.04.28 / gpt-5\n"
                        "Maestro-Prompt-Id: prompt-123\n"
                        "Maestro-Approvals-Id: approval-456\n"
                    ),
                }
            ]
        )

        self.assertEqual("agent-authored", outputs["label"])
        self.assertEqual("1", outputs["agent_commits"])
        self.assertEqual("0", outputs["untrailered_commits"])
        self.assertEqual("0", outputs["incomplete_agent_commits"])

    def test_mixed_authorship_and_incomplete_trailers_are_reported(self):
        outputs = classify(
            [
                {
                    "sha": "abc",
                    "message": (
                        "feat: partial agent change\n"
                        "\n"
                        "Co-Authored-By: Maestro <maestro@evalops.dev>\n"
                        "Maestro-Version: 2026.04.28 / gpt-5\n"
                    ),
                },
                {"sha": "def", "message": "docs: human follow-up"},
            ]
        )

        self.assertEqual("mixed-authorship", outputs["label"])
        self.assertEqual("1", outputs["agent_commits"])
        self.assertEqual("1", outputs["untrailered_commits"])
        self.assertEqual("1", outputs["incomplete_agent_commits"])

    def test_github_output_file_gets_same_outputs(self):
        with tempfile.NamedTemporaryFile(mode="r", suffix="github-output") as handle:
            outputs = classify(
                [{"sha": "abc", "message": "fix: regular change"}],
                github_output=handle.name,
            )
            file_outputs = parse_outputs(handle.read())

        self.assertEqual(outputs, file_outputs)


if __name__ == "__main__":
    unittest.main()
