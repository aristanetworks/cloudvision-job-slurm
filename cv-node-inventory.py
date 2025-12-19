#!/usr/bin/env python3
# Copyright (c) 2025 Arista Networks, Inc.
# Use of this source code is governed by the Apache License 2.0
# that can be found in the LICENSE file.
"""Monitor Slurm node inventory changes and update CloudVision NodeConfig.

This script continuously monitors the Slurm cluster for node additions or deletions
and automatically updates CloudVision NodeConfig API:
- For added nodes: collects interface data and sends NodeConfig
- For removed nodes: sends NodeConfig delete requests
"""

import argparse
import json
import logging
import os
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Dict, List, Set

# Import shared CloudVision API utilities
try:
    from cv_api import send_nodeconfig, delete_nodeconfig
except ImportError:
    send_nodeconfig = None
    delete_nodeconfig = None

# ============================================================================
# Configuration - EDIT THESE VALUES
# ============================================================================
API_SERVER = ""  # e.g., "www.arista.io"
API_TOKEN = ""  # CloudVision API token

# ============================================================================
# Optional Configuration
# ============================================================================
POLL_INTERVAL = 60  # Seconds between checks
IFACE_NAME_REGEX = r"^(eth|eno|ens|enp|em).*"  # Regex matching common physical interface prefixes
INTERFACE_DISCOVERY_JOB_NAME = "cv-interface-discovery"  # Job name for srun interface discovery job

# Setup logging
logger = logging.getLogger("cv-node-monitor")


def setup_logging(debug: bool = False) -> None:
    """Configure logging based on debug flag."""
    level = logging.DEBUG if debug else logging.INFO
    logging.basicConfig(
        level=level,
        format="%(asctime)s - %(levelname)s - %(message)s",
        stream=sys.stdout,
    )
    logger.setLevel(level)


def get_cluster_name() -> str:
    """Get Slurm cluster name from scontrol.

    Uses 'scontrol show config' to get ClusterName from Slurm configuration.
    Aborts execution if cluster name cannot be determined.

    Returns:
        Cluster name from Slurm configuration

    Raises:
        SystemExit: If cluster name cannot be determined
    """
    try:
        result = subprocess.run(
            ["scontrol", "show", "config"],
            check=True,
            capture_output=True,
            text=True,
        )
        # Parse output for "ClusterName = <name>"
        for line in result.stdout.split("\n"):
            if line.strip().startswith("ClusterName"):
                # Format: "ClusterName = cluster_name" or "ClusterName=cluster_name"
                parts = line.split("=", 1)
                if len(parts) == 2:
                    cluster_name = parts[1].strip()
                    if cluster_name:
                        logger.debug("Found cluster name from scontrol: %s",
                                     cluster_name)
                        return cluster_name
    except subprocess.CalledProcessError as exc:
        logger.error("Failed to get cluster name from scontrol: %s", exc)
        logger.error(
            "Make sure Slurm is properly configured and scontrol is available")
        sys.exit(1)
    except Exception as exc:
        logger.error("Error parsing cluster name: %s", exc)
        sys.exit(1)

    # If we get here, ClusterName was not found in scontrol output
    logger.error("ClusterName not found in 'scontrol show config' output")
    logger.error("Please ensure ClusterName is set in slurm.conf")
    sys.exit(1)


def get_all_nodes() -> Set[str]:
    """Get set of all Slurm nodes (both available and unavailable).

    Returns:
        Set of node names
    """
    try:
        result = subprocess.run(
            ["sinfo", "-h", "-N", "-o", "%n"],
            check=True,
            capture_output=True,
            text=True,
        )
    except subprocess.CalledProcessError as exc:
        logger.error("Failed to run sinfo: %s", exc)
        return set()

    nodes = set()
    for line in result.stdout.strip().split("\n"):
        if line:
            nodes.add(line.strip())

    return nodes


