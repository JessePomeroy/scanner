#!/usr/bin/env python3
"""Initialize, update, and finalize a paired benchmark evidence report."""

from __future__ import annotations

import argparse
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))

from app.benchmark_evidence import (  # noqa: E402
    BenchmarkEvidenceError,
    append_stage,
    ensure_report_open,
    initialize_report,
    load_report,
    planned_stage,
    record_artifacts,
    refresh_summary,
    run_stage,
    runtime_guidance,
    write_report,
)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="action", required=True)

    init_parser = subparsers.add_parser("init", help="Verify input and create a report.")
    init_parser.add_argument("--scan", type=Path, required=True)
    init_parser.add_argument("--expected-sha256", required=True)
    init_parser.add_argument("--scanner-baseline-commit", required=True)
    init_parser.add_argument("--report", type=Path, required=True)
    init_parser.add_argument(
        "--skip-tool-probes",
        action="store_true",
        help="Used only for lightweight tests; benchmark runs should keep probes enabled.",
    )

    run_parser = subparsers.add_parser("run", help="Run or dry-run one named stage.")
    run_parser.add_argument("--report", type=Path, required=True)
    run_parser.add_argument("--stage", required=True)
    run_parser.add_argument("--log", type=Path, default=None)
    run_parser.add_argument("--estimated-seconds", type=float, default=None)
    run_parser.add_argument(
        "--estimate-confidence",
        choices=["low", "medium", "high"],
        default="low",
    )
    run_parser.add_argument("--artifact", action="append", default=[])
    run_parser.add_argument("--dry-run", action="store_true")
    run_parser.add_argument("command", nargs=argparse.REMAINDER)

    finalize_parser = subparsers.add_parser("finalize", help="Record final artifacts.")
    finalize_parser.add_argument("--report", type=Path, required=True)
    finalize_parser.add_argument("--artifact", action="append", default=[])

    args = parser.parse_args()
    try:
        if args.action == "init":
            if args.report.exists():
                parser.error(f"report already exists: {args.report}")
            probes = {} if args.skip_tool_probes else None
            kwargs = {}
            if probes is not None:
                kwargs["tool_probes"] = probes
            report = initialize_report(
                scan_path=args.scan,
                expected_sha256=args.expected_sha256,
                scanner_baseline_revision=args.scanner_baseline_commit,
                repo_root=ROOT,
                **kwargs,
            )
            write_report(args.report, report)
            print(f"Initialized benchmark report: {args.report}")
            return

        report = load_report(args.report)
        ensure_report_open(report)
        artifacts = parse_artifacts(args.artifact)
        if args.action == "finalize":
            if not artifacts:
                raise BenchmarkEvidenceError("Finalization requires at least one artifact")
            record_artifacts(report, artifacts)
            missing = [
                name
                for name in artifacts
                if report["artifacts"][name]["status"] != "present"
            ]
            if missing:
                raise BenchmarkEvidenceError(
                    "Cannot finalize with missing artifacts: " + ", ".join(missing)
                )
            refresh_summary(report, finalized=True)
            write_report(args.report, report)
            print(f"Finalized benchmark report: {args.report}")
            return

        command = list(args.command)
        if command and command[0] == "--":
            command = command[1:]
        if not command:
            parser.error("run requires a command after --")
        if args.estimated_seconds is not None:
            report["schedule"] = runtime_guidance(
                args.estimated_seconds,
                confidence=args.estimate_confidence,
            )
        if args.dry_run:
            stage = planned_stage(name=args.stage, command=command)
        else:
            log_path = args.log or args.report.parent / "logs" / f"{args.stage}.log"
            stage = run_stage(name=args.stage, command=command, log_path=log_path)
        append_stage(report, stage)
        record_artifacts(report, artifacts)
        write_report(args.report, report)
        print(f"Recorded stage {args.stage}: {stage['status']}")
        if stage["status"] == "failed":
            raise SystemExit(stage["return_code"] or 1)
    except BenchmarkEvidenceError as error:
        parser.error(str(error))


def parse_artifacts(values: list[str]) -> dict[str, Path]:
    artifacts: dict[str, Path] = {}
    for value in values:
        name, separator, path = value.partition("=")
        if not separator or not name or not path:
            raise BenchmarkEvidenceError("Artifacts must use NAME=PATH")
        if name in artifacts:
            raise BenchmarkEvidenceError(f"Duplicate artifact name: {name}")
        artifacts[name] = Path(path)
    return artifacts


if __name__ == "__main__":
    main()
