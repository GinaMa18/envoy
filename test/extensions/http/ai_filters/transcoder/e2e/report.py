"""Renders RUN_DIR/report.md from the results.jsonl files the client scripts write.

Usage: report.py RUN_DIR

One row per (client -> backend, model): PASS or FAIL, the counts, and the failed checks and known
gaps, so the report can be linked from a ticket as is.
"""
import collections
import json
import pathlib
import subprocess
import sys


def envoy_version():
    here = pathlib.Path(__file__).resolve().parent
    try:
        sha = subprocess.run(["git", "-C", str(here), "rev-parse", "--short", "HEAD"],
                             capture_output=True, text=True, check=True).stdout.strip()
        branch = subprocess.run(["git", "-C", str(here), "rev-parse", "--abbrev-ref", "HEAD"],
                                capture_output=True, text=True, check=True).stdout.strip()
        return f"{branch} @ {sha}"
    except (OSError, subprocess.CalledProcessError):
        return "unknown"


def main():
    run_dir = pathlib.Path(sys.argv[1])
    runs = collections.OrderedDict()
    for log in sorted(run_dir.rglob("results.jsonl")):
        for line in log.read_text().splitlines():
            record = json.loads(line)
            key = (record["client"], record["backend"], record["model"], record["resolved_model"])
            runs.setdefault(key, {"records": [], "dir": log.parent})["records"].append(record)

    lines = ["# Transcoder live E2E", "",
             f"Envoy: {envoy_version()}  ", f"Run directory: `{run_dir}`", "",
             "| Client → backend | Model | Result | ok | FAIL | gap | fixed | Files |",
             "|---|---|---|---|---|---|---|---|"]
    details = []
    for (client, backend, model, resolved), run in runs.items():
        counts = collections.Counter(r["status"] for r in run["records"])
        failed = [r for r in run["records"] if r["status"] == "FAIL"]
        gaps = [r for r in run["records"] if r["status"] in ("gap", "fixed")]
        name = f"{client} → {backend}"
        model_text = model + (f" (resolved to {resolved})" if resolved else "")
        lines.append(f"| {name} | {model_text} | {'FAIL' if failed else 'PASS'} | {counts['ok']} | "
                     f"{counts['FAIL']} | {counts['gap']} | {counts['fixed']} | "
                     f"`{run['dir'].relative_to(run_dir) if run['dir'] != run_dir else '.'}` |")
        if failed or gaps:
            details.append(f"### {name}, {model_text}")
            for r in failed:
                details.append(f"- **FAIL** [{r['section']}] {r['check']}"
                               + (f": {r['detail']}" if r["detail"] else ""))
            for r in gaps:
                details.append(f"- {r['status']} {r['check']}"
                               + (f": {r['detail']}" if r["detail"] else ""))
            details.append("")

    if details:
        lines += ["", "## Failures and known gaps", ""] + details
    lines += ["", "## Evidence per run", "",
              "`envoy.yaml`, `envoy.log`, `access.log`, `stats.txt`, `results.jsonl`, the client's "
              "`unary.*` and `stream.*`, and the tap's `upstream_*` (the transcoded request and the "
              "raw backend response)."]
    report = run_dir / "report.md"
    report.write_text("\n".join(lines) + "\n")
    print(f"Report: {report}")


if __name__ == "__main__":
    main()
