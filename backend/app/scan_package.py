"""Scan package preparation, validation, and reporting."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import shutil

from app.report_writer import write_scan_report
from app.scan_validator import ScanValidationReport, find_scan_root, validate_scan_package
from app.storage import safe_extract_zip


@dataclass(frozen=True)
class PreparedScanPackage:
    scan_root: Path
    validation: ScanValidationReport
    report_path: Path

    @property
    def scan_id(self) -> str:
        return self.validation.scan_id or self.scan_root.name


def scan_id_from_path(path: Path) -> str:
    """Derive a stable scan id from a zip or directory path."""
    name = path.name
    if name.lower().endswith(".zip"):
        name = name[:-4]
    return name.replace(" ", "_")


def prepare_scan_source(scan: Path, destination: Path, *, reset: bool = True) -> Path:
    """Copy or extract a scan package into destination and return its scan root."""
    if reset and destination.exists():
        shutil.rmtree(destination)

    destination.mkdir(parents=True, exist_ok=True)

    if scan.suffix.lower() == ".zip":
        safe_extract_zip(scan, destination)
    elif scan.is_dir():
        target = destination / scan.name
        if target.exists():
            shutil.rmtree(target)
        shutil.copytree(scan, target)
    else:
        raise ValueError(f"Scan path is not a zip or directory: {scan}")

    return find_scan_root(destination)


def validate_and_report_scan(scan_root: Path) -> PreparedScanPackage:
    """Validate a scan root and write its scan_report.json."""
    validation = validate_scan_package(scan_root)
    report_path = write_scan_report(scan_root, validation)
    return PreparedScanPackage(
        scan_root=scan_root,
        validation=validation,
        report_path=report_path,
    )
