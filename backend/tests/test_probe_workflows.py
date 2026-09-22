"""The two probe workflows stay valid YAML with the agreed schedule, artifact naming and permissions."""

from pathlib import Path

import yaml

WORKFLOWS = Path(__file__).resolve().parents[2] / ".github" / "workflows"


def load(name: str) -> dict:
    return yaml.safe_load((WORKFLOWS / name).read_text(encoding="utf-8"))


def steps(workflow: dict, job: str) -> list[dict]:
    return workflow["jobs"][job]["steps"]


def test_collect_workflow_runs_every_five_minutes_and_keeps_the_run_file():
    wf = load("probe-collect.yml")
    assert wf[True]["schedule"] == [{"cron": "*/5 * * * *"}] and "workflow_dispatch" in wf[True]
    assert wf["permissions"] == {"contents": "read"}
    run = next(s for s in steps(wf, "collect") if "backend.app.probe.collect" in s.get("run", ""))
    assert "--out probe/runs" in run["run"]
    upload = next(s for s in steps(wf, "collect") if str(s.get("uses", "")).startswith("actions/upload-artifact"))
    assert upload["if"] == "always()" and upload["with"]["path"] == "probe/runs/*.json"
    assert upload["with"]["name"] == "probe-run-${{ github.run_id }}-${{ github.run_attempt }}"
    assert upload["with"]["retention-days"] == 7


def test_commit_workflow_runs_hourly_and_pushes_only_data():
    wf = load("probe-commit.yml")
    assert wf[True]["schedule"] == [{"cron": "7 * * * *"}]
    assert wf["permissions"] == {"contents": "write", "actions": "read"}
    assert wf["concurrency"]["group"] == "probe-commit"
    merge = next(s for s in steps(wf, "merge") if "backend.app.probe.merge" in s.get("run", ""))
    assert merge["env"]["GITHUB_TOKEN"] == "${{ secrets.GITHUB_TOKEN }}"
    push = next(s for s in steps(wf, "merge") if "git push" in s.get("run", ""))
    assert "git add probe/data" in push["run"] and "git pull --rebase" in push["run"]
    assert "nothing to commit" in push["run"]
