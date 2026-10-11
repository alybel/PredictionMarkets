"""Workflow actions stay on Node-24 majors; Dependabot watches them (no network)."""

from pathlib import Path

import yaml

GITHUB = Path(__file__).resolve().parents[2] / ".github"
# First major running on Node 24 (looked up in the GitHub releases, 2026-10-11).
NODE24_MIN_MAJOR = {"actions/checkout": 5, "actions/setup-python": 6, "actions/upload-artifact": 6}


def _uses():
    for wf in sorted((GITHUB / "workflows").glob("*.yml")):
        for job in yaml.safe_load(wf.read_text(encoding="utf-8"))["jobs"].values():
            yield from (s["uses"] for s in job["steps"] if "uses" in s)


def test_actions_run_on_node24_majors():
    uses = list(_uses())
    assert uses
    for ref in uses:
        action, version = ref.split("@")
        assert action in NODE24_MIN_MAJOR, f"unknown action {ref}: add its Node-24 major"
        assert int(version.lstrip("v").split(".")[0]) >= NODE24_MIN_MAJOR[action], ref


def test_dependabot_watches_github_actions_weekly():
    config = yaml.safe_load((GITHUB / "dependabot.yml").read_text(encoding="utf-8"))
    assert config["version"] == 2
    assert {"package-ecosystem": "github-actions", "directory": "/",
            "schedule": {"interval": "weekly"}} in config["updates"]
