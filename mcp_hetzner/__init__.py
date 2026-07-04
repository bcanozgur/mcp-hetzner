"""
Hetzner Cloud MCP Server - MCP interface for Hetzner Cloud API

This MCP package provides functions to manage Hetzner Cloud resources:
- List, create, and delete servers
- Get server details
- List available images, server types, and locations
- Power on/off and reboot servers
- Create, manage, and apply firewalls
- Create, attach, detach, and resize volumes
- Create and manage private networks (subnets, routes, server attachment)
- Manage Hetzner DNS zones and records (separate DNS API / token)
- Manage SSH keys for secure server access
"""

__version__ = "0.3.0"