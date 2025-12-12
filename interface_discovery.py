#!/usr/bin/env python3
# Copyright (c) 2025 Arista Networks, Inc.
# Use of this source code is governed by the Apache License 2.0
# that can be found in the LICENSE file.
"""Worker script for collecting interface data on Slurm nodes.

This script is executed on worker nodes via srun to collect network interface
information (MAC addresses, IP addresses) and output as JSON.

Uses /sys/class/net (sysfs) for interface discovery instead of ip command.
"""

import fcntl
import json
import logging
import os
import re
import socket
import struct
import sys
from pathlib import Path
from typing import List, Optional, Dict, Any

# Logging configuration
LOG_LEVEL_NAME = os.environ.get("LOG_LEVEL", "INFO")
IFACE_NAME_REGEX = os.environ.get("IFACE_NAME_REGEX", "")
SYSFS_NET_PATH = Path("/sys/class/net")

LOG_LEVEL = getattr(logging, LOG_LEVEL_NAME.upper(), logging.INFO)
logging.basicConfig(
    level=LOG_LEVEL,
    format="%(asctime)s - %(levelname)s - %(message)s",
    stream=sys.stderr,
)
log = logging.getLogger("slurmnode")

# Get node name and cluster name from Slurm environment
NODE_NAME = os.environ.get("SLURMD_NODENAME")
if not NODE_NAME:
    log.error(
        "Missing node name (SLURMD_NODENAME not set); aborting data collection."
    )
    sys.exit(1)

CLUSTER_NAME = os.environ.get("SLURM_CLUSTER_NAME", "slurm")
HOSTNAME = socket.gethostname().split(".")[0]
log.info("Collecting interface data from node: %s (cluster: %s)", NODE_NAME,
         CLUSTER_NAME)


def get_interface_mac(interface_name: str) -> Optional[str]:
    """Get MAC address for a network interface from sysfs."""
    try:
        mac_path = SYSFS_NET_PATH / interface_name / "address"
        if mac_path.exists():
            mac = mac_path.read_text().strip().lower()
            # Filter out invalid MACs
            if mac and mac != "00:00:00:00:00:00":
                return mac
    except Exception as e:
        log.debug("Failed to read MAC for %s: %s", interface_name, e)
    return None


def get_interface_ip(interface_name: str) -> Optional[str]:
    """Get IPv4 address for a network interface using socket ioctl.

    This is more reliable than parsing /proc/net/fib_trie and doesn't
    require the ip command.
    """
    try:
        # Read interface operstate to check if it's up
        operstate_path = SYSFS_NET_PATH / interface_name / "operstate"
        if operstate_path.exists():
            operstate = operstate_path.read_text().strip()
            if operstate != "up":
                log.debug("Interface %s is not up (state: %s)", interface_name,
                          operstate)
                return None

        # Use ioctl to get IP address
        # SIOCGIFADDR = 0x8915 (get interface address)
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            # Pack interface name into struct
            ifreq = struct.pack('256s', interface_name[:15].encode('utf-8'))
            # Get IP address using ioctl
            result = fcntl.ioctl(sock.fileno(), 0x8915, ifreq)
            # Unpack the result to get IP address
            ip_addr = socket.inet_ntoa(result[20:24])

            # Validate it's a real IP (not 127.0.0.1, not 0.0.0.0)
            if ip_addr and ip_addr not in ("127.0.0.1", "0.0.0.0"):
                log.debug("Found IP %s for interface %s", ip_addr,
                          interface_name)
                return ip_addr
        finally:
            sock.close()

        log.debug("No IP address found for %s", interface_name)
        return None

    except OSError as e:
        # EADDRNOTAVAIL (99) or other errors mean no IP assigned
        log.debug("No IP for %s: %s", interface_name, e)
        return None
    except Exception as e:
        log.debug("Failed to get IP for %s: %s", interface_name, e)
        return None


def is_physical_interface(interface_name: str) -> bool:
    """Check if interface is a physical network interface.

    Physical interfaces have a 'device' symlink pointing to the PCI device.
    Virtual interfaces (veth, bridges, etc.) do not have this.
    """
    try:
        device_path = SYSFS_NET_PATH / interface_name / "device"
        return device_path.exists() and device_path.is_symlink()
    except Exception:
        return False


def collect_interfaces() -> List[Dict[str, Any]]:
    """Collect network interface information from the current node using sysfs.

    Returns:
        List of interface dictionaries with name, mac_address, and ip_addresses
    """
    pattern = IFACE_NAME_REGEX or None
    iface_re = None
    if pattern:
        try:
            iface_re = re.compile(pattern)
        except re.error as exc:
            log.warning(
                "Invalid IFACE_NAME_REGEX '%s': %s; falling back to default interface prefixes",
                pattern,
                exc,
            )
            iface_re = None

    interfaces = []

    try:
        # Iterate through all network interfaces in /sys/class/net
        for iface_path in sorted(SYSFS_NET_PATH.iterdir()):
            if not iface_path.is_dir():
                continue

            iface_name = iface_path.name

            # Skip loopback and known virtual interfaces
            if iface_name in ("lo", "docker0",
                              "cni0") or iface_name.startswith("veth"):
                continue

            # Check if this is a physical interface (has device symlink)
            if not is_physical_interface(iface_name):
                log.debug("Skipping %s (not a physical interface)", iface_name)
                continue

            # Apply name filter
            match = False
            if iface_re is not None:
                if iface_re.search(iface_name):
                    match = True
            else:
                # Default: match common physical interface prefixes
                if iface_name.startswith(("eth", "eno", "ens", "enp", "em")):
                    match = True

            if not match:
                log.debug("Skipping %s (does not match name filter)",
                          iface_name)
                continue

            # Get MAC address
            iface_mac = get_interface_mac(iface_name)
            if not iface_mac:
                log.debug("Skipping %s (no MAC address)", iface_name)
                continue

            # Get IP address
            iface_ip = get_interface_ip(iface_name)

            # Build interface info
            iface_info = {
                "name": iface_name,
                "mac_address": iface_mac,
                "ip_addresses": [iface_ip] if iface_ip else [],
            }

            interfaces.append(iface_info)
            log.debug("Discovered interface %s: MAC=%s, IP=%s", iface_name,
                      iface_mac, iface_ip or "none")

    except Exception as e:
        log.error("Failed to discover network interfaces: %s", e)

    return sorted(interfaces, key=lambda x: x["name"])


if __name__ == "__main__":
    interfaces = collect_interfaces()
    node_data = {
        "node_name": NODE_NAME,
        "hostname": HOSTNAME,
        "location": CLUSTER_NAME,
        "interfaces": interfaces,
    }

    if not interfaces:
        log.warning("No usable interfaces found on node %s", NODE_NAME)
    else:
        log.info("Found %d interface(s) on node %s:", len(interfaces),
                 NODE_NAME)
        for iface in interfaces:
            log.info(
                "  - %s: %s (IPs: %s)",
                iface["name"],
                iface["mac_address"],
                ", ".join(iface["ip_addresses"]),
            )

    print(json.dumps(node_data))
