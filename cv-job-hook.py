#!/usr/bin/env python3
# Copyright (c) 2025 Arista Networks, Inc.
# Use of this source code is governed by the Apache License 2.0
# that can be found in the LICENSE file.
"""CloudVision Slurm job hook to send JobConfig updates.

This script is intended to be used as both PrologSlurmctld and EpilogSlurmctld.
It reports job lifecycle state and the list of nodes allocated to the job
using the CloudVision JobConfig API.
"""

import json
import logging
import os
import re
import sys
from datetime import datetime, timezone

# Import shared CloudVision API utilities
try:
    from cv_api import send_jobconfig
except ImportError:
    send_jobconfig = None

# ============================================================================
# CloudVision JobConfig Configuration (REQUIRED - EDIT BEFORE USE)
# ============================================================================
# Set these values to match your CloudVision deployment.
# See https://aristanetworks.github.io/cloudvision-apis/connecting for details
# on obtaining API_SERVER and API_TOKEN.

API_SERVER = ""  # e.g., "www.arista.io"
API_TOKEN = ""  # CloudVision API token with JobConfig write permissions

# ============================================================================
# Optional Configuration
# ============================================================================
LOG_FILE = os.environ.get(
    "CV_LOG_FILE", "/var/log/slurm/cvjob.log"
)  # Log file path (configurable via CV_LOG_FILE env var)
LOG_LEVEL = "INFO"  # Log verbosity: "DEBUG", "INFO", "WARNING", "ERROR"
LOG_LEVEL_NUM = getattr(logging, LOG_LEVEL.upper(), logging.INFO)

# Job Filtering Configuration
# By default, exclude CloudVision internal jobs (those starting with "cv-")
JOB_NAME_FILTER = r"^(?!cv-)"  # Negative lookahead: exclude jobs starting with "cv-"
PARTITION_FILTER = None  # e.g., ["gpu", "compute"] to only report jobs in these partitions

logging.basicConfig(
    level=LOG_LEVEL_NUM,
    format="%(asctime)s - %(levelname)s - %(message)s",
    handlers=[
        logging.FileHandler(LOG_FILE),
        logging.StreamHandler(sys.stdout),
    ],
)
logger = logging.getLogger("slurmjob")


def parse_nodelist(nodelist: str):
    """Expand a Slurm nodelist expression into a flat list of node names.

    Examples:
      "node1" -> ["node1"]
      "node[1-3,5]" -> ["node1", "node2", "node3", "node5"].
    """
    if not nodelist:
        return []
    if "[" not in nodelist:
        return [nodelist]
    m = re.match(r"([A-Za-z0-9_-]+)\[([\d,-]+)\]", nodelist)
    if not m:
        return [nodelist]
    prefix, ranges = m.group(1), m.group(2)
    nodes = []
    for part in ranges.split(","):
        part = part.strip()
        if not part:
            continue
        if "-" in part:
            a, b = part.split("-", 1)
            try:
                start, end = int(a), int(b)
            except ValueError:
                nodes.append(f"{prefix}{part}")
                continue
            for i in range(start, end + 1):
                nodes.append(f"{prefix}{i}")
        else:
            nodes.append(f"{prefix}{part}")
    return nodes


