"""Scan every provider, dedupe, and aggregate. Port of the scan loop in T3's UsageService.ts."""

from __future__ import annotations

import json
from typing import Callable, Dict, List, Optional, Sequence

from .aggregate import Aggregator
from .model import PROVIDERS, Record
from .readers import Env, all_readers
from .scan_cache import ScanCache
from .transcripts import FileScan, SourceScan

MALFORMED = "{n} malformed or inconsistent usage record(s) were skipped; usage may be incomplete."


def shared_sessions(files: Sequence[FileScan], providers_with_sharing: set) -> set:
    """Sessions that appear in more than one file (a rollout moved after it was read)."""
    first_file: Dict[str, int] = {}
    shared = set()
    for index, scan in enumerate(files):
        previous = ""
        for rec in scan.records:
            if rec.provider not in providers_with_sharing or rec.session == previous or not rec.session:
                continue
            previous = rec.session
            first = first_file.get(rec.session)
            if first is None:
                first_file[rec.session] = index
            elif first != index:
                shared.add(rec.session)
    return shared


def run_scan(
    aggregator: Aggregator,
    env: Env,
    window_start_ms: float,
    providers: Optional[Sequence[str]] = None,
    progress: Optional[Callable[[str], None]] = None,
    home_label: Optional[str] = None,
    cache: Optional[ScanCache] = None,
) -> List[dict]:
    """Scan the selected providers into `aggregator`; return one source report per directory.

    `cache` (optional) reuses the parsed records of unchanged transcripts; output is identical
    with or without it.
    """
    readers = all_readers()
    selected = [p for p in PROVIDERS if providers is None or p in providers]
    scanned: List[SourceScan] = []
    for provider in selected:
        if progress:
            progress(f"Scanning {provider}...")
        try:
            scanned.extend(readers[provider](env, window_start_ms, cache))
        except Exception as error:  # one broken reader must not hide the others
            scanned.append(SourceScan(provider, "", [], "failed", f"{provider} could not be read: {type(error).__name__}"))

    from .readers import codex

    sharing = {codex.FORMAT.provider} if codex.FORMAT.shared_sessions else set()
    all_files = [scan for source in scanned for scan in (source.files or [])]
    crossing = shared_sessions(all_files, sharing)

    reports: List[dict] = []
    for source in scanned:
        scanned_files = skipped = malformed = 0
        sessions: set = set()
        for file in source.files or []:
            malformed += file.malformed
            if not file.records:
                skipped += 1
                continue
            scanned_files += 1
            occurrences: Dict[str, int] = {}
            for rec in file.records:
                if rec.provider in sharing and rec.session in crossing:
                    # Dedupe moved copies but keep repeated equal events within one file.
                    key = json.dumps([rec.provider, rec.session, rec.ts_ms, rec.model,
                                      rec.uncached, rec.cached, rec.cache_write, rec.output, rec.reasoning])
                    count = occurrences.get(key, 0) + 1
                    occurrences[key] = count
                    rec = rec._replace(dedupe_key=f"{key}:{count}")
                if aggregator.add(rec) and rec.session:
                    sessions.add(rec.session)
        message = source.message
        if source.files is None:
            message = message or "No transcript directory on this machine."
        status = source.status
        if source.files is None and scanned_files == 0:
            status = "missing"
        elif malformed > 0 and status == "ok":
            status = "partial"
        if malformed > 0:
            message = " ".join(part for part in (message, MALFORMED.format(n=malformed)) if part)
        directory = source.directory
        if home_label and directory.startswith(env.home):
            directory = "~" + directory[len(env.home):]
        reports.append({
            "provider": source.provider,
            "path": directory,
            "status": status,
            "scannedFiles": scanned_files,
            "skippedFiles": skipped,
            "malformedRecords": malformed,
            "distinctSessions": len(sessions),
            "message": message,
        })
    return reports
