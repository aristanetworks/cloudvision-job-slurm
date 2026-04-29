#!/usr/bin/env python3
# Copyright (c) 2025 Arista Networks, Inc.
# Use of this source code is governed by the Apache License 2.0
# that can be found in the LICENSE file.
"""Manually send a single Slurm job's JobConfig to CloudVision.

Run on the Slurm controller node to push a specific job to CloudVision
without relying on the PrologSlurmctld/EpilogSlurmctld hooks.

Job information is collected from Slurm via:
  - `scontrol show job <id>`  (for running / still-in-queue jobs)
  - `sacct -j <id>`           (fallback for completed/failed/cancelled jobs)

Usage:
    cv-job-send.py <job_id> --api-server www.arista.io --api-token <token>
    CV_API_SERVER=www.arista.io CV_API_TOKEN=xxx cv-job-send.py <job_id>
"""

import argparse
import json
import logging
import os
import re
import subprocess
import sys
from datetime import datetime, timezone
from typing import Dict, List, Optional, Tuple

try:
    from cv_api import JOBCONFIG_ENDPOINT, send_jobconfig
except ImportError:
    print(
        "ERROR: cv_api module not found. Run this script from the directory "
        "containing cv_api.py (e.g., /opt/slurm/cloudvision/).",
        file=sys.stderr,
    )
    sys.exit(2)

logger = logging.getLogger("cv-job-send")

# Manually-sent jobs are reported as JOB_TYPE_CONFIGURED so CloudVision can
# distinguish them from jobs published automatically by the prolog/epilog hook.
JOB_TYPE = "JOB_TYPE_CONFIGURED"

# Slurm state strings -> CloudVision JobConfig state.
# SUSPENDED is grouped with RUNNING because the job still holds its allocation.
# PENDING (and other states with no CloudVision equivalent) intentionally fall
# through to JOB_STATE_UNKNOWN, which the caller treats as an error and skips
# sending — CloudVision's JobConfig has no PENDING state.
_RUNNING_STATES = {"RUNNING", "COMPLETING", "SUSPENDED"}
_COMPLETED_STATES = {"COMPLETED"}
_FAILED_STATES = {
    "FAILED",
    "NODE_FAIL",
    "BOOT_FAIL",
    "OUT_OF_MEMORY",
    "DEADLINE",
    "TIMEOUT",
    "PREEMPTED",
    "LAUNCH_FAILED",
}
_CANCELLED_STATES = {"CANCELLED", "REVOKED"}
_TERMINAL_STATES = _COMPLETED_STATES | _FAILED_STATES | _CANCELLED_STATES


def parse_nodelist(nodelist: str) -> List[str]:
    """Expand a Slurm nodelist expression into a flat list of node names.

    Handles simple bracketed ranges like "node[1-3,5]" and comma-joined
    groups like "n1,n[2-3]". Non-numeric or unrecognized forms fall through
    as a single literal entry.
    """
    if not nodelist or nodelist in ("(null)", "None"):
        return []

    nodes: List[str] = []
    # Split top-level by commas that are NOT inside brackets.
    depth = 0
    buf = ""
    parts: List[str] = []
    for ch in nodelist:
        if ch == "[":
            depth += 1
        elif ch == "]":
            depth -= 1
        if ch == "," and depth == 0:
            parts.append(buf)
            buf = ""
        else:
            buf += ch
    if buf:
        parts.append(buf)

    for part in parts:
        part = part.strip()
        if not part:
            continue
        if "[" not in part:
            nodes.append(part)
            continue
        m = re.match(r"([A-Za-z0-9_-]+)\[([\d,\-]+)\]", part)
        if not m:
            nodes.append(part)
            continue
        prefix, ranges = m.group(1), m.group(2)
        for r in ranges.split(","):
            r = r.strip()
            if "-" in r:
                a, b = r.split("-", 1)
                try:
                    start, end = int(a), int(b)
                    width = len(a)
                except ValueError:
                    nodes.append(f"{prefix}{r}")
                    continue
                for i in range(start, end + 1):
                    nodes.append(f"{prefix}{str(i).zfill(width)}")
            elif r:
                nodes.append(f"{prefix}{r}")
    return nodes