def get_available_nodes() -> tuple[List[str], List[str]]:
    """Get list of available and unavailable Slurm nodes.

    Returns:
        Tuple of (available_nodes, unavailable_nodes)
    """
    try:
        result = subprocess.run(
            ["sinfo", "-h", "-N", "-o", "%n %T"],
            check=True,
            capture_output=True,
            text=True,
        )
    except subprocess.CalledProcessError as exc:
        logger.error("Failed to run sinfo: %s", exc)
        return [], []

    available = []
    unavailable = []
    # States where nodes can run jobs (case-insensitive)
    available_states = {"idle", "allocated", "mixed", "completing"}

    logger.debug("sinfo output:\n%s", result.stdout)

    for line in result.stdout.strip().split("\n"):
        if not line:
            continue
        parts = line.split()
        if len(parts) >= 2:
            node_name, state = parts[0], parts[1]
            # Normalize state to lowercase for comparison
            state_lower = state.lower()
            logger.debug("Node: %s, State: '%s' (lowercase: '%s')", node_name,
                         state, state_lower)
            if state_lower in available_states:
                available.append(node_name)
            else:
                unavailable.append(node_name)
                logger.warning(
                    "Node %s has state '%s' (not in available states: %s)",
                    node_name, state, available_states)

    # Remove duplicates and sort
    available = sorted(set(available))
    unavailable = sorted(set(unavailable))

    return available, unavailable


def load_worker_script() -> str:
    """Load the worker script from interface_discovery.py.

    Returns:
        Worker script content as string
    """
    script_dir = Path(__file__).parent
    worker_script_path = script_dir / "interface_discovery.py"
    try:
        with open(worker_script_path, "r") as f:
            return f.read()
    except FileNotFoundError:
        logger.error("Worker script not found: %s", worker_script_path)
        sys.exit(1)
    except Exception as exc:
        logger.error("Failed to load worker script: %s", exc)
        sys.exit(1)


def collect_from_nodes(nodes: List[str],
                       cluster_name: str,
                       debug: bool = False) -> List[Dict[str, Any]]:
    """Collect interface data from specific nodes using srun.

    Args:
        nodes: List of node names to collect from
        cluster_name: Slurm cluster name to pass to worker script
        debug: Whether to enable debug logging

    Returns:
        List of node data dictionaries
    """
    if not nodes:
        logger.warning("No nodes to collect from")
        return []

    logger.info("Collecting interface data from %d node(s)...", len(nodes))

    # Set environment variables for worker script
    env = os.environ.copy()
    env["LOG_LEVEL"] = "DEBUG" if debug else "INFO"
    env["IFACE_NAME_REGEX"] = IFACE_NAME_REGEX
    env["SLURM_CLUSTER_NAME"] = cluster_name

    # Load worker script
    worker_script = load_worker_script()

    # Run srun to execute worker script on specified nodes in parallel
    node_list = ",".join(nodes)
    cmd = [
        "srun",
        "--job-name",
        INTERFACE_DISCOVERY_JOB_NAME,
        "--nodes",
        str(len(nodes)),
        "--ntasks",
        str(len(nodes)),
        "--ntasks-per-node",
        "1",
        "--nodelist",
        node_list,
        "--oversubscribe",
        "--immediate",
        "python3",
        "-c",
        worker_script,
    ]

    try:
        result = subprocess.run(
            cmd,
            check=True,
            capture_output=True,
            text=True,
            env=env,
        )
    except subprocess.CalledProcessError as exc:
        logger.error("srun failed: %s", exc)
        logger.error("stderr: %s", exc.stderr)
        return []

    # Parse JSON output from each node
    node_data_list = []
    for line in result.stdout.strip().split("\n"):
        if not line:
            continue
        try:
            node_data = json.loads(line)
            node_data_list.append(node_data)
        except json.JSONDecodeError as exc:
            logger.warning("Failed to parse JSON from node: %s", exc)
            logger.debug("Line: %s", line)

    logger.info("Successfully collected data from %d node(s)",
                len(node_data_list))
    return node_data_list


