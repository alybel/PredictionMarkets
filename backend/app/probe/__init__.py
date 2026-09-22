"""GitHub Actions probe: six fixed markets, one JSON file per run, hourly merge into daily files.

``collect`` runs every five minutes and writes a run file; ``merge`` runs
hourly, pulls the run files (workflow artifacts) and appends them to
``probe/data/<day>.jsonl``. See ActionsReadme.md for the workflow setup.
"""

from backend.app.logging_setup import PROJECT_ROOT

PROBE_DIR = PROJECT_ROOT / "probe"
CONFIG_PATH = PROBE_DIR / "markets.json"
RUNS_DIR = PROBE_DIR / "runs"
DATA_DIR = PROBE_DIR / "data"
ARTIFACT_PREFIX = "probe-run-"