def _local_naive_to_utc_iso(ts: str) -> Optional[str]:
    """Convert a Slurm local-time timestamp (YYYY-MM-DDTHH:MM:SS) to UTC ISO 8601.

    Returns None for placeholders like "Unknown", "N/A", "None", or unparseable
    input. Slurm prints times in the controller's local timezone without an
    offset; we attach the local tz then convert to UTC.
    """
    if not ts or ts in ("Unknown", "N/A", "None", "(null)"):
        return None
    try:
        naive = datetime.strptime(ts, "%Y-%m-%dT%H:%M:%S")
    except ValueError:
        logger.debug("Unparseable Slurm timestamp: %r", ts)
        return None
    local = naive.astimezone()  # attach local tz from system
    return local.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _map_state(slurm_state: str) -> str:
    """Map a Slurm job state string to a CloudVision JOB_STATE_* value."""
    if not slurm_state:
        return "JOB_STATE_UNKNOWN"
    # sacct can emit forms like "CANCELLED by 1000" or "COMPLETED+" (the '+'
    # suffix flags additional state info, e.g. job steps with non-zero exits).
    # Take the first token and strip the trailing '+' to get the bare state.
    head = slurm_state.split()[0].rstrip("+")
    if head in _RUNNING_STATES:
        return "JOB_STATE_RUNNING"
    if head in _COMPLETED_STATES:
        return "JOB_STATE_COMPLETED"
    if head in _CANCELLED_STATES:
        return "JOB_STATE_CANCELLED"
    if head in _FAILED_STATES:
        return "JOB_STATE_FAILED"
    return "JOB_STATE_UNKNOWN"


def get_cluster_name() -> str:
    """Return ClusterName from `scontrol show config`. Aborts on failure."""
    try:
        result = subprocess.run(
            ["scontrol", "show", "config"],
            check=True,
            capture_output=True,
            text=True,
        )
    except (subprocess.CalledProcessError, FileNotFoundError) as exc:
        logger.error("Failed to run 'scontrol show config': %s", exc)
        sys.exit(1)

    for line in result.stdout.splitlines():
        if line.strip().startswith("ClusterName"):
            parts = line.split("=", 1)
            if len(parts) == 2 and parts[1].strip():
                return parts[1].strip()

    logger.error("ClusterName not found in 'scontrol show config' output. "
                 "Set ClusterName in slurm.conf.")
    sys.exit(1)


def fetch_job_via_scontrol(job_id: str) -> Optional[Dict[str, str]]:
    """Return parsed scontrol fields for a job, or None if unknown.

    `scontrol show job` only knows jobs that are still tracked by slurmctld
    (pending, running, or recently completed within MinJobAge). Older jobs
    must be looked up via sacct.
    """
    try:
        result = subprocess.run(
            ["scontrol", "show", "job", job_id],
            capture_output=True,
            text=True,
            check=False,
        )
    except FileNotFoundError as exc:
        logger.error("scontrol not found: %s", exc)
        return None

    if result.returncode != 0:
        stderr = (result.stderr or "").strip()
        logger.debug("scontrol show job %s failed: %s", job_id, stderr)
        return None

    # scontrol prints space-separated Key=Value tokens across multiple lines.
    # Most values are whitespace-free; we parse them with a non-greedy regex.
    fields: Dict[str, str] = {}
    for key, value in re.findall(r"(\w+)=(\S*)", result.stdout):
        # Keep the first occurrence; some keys recur for steps but the first
        # is the top-level job entry.
        fields.setdefault(key, value)

    if not fields.get("JobId"):
        logger.debug("scontrol output had no JobId field for %s", job_id)
        return None

    return fields