def send_nodeconfig_for_node(node_data: Dict[str, Any]) -> bool:
    """Send NodeConfig to CloudVision API.

    Args:
        node_data: Dictionary with node_name, hostname, location, and interfaces

    Returns:
        True if successful, False otherwise
    """
    if send_nodeconfig is None:
        logger.error("cv_api module not available; cannot send NodeConfig")
        return False

    if not API_SERVER or not API_TOKEN:
        logger.error("API_SERVER and API_TOKEN must be configured")
        return False

    node_name = node_data.get("node_name")
    location = node_data.get("location")
    interfaces = node_data.get("interfaces", [])

    if not node_name:
        logger.error("Missing node_name in node_data")
        return False

    # Call shared cv_api function
    return send_nodeconfig(
        api_server=API_SERVER,
        api_token=API_TOKEN,
        node_name=node_name,
        location=location,
        interfaces=interfaces,
    )


def delete_nodeconfig_for_node(node_name: str) -> bool:
    """Delete NodeConfig from CloudVision API.

    Args:
        node_name: Name of the node to delete

    Returns:
        True if successful, False otherwise
    """
    if delete_nodeconfig is None:
        logger.error("cv_api module not available; cannot delete NodeConfig")
        return False

    if not API_SERVER or not API_TOKEN:
        logger.error("API_SERVER and API_TOKEN must be configured")
        return False

    # Call shared cv_api function
    return delete_nodeconfig(
        api_server=API_SERVER,
        api_token=API_TOKEN,
        node_name=node_name,
    )


def monitor_nodes(poll_interval: int, debug: bool = False) -> None:
    """Monitor Slurm nodes and update CloudVision on changes.

    Args:
        poll_interval: Seconds between checks
        debug: Whether to enable debug logging
    """
    logger.info("Starting Slurm node monitor (poll interval: %d seconds)",
                poll_interval)

    cluster_name = get_cluster_name()
    logger.info("Cluster name: %s", cluster_name)

    # Get initial node list and states
    previous_nodes = get_all_nodes()
    previous_available_nodes = set()

    if not previous_nodes:
        logger.warning("No nodes found in initial scan")
    else:
        logger.info("Initial node count: %d", len(previous_nodes))
        logger.debug("Initial nodes: %s", sorted(previous_nodes))

        # Run initial inventory for all available nodes
        logger.info("Running initial node inventory for all nodes...")
        available_nodes, unavailable_nodes = get_available_nodes()
        previous_available_nodes = set(available_nodes)

        if unavailable_nodes:
            logger.info("Unavailable nodes (%d): %s", len(unavailable_nodes),
                        ", ".join(unavailable_nodes))

        if available_nodes:
            logger.info("Collecting from %d available nodes...",
                        len(available_nodes))
            node_data_list = collect_from_nodes(available_nodes, cluster_name,
                                                debug)

            # Send NodeConfig for all nodes
            success_count = 0
            failure_count = 0
            for node_data in node_data_list:
                if send_nodeconfig_for_node(node_data):
                    success_count += 1
                else:
                    failure_count += 1

            logger.info("Initial inventory: %d succeeded, %d failed",
                        success_count, failure_count)

    # Monitor loop
    while True:
        time.sleep(poll_interval)

        current_nodes = get_all_nodes()
        current_available_nodes, _ = get_available_nodes()
        current_available_set = set(current_available_nodes)

        # Detect node list changes (added/removed)
        added_nodes = current_nodes - previous_nodes
        removed_nodes = previous_nodes - current_nodes

        # Detect state changes (nodes that became available)
        newly_available_nodes = current_available_set - previous_available_nodes

        # Nodes to update: newly added nodes that are available OR nodes that became available
        nodes_to_update = (added_nodes
                           & current_available_set) | newly_available_nodes

        if not added_nodes and not removed_nodes and not newly_available_nodes:
            logger.debug("No node or state changes detected")
            previous_nodes = current_nodes
            previous_available_nodes = current_available_set
            continue

        if added_nodes:
            logger.info("Detected %d new node(s): %s", len(added_nodes),
                        ", ".join(sorted(added_nodes)))

        if newly_available_nodes:
            logger.info("Detected %d node(s) became available: %s",
                        len(newly_available_nodes),
                        ", ".join(sorted(newly_available_nodes)))

        if nodes_to_update:
            # Collect and send NodeConfig for nodes that need updating
            nodes_to_update_list = sorted(list(nodes_to_update))
            logger.info("Updating %d node(s)...", len(nodes_to_update_list))
            node_data_list = collect_from_nodes(nodes_to_update_list,
                                                cluster_name, debug)

            success_count = 0
            failure_count = 0
            for node_data in node_data_list:
                if send_nodeconfig_for_node(node_data):
                    success_count += 1
                else:
                    failure_count += 1

            logger.info("Updated nodes: %d succeeded, %d failed",
                        success_count, failure_count)

        if removed_nodes:
            logger.info("Detected %d removed node(s): %s", len(removed_nodes),
                        ", ".join(sorted(removed_nodes)))

            # Delete NodeConfig for removed nodes
            success_count = 0
            failure_count = 0
            for node_name in sorted(removed_nodes):
                if delete_nodeconfig_for_node(node_name):
                    success_count += 1
                else:
                    failure_count += 1

            logger.info("Deleted nodes: %d succeeded, %d failed",
                        success_count, failure_count)

        # Update previous state
        previous_nodes = current_nodes
        previous_available_nodes = current_available_set


