# Copyright (c) 2025 Arista Networks, Inc.
# Use of this source code is governed by the Apache License 2.0
# that can be found in the LICENSE file.
"""CloudVision API client helpers for JobConfig and NodeConfig.

This module centralizes the HTTP calls and payload building for CloudVision
JobConfig and NodeConfig resources.
"""

import json
import logging
from typing import Any, Dict, List, Optional

import requests
from urllib3.exceptions import InsecureRequestWarning

# Suppress SSL warnings since we're using verify=False in case of on-prem cvp
requests.packages.urllib3.disable_warnings(InsecureRequestWarning)

logger = logging.getLogger(__name__)

# CloudVision API endpoint paths
JOBCONFIG_ENDPOINT = "/api/resources/computejob/v1/JobConfig"
NODECONFIG_ENDPOINT = "/api/resources/computejob/v1/NodeConfig"


def _build_auth_headers(api_token: str) -> Dict[str, str]:
    """Build standard CloudVision auth headers for a bearer token."""
    return {
        "accept": "application/json",
        "Content-Type": "application/json",
        "Authorization": f"Bearer {api_token}",
    }


def send_jobconfig(api_server: str,
                   api_token: str,
                   job_id: str,
                   job_name: str,
                   location: str,
                   job_state: str,
                   start_time: str,
                   end_time: Optional[str] = None,
                   nodes: Optional[List[str]] = None,
                   interfaces: Optional[List[str]] = None,
                   jobconfig_mode: str = 'interface',
                   isTenantJob: bool = False) -> bool:
    """Build and send a JobConfig payload to CloudVision.

    Args:
        api_server: CloudVision API server address (e.g., "www.arista.io")
        api_token: CloudVision API bearer token
        job_id: Unique job identifier (job UID)
        job_name: Name of the job
        location: Job location identifier (e.g., cluster name, namespace, etc.)
        job_state: Job state (JOB_STATE_RUNNING, JOB_STATE_COMPLETED,
                   JOB_STATE_FAILED, JOB_STATE_CANCELLED)
        start_time: Job start time in ISO 8601 format (MANDATORY)
        end_time: Job end time in ISO 8601 format (optional)
        nodes: List of node IDs on which this job is running. Use when nodes are
               exclusively allocated to the job (all interfaces on the nodes used).
               Required when jobconfig_mode='node'.
        interfaces: List of compute node interface MAC addresses on which this job
                   is running. Use when only partial interfaces per compute node
                   are allocated to the job. Required when jobconfig_mode='interface'.
        jobconfig_mode: 'node' or 'interface' - determines which field to populate
        isTenantJob: If True, marks the job as a tenant type job
    Returns:
        True on HTTP 2xx, False otherwise.
    """
    if not api_server or not api_token:
        logger.debug("JobConfig API not configured, skipping API call")
        return False

    # Validate that we have resources to report
    if jobconfig_mode == 'interface':
        if not interfaces:
            logger.info(
                "[CV-API] Skipping JobConfig for job %s: no interfaces found",
                job_id)
            return False
    else:  # node mode
        if not nodes:
            logger.info(
                "[CV-API] Skipping JobConfig for job %s: no nodes found",
                job_id)
            return False

    # Build job data payload
    job_data = {
        "key": {
            "id": job_id
        },
        "name": job_name,
        "state": job_state,
        "start_time": start_time,
        "location": location,
    }

    # Add nodes or interfaces based on jobconfig_mode
    if jobconfig_mode == 'interface':
        job_data["interfaces"] = {"values": interfaces}
    else:
        job_data["nodes"] = {"values": nodes}

    # Validate end_time for terminal states
    if job_state in [
            "JOB_STATE_COMPLETED", "JOB_STATE_FAILED", "JOB_STATE_CANCELLED"
    ]:
        if not end_time:
            logger.error(
                "[CV-API] Missing end_time for job %s in terminal state %s",
                job_id, job_state)
            return False
        job_data["end_time"] = end_time

    # Add job type if this is a tenant type job used by a tenant scheduler
    if isTenantJob:
        job_data["type"] = "JOB_TYPE_TENANT"

    # Construct API endpoint
    api_endpoint = f"https://{api_server}{JOBCONFIG_ENDPOINT}"
    headers = _build_auth_headers(api_token)

    try:
        if jobconfig_mode == 'interface' and interfaces:
            count_info = f"interfaces={len(interfaces)}"
        else:
            count_info = f"nodes={len(nodes)}"
        logger.info(
            "[CV-API] Sending JobConfig: key=%s, name=%s, state=%s, %s, start_time=%s, end_time=%s",
            job_data.get("key"), job_name, job_state, count_info, start_time,
            end_time)
        logger.debug("API Request Payload: %s", job_data)

        response = requests.post(
            api_endpoint,
            headers=headers,
            json=job_data,
            verify=False,
            timeout=30,
        )

        response.raise_for_status()

        logger.debug(
            "Successfully sent job data to API. Response: %s",
            response.status_code,
        )
        logger.debug("API Response Body: %s", response.text)

        return True

    except requests.exceptions.RequestException as e:
        logger.error("Failed to send job data to API: %s", str(e))
        logger.error("Failed API Request Payload: %s", job_data)
        if getattr(e, "response", None) is not None:
            logger.error("Response status: %s", e.response.status_code)
            logger.error("Response body: %s", e.response.text)
        return False
    except Exception as e:  # noqa: BLE001
        logger.error("Unexpected error sending job data to API: %s", str(e))
        logger.error("Failed API Request Payload: %s", job_data)
        return False