def fetch_job_via_sacct(job_id: str) -> Optional[Dict[str, str]]:
    """Return parsed sacct fields for a job, or None if no record exists.

    Uses parsable (`-P`) output and selects only the top-level job row
    (JobID without `.batch`, `.extern`, or numeric step suffixes).
    """
    fmt = "JobID,JobName,State,Start,End,NodeList,Partition,Cluster"
    try:
        result = subprocess.run(
            [
                "sacct", "-j", job_id, "-P", "-n", "--format=" + fmt,
                "--parsable2"
            ],
            capture_output=True,
            text=True,
            check=False,
        )
    except FileNotFoundError as exc:
        logger.error("sacct not found: %s", exc)
        return None

    if result.returncode != 0:
        logger.error("sacct -j %s failed: %s", job_id,
                     (result.stderr or "").strip())
        return None

    columns = fmt.split(",")
    for line in result.stdout.splitlines():
        if not line.strip():
            continue
        values = line.split("|")
        if len(values) != len(columns):
            continue
        row = dict(zip(columns, values))
        # Match the parent row only (e.g. "12345"), not "12345.batch" / "12345.0".
        if row.get("JobID", "").split(".")[0] == str(job_id):
            if "." in row["JobID"]:
                continue
            return row
    return None


def build_from_scontrol(
        fields: Dict[str, str]) -> Tuple[Dict[str, object], List[str]]:
    """Build a normalized job record from scontrol output.

    Returns (record, errors). If errors is non-empty, record is incomplete
    and should not be sent.
    """
    errors: List[str] = []
    job_id = fields.get("JobId", "").strip()
    job_name = fields.get("JobName", "").strip()
    partition = fields.get("Partition", "").strip()
    nodelist = fields.get("NodeList", "").strip()
    state_raw = fields.get("JobState", "").strip()
    start = _local_naive_to_utc_iso(fields.get("StartTime", ""))
    end = _local_naive_to_utc_iso(fields.get("EndTime", ""))
    state = _map_state(state_raw)
    nodes = parse_nodelist(nodelist)

    if not job_id:
        errors.append("job_id")
    if not job_name:
        errors.append("job_name")
    if not start:
        errors.append("start_time")
    if not nodes:
        errors.append("nodes")
    if state == "JOB_STATE_UNKNOWN":
        errors.append(f"state (slurm state={state_raw or 'empty'})")

    return (
        {
            "job_id": job_id,
            "job_name": job_name,
            "partition": partition,
            "state": state,
            "start_time": start,
            "end_time": end,
            "nodes": nodes,
        },
        errors,
    )


def build_from_sacct(
        row: Dict[str, str]) -> Tuple[Dict[str, object], List[str]]:
    """Build a normalized job record from a single sacct row."""
    errors: List[str] = []
    job_id = row.get("JobID", "").strip()
    job_name = row.get("JobName", "").strip()
    partition = row.get("Partition", "").strip()
    nodelist = row.get("NodeList", "").strip()
    state_raw = row.get("State", "").strip()
    start = _local_naive_to_utc_iso(row.get("Start", ""))
    end = _local_naive_to_utc_iso(row.get("End", ""))
    state = _map_state(state_raw)
    nodes = parse_nodelist(nodelist)

    if not job_id:
        errors.append("job_id")
    if not job_name:
        errors.append("job_name")
    if not start:
        errors.append("start_time")
    if not nodes:
        errors.append("nodes")
    if state == "JOB_STATE_UNKNOWN":
        errors.append(f"state (slurm state={state_raw or 'empty'})")

    return (
        {
            "job_id": job_id,
            "job_name": job_name,
            "partition": partition,
            "state": state,
            "start_time": start,
            "end_time": end,
            "nodes": nodes,
            "cluster_from_sacct": row.get("Cluster", "").strip(),
        },
        errors,
    )


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=
        "Manually push a single Slurm job's JobConfig to CloudVision.",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument("job_id", help="Slurm job ID to report")
    parser.add_argument(
        "--api-server",
        default=os.environ.get("CV_API_SERVER", ""),
        help="CloudVision API server (env: CV_API_SERVER)",
    )
    parser.add_argument(
        "--api-token",
        default=os.environ.get("CV_API_TOKEN", ""),
        help="CloudVision API token (env: CV_API_TOKEN)",
    )
    parser.add_argument("-v",
                        "--verbose",
                        action="store_true",
                        help="Enable DEBUG logging")
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print the API URL and JSON payload to stdout without sending it. "
        "API token is not required in this mode.",
    )
    return parser.parse_args()