def run_once(debug: bool = False) -> None:
    """Run node inventory collection once and exit.

    Args:
        debug: Whether to enable debug logging
    """
    logger.info("Running one-time node inventory collection...")

    cluster_name = get_cluster_name()
    logger.info("Cluster name: %s", cluster_name)

    # Get available nodes
    available_nodes, unavailable_nodes = get_available_nodes()

    if unavailable_nodes:
        logger.info("Unavailable nodes (%d): %s", len(unavailable_nodes),
                    ", ".join(unavailable_nodes))

    if not available_nodes:
        logger.warning("No available nodes found")
        return

    logger.info("Collecting from %d available nodes...", len(available_nodes))
    node_data_list = collect_from_nodes(available_nodes, cluster_name, debug)

    # Send NodeConfig for all nodes
    success_count = 0
    failure_count = 0
    for node_data in node_data_list:
        if send_nodeconfig_for_node(node_data):
            success_count += 1
        else:
            failure_count += 1

    logger.info("Inventory complete: %d succeeded, %d failed", success_count,
                failure_count)

    if failure_count > 0:
        sys.exit(1)


def main() -> None:
    """Main entry point."""
    parser = argparse.ArgumentParser(
        description=
        "Collect Slurm node inventory and update CloudVision NodeConfig")
    parser.add_argument(
        "-v",
        "--debug",
        action="store_true",
        help="Enable debug logging",
    )
    parser.add_argument(
        "--monitor",
        action="store_true",
        help=
        "Run in continuous monitoring mode (monitors node add/delete events)",
    )
    parser.add_argument(
        "--poll-interval",
        type=int,
        default=POLL_INTERVAL,
        help=
        f"Seconds between node checks in monitor mode (default: {POLL_INTERVAL})",
    )
    args = parser.parse_args()

    setup_logging(debug=args.debug)

    # Verify API configuration
    if not API_SERVER or not API_TOKEN:
        logger.error(
            "API_SERVER and API_TOKEN must be configured in the script")
        logger.error("Edit %s and set API_SERVER and API_TOKEN values",
                     __file__)
        sys.exit(1)

    try:
        if args.monitor:
            monitor_nodes(args.poll_interval, debug=args.debug)
        else:
            run_once(debug=args.debug)
    except KeyboardInterrupt:
        logger.info("Received interrupt signal, shutting down...")
        sys.exit(0)


if __name__ == "__main__":
    main()