def convert_timestamp(ts: str):
    """Convert a Unix timestamp string to an ISO 8601 UTC timestamp.

    Returns None if the input is missing or cannot be parsed.
    """
    try:
        t = int(ts)
    except (TypeError, ValueError):
        return None
    return datetime.fromtimestamp(
        t, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def determine_job_state(context: str) -> str:
    """Derive CloudVision job state from Slurm context and exit codes.

    In prolog context, returns JOB_STATE_RUNNING. In epilog context, inspects
    SLURM_JOB_EXIT_CODE / SLURM_JOB_EXIT_CODE2 to distinguish COMPLETED,
    FAILED, and CANCELLED. For any other context, returns JOB_STATE_UNKNOWN.
    """
    if context == "prolog_slurmctld":
        return "JOB_STATE_RUNNING"
    if context != "epilog_slurmctld":
        return "JOB_STATE_UNKNOWN"
    exit_code_str = os.environ.get("SLURM_JOB_EXIT_CODE")
    exit_code2_str = os.environ.get("SLURM_JOB_EXIT_CODE2")
    derived_ec_str = os.environ.get("SLURM_JOB_DERIVED_EC")
    exit_code = signal = None
    if exit_code2_str and ":" in exit_code2_str:
        a, b = exit_code2_str.split(":", 1)
        try:
            exit_code = int(a)
        except ValueError:
            pass
        try:
            signal = int(b)
        except ValueError:
            pass
    elif exit_code_str is not None:
        try:
            exit_code = int(exit_code_str)
        except ValueError:
            pass
    state = "JOB_STATE_COMPLETED"
    if exit_code is not None or signal is not None:
        if exit_code == 0 and signal and signal != 0:
            state = "JOB_STATE_CANCELLED"
        elif exit_code is not None and exit_code != 0:
            state = "JOB_STATE_FAILED"
    logger.info(
        "Job exit_code=%s exit_code2=%s derived_ec=%s -> state=%s",
        exit_code_str,
        exit_code2_str,
        derived_ec_str,
        state,
    )
    return state


def build_job_data():
    """Construct the JobConfig payload from Slurm environment variables.

    Reads SLURM_* variables provided by PrologSlurmctld/EpilogSlurmctld,
    validates required fields, applies filtering rules, and returns a dict
    matching the CloudVision computejob.v1.JobConfig schema.

    Returns None if validation fails or job is filtered out.
    """
    job_id = os.environ.get("SLURM_JOB_ID", "")
    cluster_name = os.environ.get("SLURM_CLUSTER_NAME", "")
    partition = os.environ.get("SLURM_JOB_PARTITION", "")
    start_time = os.environ.get("SLURM_JOB_START_TIME", "")
    job_name = os.environ.get("SLURM_JOB_NAME", "")
    nodelist = os.environ.get("SLURM_JOB_NODELIST", "")
    context = os.environ.get("SLURM_SCRIPT_CONTEXT", "")

    # Convert and parse fields
    start_iso = convert_timestamp(start_time)
    state = determine_job_state(context)
    nodes = parse_nodelist(nodelist)

    # Location is just the cluster name
    location = cluster_name

    # Validate all required fields are non-empty
    missing_fields = []
    if not job_id:
        missing_fields.append("job_id")
    if not location:
        missing_fields.append("location")
    if not job_name:
        missing_fields.append("job_name")
    if not start_iso:
        missing_fields.append("start_time")
    if not nodes:
        missing_fields.append("nodes")
    if state == "JOB_STATE_UNKNOWN":
        missing_fields.append("state (invalid context)")

    if missing_fields:
        logger.error("Job %s (%s): Missing or invalid required fields: %s",
                     job_id or "unknown", job_name or "unknown",
                     ", ".join(missing_fields))
        return None

    # Apply job name regex filter (filter on original job_name, not formatted)
    if JOB_NAME_FILTER:
        try:
            if not re.match(JOB_NAME_FILTER, job_name):
                logger.info(
                    "Job %s (%s): Filtered out - name does not match filter",
                    job_id, job_name)
                return None
        except re.error as e:
            logger.error("Invalid JOB_NAME_FILTER pattern '%s': %s",
                         JOB_NAME_FILTER, e)

    # Apply partition filter
    if PARTITION_FILTER and partition:
        if partition not in PARTITION_FILTER:
            logger.info(
                "Job %s (%s): Filtered out - partition '%s' not in filter: %s",
                job_id, job_name, partition, PARTITION_FILTER)
            return None

    # Extract optional end_time
    end_time = os.environ.get("SLURM_JOB_END_TIME", "")
    end_iso = convert_timestamp(end_time) if end_time else None

    # Add partition to job name sent to CV
    job_name_with_partition = f"{job_name}@{partition}" if job_name and partition else job_name

    # Build payload
    data = {
        "key": {
            "id": str(job_id),
            "location": location
        },
        "name": job_name_with_partition,
        "state": state,
        "start_time": start_iso,
        "nodes": {
            "values": nodes
        },
    }

    # Only include end_time if job is completed/failed/cancelled,
    # otherwise the end_time is job expiration time which is not what we want.
    if state in ("JOB_STATE_COMPLETED", "JOB_STATE_FAILED",
                 "JOB_STATE_CANCELLED"):
        if end_iso:
            data["end_time"] = end_iso
        else:
            logger.error(
                "Job %s (%s): Missing end_time for terminal state: %s", job_id,
                job_name, state)
            return None

    logger.debug("Job %s (%s): JobConfig payload: %s", job_id, job_name,
                 json.dumps(data, indent=2))
    return data


def send_job_data(job_data) -> bool:
    """POST the JobConfig payload to the CloudVision JobConfig REST endpoint.

    Returns True on HTTP 2xx, and False on any error or non-success status.
    Does not raise exceptions; all failures are logged and treated as
    non-fatal from the Slurm scheduler's perspective.
    """
    if send_jobconfig is None:
        logger.error("api_utils module not available; cannot send JobConfig")
        return False

    job_id = job_data.get("key", {}).get("id", "unknown")
    job_name = job_data.get("name", "unknown")
    location = job_data.get("key", {}).get("location", "slurm")
    state = job_data.get("state", "")
    nodes = job_data.get("nodes", {}).get("values", [])
    start_time = job_data.get("start_time", "")
    end_time = job_data.get("end_time")

    # Call shared cv_api function
    return send_jobconfig(
        api_server=API_SERVER,
        api_token=API_TOKEN,
        job_id=job_id,
        job_name=job_name,
        location=location,
        job_state=state,
        nodes=nodes,
        start_time=start_time,
        end_time=end_time,
        jobconfig_mode='node',  # Slurm uses node mode
    )


def main() -> int:
    """Entry point for Slurm PrologSlurmctld/EpilogSlurmctld hooks.

    Builds and sends a JobConfig update to CloudVision if API settings are
    configured. All failures are logged but treated as non-fatal so that Slurm
    job execution is never blocked by monitoring.
    """
    logger.debug("SLURM environment: %s", {
        k: v
        for k, v in os.environ.items() if k.startswith("SLURM_")
    })
    if not API_SERVER or not API_TOKEN:
        logger.warning(
            "CloudVision API is not configured (API_SERVER or API_TOKEN empty)."
        )
        return 0
    job_data = build_job_data()

    # Check if validation failed or job was filtered out
    if job_data is not None:
        send_job_data(job_data)

    return 0


if __name__ == "__main__":
    sys.exit(main())