def _build_jobconfig_payload(job_id: str, job_name: str, location: str,
                             state: str, start_time: str,
                             end_time: Optional[str], nodes: List[str],
                             job_type: str) -> Dict[str, object]:
    """Build the JobConfig payload for node mode.

    Mirrors the shape produced by cv_api.send_jobconfig so dry-run output
    matches what would actually be sent.
    """
    payload: Dict[str, object] = {
        "key": {
            "id": job_id
        },
        "name": job_name,
        "state": state,
        "start_time": start_time,
        "location": location,
        "nodes": {
            "values": nodes
        },
        "type": job_type,
    }
    if end_time:
        payload["end_time"] = end_time
    return payload


def main() -> int:
    args = parse_args()
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s - %(levelname)s - %(message)s",
    )

    if not args.api_server:
        logger.error(
            "API server is required. Pass --api-server or set CV_API_SERVER.")
        return 2
    if not args.api_token and not args.dry_run:
        logger.error(
            "API token is required (omit only with --dry-run). "
            "Pass --api-token or set CV_API_TOKEN.")
        return 2

    cluster = get_cluster_name()
    logger.info("Cluster: %s", cluster)

    record: Optional[Dict[str, object]] = None
    errors: List[str] = []

    fields = fetch_job_via_scontrol(args.job_id)
    if fields:
        logger.info("Found job %s via scontrol", args.job_id)
        record, errors = build_from_scontrol(fields)
    else:
        row = fetch_job_via_sacct(args.job_id)
        if row:
            logger.info("Found job %s via sacct", args.job_id)
            record, errors = build_from_sacct(row)
        else:
            logger.error(
                "Job %s not found via sacct (accounting may be disabled or "
                "the job may not exist)", args.job_id)
            return 1

    assert record is not None
    if errors:
        logger.error("Job %s: missing/invalid fields: %s", args.job_id,
                     ", ".join(errors))
        return 1

    location = cluster
    job_name_with_partition = (f"{record['job_name']}@{record['partition']}"
                               if record["partition"] else record["job_name"])
    unique_job_id = f"{record['job_id']}@{location}"
    state = record["state"]
    cv_terminal = {
        "JOB_STATE_COMPLETED", "JOB_STATE_FAILED", "JOB_STATE_CANCELLED"
    }
    # Only include end_time for terminal states; for running jobs Slurm reports
    # EndTime as the job's projected expiration, which is not what CV wants.
    end_time = record["end_time"] if state in cv_terminal else None
    if state in cv_terminal and not end_time:
        logger.error("Job %s in terminal state %s but no end_time available",
                     args.job_id, state)
        return 1

    if args.dry_run:
        payload = _build_jobconfig_payload(
            job_id=unique_job_id,
            job_name=job_name_with_partition,
            location=location,
            state=state,
            start_time=record["start_time"],
            end_time=end_time,
            nodes=record["nodes"],
            job_type=JOB_TYPE,
        )
        url = f"https://{args.api_server}{JOBCONFIG_ENDPOINT}"
        print(f"POST {url}")
        print(json.dumps(payload, indent=2))
        logger.info(
            "[dry-run] Did not contact CloudVision (job %s, state=%s, nodes=%d)",
            args.job_id, state, len(record["nodes"]))
        return 0

    ok = send_jobconfig(
        api_server=args.api_server,
        api_token=args.api_token,
        job_id=unique_job_id,
        job_name=job_name_with_partition,
        location=location,
        job_state=state,
        start_time=record["start_time"],
        end_time=end_time,
        nodes=record["nodes"],
        jobconfig_mode="node",
        job_type=JOB_TYPE,
    )
    if not ok:
        logger.error("Failed to send JobConfig for job %s", args.job_id)
        return 1
    logger.info("Sent JobConfig for job %s (state=%s, nodes=%d)", args.job_id,
                state, len(record["nodes"]))
    return 0


if __name__ == "__main__":
    sys.exit(main())