def send_nodeconfig(api_server: str, api_token: str, node_name: str,
                    location: str, interfaces: List[Dict[str, Any]]) -> bool:
    """Build and send a NodeConfig payload to CloudVision.

    Args:
        api_server: CloudVision API server address (e.g., "www.arista.io")
        api_token: CloudVision API bearer token
        node_name: Name/ID of the node
        location: Location identifier (e.g., cluster name)
        interfaces: List of interface dicts with keys:
            - name: Interface name
            - mac_address: MAC address

    Returns:
        True on HTTP 2xx, False otherwise.
    """
    if not api_server or not api_token:
        logger.debug(
            "NodeConfig API not configured, skipping NodeConfig call for node %s",
            node_name)
        return False

    # Build nodeconfig payload
    payload = {
        "key": {
            "id": node_name,
        },
        "location": location,
        "hostname": node_name,
        "data_interfaces": {
            "values": [{
                "name": iface.get("name"),
                "mac_address": iface.get("mac_address"),
                "ip_addresses": {
                    "values": iface.get("ip_addresses") or [],
                },
            } for iface in interfaces],
        },
    }

    # Construct API endpoint
    api_endpoint = f"https://{api_server}{NODECONFIG_ENDPOINT}"
    headers = _build_auth_headers(api_token)

    try:
        # Extract and sort MAC addresses for logging
        mac_addresses = sorted(
            [iface.get("mac_address", "") for iface in interfaces])
        logger.info(
            "[CV-API] Sending NodeConfig for node %s with %d interfaces: %s",
            node_name,
            len(interfaces),
            mac_addresses,
        )
        logger.debug("API Request Payload: %s", payload)

        response = requests.post(
            api_endpoint,
            headers=headers,
            data=json.dumps(payload),
            timeout=10,
            verify=False,
        )

        response.raise_for_status()

        logger.debug(
            "Successfully sent NodeConfig to API. Response: %s",
            response.status_code,
        )

        return True

    except requests.exceptions.RequestException as e:
        logger.error("Failed to send NodeConfig to API for node %s: %s",
                     node_name, str(e))
        logger.error("Failed NodeConfig API Request Payload: %s", payload)
        if getattr(e, "response", None) is not None:
            logger.error("Response status: %s", e.response.status_code)
            logger.error("Response body: %s", e.response.text)
        return False
    except Exception as e:  # noqa: BLE001
        logger.error(
            "Unexpected error sending NodeConfig to API for node %s: %s",
            node_name,
            str(e),
        )
        logger.error("Failed NodeConfig API Request Payload: %s", payload)
        return False


def delete_nodeconfig(api_server: str, api_token: str, node_name: str) -> bool:
    """Delete a NodeConfig from CloudVision.

    Args:
        api_server: CloudVision API server address (e.g., "www.arista.io")
        api_token: CloudVision API bearer token
        node_name: Name/ID of the node to delete

    Returns:
        True on HTTP 2xx, False otherwise.
    """
    if not api_server or not api_token:
        logger.debug(
            "NodeConfig API not configured, skipping NodeConfig delete for node %s",
            node_name)
        return False

    # Construct API endpoint with key parameters
    api_endpoint = (f"https://{api_server}{NODECONFIG_ENDPOINT}"
                    f"?key.id={node_name}")
    headers = _build_auth_headers(api_token)

    try:
        logger.info(
            "[CV-API] Deleting NodeConfig for node %s",
            node_name,
        )

        response = requests.delete(
            api_endpoint,
            headers=headers,
            timeout=10,
            verify=False,
        )

        response.raise_for_status()

        logger.debug(
            "Successfully deleted NodeConfig from API. Response: %s",
            response.status_code,
        )

        return True

    except requests.exceptions.RequestException as e:
        logger.error("Failed to delete NodeConfig from API for node %s: %s",
                     node_name, str(e))
        if getattr(e, "response", None) is not None:
            logger.error("Response status: %s", e.response.status_code)
            logger.error("Response body: %s", e.response.text)
        return False
    except Exception as e:  # noqa: BLE001
        logger.error(
            "Unexpected error deleting NodeConfig from API for node %s: %s",
            node_name,
            str(e),
        )
        return False
