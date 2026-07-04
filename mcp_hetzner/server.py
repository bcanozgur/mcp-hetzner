#!/usr/bin/env python
"""
Hetzner Cloud MCP Server - MCP interface for Hetzner Cloud API

This MCP service provides functions to manage Hetzner Cloud resources:
- List, create, and delete servers
- Get server details
- List available images, server types, and locations
- Power on/off and reboot servers
- Create, manage, and apply firewalls
- Create, attach, detach, and resize volumes
- Manage SSH keys for secure server access
- Create and manage private networks (subnets, routes, server attachment)
- Manage Hetzner DNS zones and records (separate DNS API / token)

All Hetzner API calls (Cloud and DNS) transparently retry on rate-limit
(HTTP 429) responses with exponential backoff and jitter.
"""

import functools
import logging
import os
import random
import sys
import time
from typing import Callable, Dict, List, Optional, Any, TypeVar, Union

import dotenv
import requests
from hcloud import Client, APIException
from hcloud.images.domain import Image
from hcloud.locations.domain import Location
from hcloud.server_types.domain import ServerType
from hcloud.servers.domain import Server
from hcloud.firewalls.domain import Firewall, FirewallRule, FirewallResource, FirewallResourceLabelSelector
from hcloud.networks.domain import Network, NetworkSubnet, NetworkRoute
from hcloud.volumes.domain import Volume
from hcloud.ssh_keys.domain import SSHKey
from pydantic import BaseModel, Field

from mcp.server.fastmcp import FastMCP

# All diagnostics MUST go to stderr. The stdio transport uses stdout for the
# JSON-RPC stream, so any stray stdout write (a bare print, a traceback) would
# corrupt the protocol and break the MCP client.
logging.basicConfig(
    level=os.environ.get("MCP_HETZNER_LOG_LEVEL", "INFO").upper(),
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    stream=sys.stderr,
)
logger = logging.getLogger("mcp_hetzner")

# Load environment variables from a .env in the user's working directory (not
# the installed package location) as a convenience for local development. The
# primary, recommended mechanism is a real HCLOUD_TOKEN injected by the MCP
# client via its `env` config block.
dotenv.load_dotenv(dotenv.find_dotenv(usecwd=True))

# The hcloud client is created lazily once a token is resolved (see
# resolve_client). Importing this module therefore never requires a token and
# never exits the process — important for tests, tooling, and MCP clients that
# import before they run.
client: Optional[Client] = None

# Rate-limit retry configuration. Hetzner Cloud limits API calls (3600/hour by
# default) and answers over-limit requests with HTTP 429 / a
# "rate_limit_exceeded" APIException. We transparently retry those with
# exponential backoff + full jitter so bursts of tool calls don't fail hard.
# Tunable via environment variables for operators with different limits.
RATE_LIMIT_MAX_RETRIES = int(os.environ.get("MCP_HETZNER_MAX_RETRIES", 5))
RATE_LIMIT_BASE_DELAY = float(os.environ.get("MCP_HETZNER_RETRY_BASE_DELAY", 1.0))
RATE_LIMIT_MAX_DELAY = float(os.environ.get("MCP_HETZNER_RETRY_MAX_DELAY", 60.0))

# Hetzner DNS is a SEPARATE product from Hetzner Cloud, served by a different
# API host and authenticated with its own token (created at
# https://dns.hetzner.com/settings/api-token). The hcloud SDK does not cover it,
# so DNS tools talk to the REST API directly via _dns_request().
DNS_API_BASE = os.environ.get("HETZNER_DNS_API_BASE", "https://dns.hetzner.com/api/v1")
DNS_REQUEST_TIMEOUT = float(os.environ.get("MCP_HETZNER_DNS_TIMEOUT", 30.0))

F = TypeVar("F", bound=Callable[..., Any])


def _is_rate_limit_error(exc: APIException) -> bool:
    """True if an APIException represents a Hetzner rate-limit response.

    The ``code`` may arrive as the string ``"rate_limit_exceeded"`` or as the
    numeric HTTP status ``429`` depending on the endpoint, so we check both.
    """
    return exc.code in ("rate_limit_exceeded", 429)


def _with_rate_limit_retry(request_func: F) -> F:
    """Wrap ``Client.request`` so rate-limited calls are retried with backoff.

    Every hcloud domain client (servers, volumes, networks, …) funnels its HTTP
    calls through the single ``Client.request`` method, so wrapping that one
    choke point transparently protects every tool in this module. Only
    rate-limit errors are retried; all other exceptions propagate unchanged.
    """

    @functools.wraps(request_func)
    def wrapper(*args: Any, **kwargs: Any) -> Any:
        attempt = 0
        while True:
            try:
                return request_func(*args, **kwargs)
            except APIException as exc:
                if not _is_rate_limit_error(exc) or attempt >= RATE_LIMIT_MAX_RETRIES:
                    raise
                # Exponential backoff capped at RATE_LIMIT_MAX_DELAY, with full
                # jitter to avoid a thundering herd of synchronized retries.
                ceiling = min(RATE_LIMIT_MAX_DELAY, RATE_LIMIT_BASE_DELAY * (2 ** attempt))
                delay = random.uniform(0, ceiling)
                attempt += 1
                logger.warning(
                    "Hetzner rate limit hit; retry %d/%d in %.2fs",
                    attempt, RATE_LIMIT_MAX_RETRIES, delay,
                )
                time.sleep(delay)

    return wrapper  # type: ignore[return-value]


def resolve_client(token: Optional[str] = None) -> Client:
    """Resolve the Hetzner API token and initialise the module-level client.

    Precedence: an explicit ``token`` argument (e.g. from ``--token``) wins,
    otherwise the ``HCLOUD_TOKEN`` environment variable is used (which may have
    been populated from a local ``.env``). Raises ``RuntimeError`` if neither
    is present.
    """
    global client
    resolved = token or os.environ.get("HCLOUD_TOKEN")
    if not resolved:
        raise RuntimeError(
            "No Hetzner Cloud API token found. Set the HCLOUD_TOKEN environment "
            "variable (recommended: inject it via your MCP client's `env` "
            "config), pass --token, or add HCLOUD_TOKEN to a local .env file. "
            "Create a token in the Hetzner Cloud Console: "
            "Project -> Security -> API Tokens (Read & Write)."
        )
    client = Client(token=resolved)
    # Transparently retry rate-limited requests for every domain client. The
    # domain clients (servers, volumes, networks, …) all funnel their HTTP calls
    # through the inner ClientBase's ``request`` method (``Client.request`` is a
    # thin delegator they bypass), so we wrap that inner choke point. Fall back
    # to the public method if the internal layout ever changes.
    inner = getattr(client, "_client", None)
    if inner is not None and hasattr(inner, "request"):
        inner.request = _with_rate_limit_retry(inner.request)
    else:  # pragma: no cover - defensive fallback for future hcloud layouts
        client.request = _with_rate_limit_retry(client.request)
    return client


# Create MCP server with server configuration
mcp = FastMCP(
    "Hetzner Cloud",
    host=os.environ.get("MCP_HOST", "localhost"),
    port=int(os.environ.get("MCP_PORT", 8080))
)

# Helper function to convert Server object to dict
def server_to_dict(server: Server) -> Dict[str, Any]:
    """Convert a Server object to a dictionary with relevant information."""
    return {
        "id": server.id,
        "name": server.name,
        "status": server.status,
        "created": server.created.isoformat() if server.created else None,
        "server_type": server.server_type.name if server.server_type else None,
        "image": server.image.name if server.image else None,
        "datacenter": server.datacenter.name if server.datacenter else None,
        "location": server.datacenter.location.name if server.datacenter and server.datacenter.location else None,
        "public_net": {
            "ipv4": server.public_net.ipv4.ip if server.public_net and server.public_net.ipv4 else None,
            "ipv6": server.public_net.ipv6.ip if server.public_net and server.public_net.ipv6 else None,
        },
        "included_traffic": server.included_traffic,
        "outgoing_traffic": server.outgoing_traffic,
        "ingoing_traffic": server.ingoing_traffic,
        "backup_window": server.backup_window,
        "rescue_enabled": server.rescue_enabled,
        "locked": server.locked,
        "protection": {
            "delete": server.protection["delete"] if server.protection else False,
            "rebuild": server.protection["rebuild"] if server.protection else False,
        },
        "labels": server.labels,
        "volumes": [volume.id for volume in server.volumes] if server.volumes else [],
    }

# Helper function to convert Volume object to dict
def volume_to_dict(volume: Volume) -> Dict[str, Any]:
    """Convert a Volume object to a dictionary with relevant information."""
    return {
        "id": volume.id,
        "name": volume.name,
        "size": volume.size,
        "location": volume.location.name if volume.location else None,
        "server": volume.server.id if volume.server else None,
        "linux_device": volume.linux_device,
        "protection": {
            "delete": volume.protection["delete"] if volume.protection else False,
        },
        "labels": volume.labels,
        "format": volume.format,
        "created": volume.created.isoformat() if volume.created else None,
        "status": volume.status,
    }

# Helper function to convert SSHKey object to dict
def ssh_key_to_dict(ssh_key: SSHKey) -> Dict[str, Any]:
    """Convert an SSHKey object to a dictionary with relevant information."""
    return {
        "id": ssh_key.id,
        "name": ssh_key.name,
        "fingerprint": ssh_key.fingerprint,
        "public_key": ssh_key.public_key,
        "labels": ssh_key.labels,
        "created": ssh_key.created.isoformat() if ssh_key.created else None,
    }

# Helper function to convert Firewall object to dict
def firewall_to_dict(firewall: Firewall) -> Dict[str, Any]:
    """Convert a Firewall object to a dictionary with relevant information."""
    # Convert rules to dict
    rules = []
    if firewall.rules:
        for rule in firewall.rules:
            rule_dict = {
                "direction": rule.direction,
                "protocol": rule.protocol,
                "source_ips": rule.source_ips,
            }
            if rule.port:
                rule_dict["port"] = rule.port
            if rule.destination_ips:
                rule_dict["destination_ips"] = rule.destination_ips
            if rule.description:
                rule_dict["description"] = rule.description
            rules.append(rule_dict)
    
    # Convert applied_to resources to dict
    applied_to = []
    if firewall.applied_to:
        for resource in firewall.applied_to:
            resource_dict = {"type": resource.type}
            if resource.server:
                resource_dict["server"] = {"id": resource.server.id, "name": resource.server.name}
            if resource.label_selector:
                resource_dict["label_selector"] = {"selector": resource.label_selector.selector}
            if getattr(resource, 'applied_to_resources', None):
                applied_resources = []
                for applied_resource in resource.applied_to_resources:
                    applied_resource_dict = {"type": applied_resource.type}
                    if applied_resource.server:
                        applied_resource_dict["server"] = {"id": applied_resource.server.id, "name": applied_resource.server.name}
                    applied_resources.append(applied_resource_dict)
                resource_dict["applied_to_resources"] = applied_resources
            applied_to.append(resource_dict)
    
    return {
        "id": firewall.id,
        "name": firewall.name,
        "rules": rules,
        "applied_to": applied_to,
        "labels": firewall.labels,
        "created": firewall.created.isoformat() if firewall.created else None,
    }

# Helper function to convert Network object to dict
def network_to_dict(network: Network) -> Dict[str, Any]:
    """Convert a Network object to a dictionary with relevant information."""
    subnets = []
    if network.subnets:
        for subnet in network.subnets:
            subnets.append({
                "type": subnet.type,
                "ip_range": subnet.ip_range,
                "network_zone": subnet.network_zone,
                "gateway": subnet.gateway,
                "vswitch_id": subnet.vswitch_id,
            })

    routes = []
    if network.routes:
        for route in network.routes:
            routes.append({
                "destination": route.destination,
                "gateway": route.gateway,
            })

    return {
        "id": network.id,
        "name": network.name,
        "ip_range": network.ip_range,
        "subnets": subnets,
        "routes": routes,
        "servers": [server.id for server in network.servers] if network.servers else [],
        "protection": {
            "delete": network.protection["delete"] if network.protection else False,
        },
        "labels": network.labels,
        "created": network.created.isoformat() if network.created else None,
    }

# Create Server Parameters Model
class CreateServerParams(BaseModel):
    name: str = Field(..., description="Name of the server")
    server_type: str = Field(..., description="Server type (e.g., cx11, cx21, etc.)")
    image: str = Field(..., description="Image name or ID (e.g., ubuntu-22.04, debian-11, etc.)")
    location: Optional[str] = Field("nbg1", description="Location (e.g., nbg1, fsn1, etc.)")
    ssh_keys: Optional[List[int]] = Field(None, description="List of SSH key IDs")

# Server ID Parameter Model
class ServerIdParam(BaseModel):
    server_id: int = Field(..., description="The ID of the server")

# Firewall ID Parameter Model
class FirewallIdParam(BaseModel):
    firewall_id: int = Field(..., description="The ID of the firewall")

# Firewall Rule Parameter Model
class FirewallRuleParam(BaseModel):
    direction: str = Field(..., description="Direction of the rule (in or out)")
    protocol: str = Field(..., description="Protocol (tcp, udp, icmp, esp, or gre)")
    source_ips: List[str] = Field(..., description="List of source IPs in CIDR notation")
    port: Optional[str] = Field(None, description="Port or port range (e.g., '80' or '80-85'), only for TCP/UDP")
    destination_ips: Optional[List[str]] = Field(None, description="List of destination IPs in CIDR notation")
    description: Optional[str] = Field(None, description="Description of the rule")

# Firewall Resource Parameter Model
class FirewallResourceParam(BaseModel):
    type: str = Field(..., description="Type of resource ('server' or 'label_selector')")
    server_id: Optional[int] = Field(None, description="Server ID (required when type is 'server')")
    label_selector: Optional[str] = Field(None, description="Label selector (required when type is 'label_selector')")

# Create Firewall Parameter Model
class CreateFirewallParams(BaseModel):
    name: str = Field(..., description="Name of the firewall")
    rules: Optional[List[FirewallRuleParam]] = Field(None, description="List of firewall rules")
    resources: Optional[List[FirewallResourceParam]] = Field(None, description="List of resources to apply the firewall to")
    labels: Optional[Dict[str, str]] = Field(None, description="User-defined labels (key-value pairs)")

# Update Firewall Parameter Model
class UpdateFirewallParams(BaseModel):
    firewall_id: int = Field(..., description="The ID of the firewall")
    name: Optional[str] = Field(None, description="New name for the firewall")
    labels: Optional[Dict[str, str]] = Field(None, description="User-defined labels (key-value pairs)")

# Set Firewall Rules Parameter Model
class SetFirewallRulesParams(BaseModel):
    firewall_id: int = Field(..., description="The ID of the firewall")
    rules: List[FirewallRuleParam] = Field(..., description="List of firewall rules")

# Apply/Remove Firewall Resources Parameter Model
class FirewallResourcesParams(BaseModel):
    firewall_id: int = Field(..., description="The ID of the firewall")
    resources: List[FirewallResourceParam] = Field(..., description="List of resources to apply/remove the firewall to/from")

# Volume ID Parameter Model
class VolumeIdParam(BaseModel):
    volume_id: int = Field(..., description="The ID of the volume")

# Create Volume Parameter Model
class CreateVolumeParams(BaseModel):
    name: str = Field(..., description="Name of the volume")
    size: int = Field(..., description="Size of the volume in GB (min 10, max 10240)")
    location: Optional[str] = Field(None, description="Location where the volume will be created (e.g., nbg1, fsn1)")
    server: Optional[int] = Field(None, description="ID of the server to attach the volume to")
    automount: Optional[bool] = Field(False, description="Auto-mount the volume after attaching it")
    format: Optional[str] = Field(None, description="Filesystem format (e.g., xfs, ext4)")
    labels: Optional[Dict[str, str]] = Field(None, description="User-defined labels (key-value pairs)")

# Attach Volume Parameter Model
class AttachVolumeParams(BaseModel):
    volume_id: int = Field(..., description="The ID of the volume")
    server_id: int = Field(..., description="The ID of the server to attach the volume to")
    automount: Optional[bool] = Field(False, description="Auto-mount the volume after attaching it")

# Resize Volume Parameter Model
class ResizeVolumeParams(BaseModel):
    volume_id: int = Field(..., description="The ID of the volume")
    size: int = Field(..., description="New size of the volume in GB (must be greater than current size)")

# SSH Key ID Parameter Model
class SSHKeyIdParam(BaseModel):
    ssh_key_id: int = Field(..., description="The ID of the SSH key")

# Create SSH Key Parameter Model
class CreateSSHKeyParams(BaseModel):
    name: str = Field(..., description="Name of the SSH key")
    public_key: str = Field(..., description="The public key in OpenSSH format")
    labels: Optional[Dict[str, str]] = Field(None, description="User-defined labels (key-value pairs)")

# Update SSH Key Parameter Model
class UpdateSSHKeyParams(BaseModel):
    ssh_key_id: int = Field(..., description="The ID of the SSH key")
    name: str = Field(..., description="New name for the SSH key")
    labels: Optional[Dict[str, str]] = Field(None, description="User-defined labels (key-value pairs)")

# Network ID Parameter Model
class NetworkIdParam(BaseModel):
    network_id: int = Field(..., description="The ID of the network")

# Create Network Parameter Model
class CreateNetworkParams(BaseModel):
    name: str = Field(..., description="Name of the network")
    ip_range: str = Field(..., description="IP range of the whole network in CIDR notation (e.g., '10.0.0.0/16')")
    expose_routes_to_vswitch: Optional[bool] = Field(None, description="Expose routes to connected vSwitch (advanced)")
    labels: Optional[Dict[str, str]] = Field(None, description="User-defined labels (key-value pairs)")

# Update Network Parameter Model
class UpdateNetworkParams(BaseModel):
    network_id: int = Field(..., description="The ID of the network")
    name: Optional[str] = Field(None, description="New name for the network")
    expose_routes_to_vswitch: Optional[bool] = Field(None, description="Expose routes to connected vSwitch (advanced)")
    labels: Optional[Dict[str, str]] = Field(None, description="User-defined labels (key-value pairs)")

# Add Subnet Parameter Model
class AddSubnetParams(BaseModel):
    network_id: int = Field(..., description="The ID of the network")
    ip_range: str = Field(..., description="IP range of the subnet in CIDR notation, within the network range (e.g., '10.0.1.0/24')")
    network_zone: str = Field(..., description="Network zone of the subnet (e.g., 'eu-central', 'us-east', 'us-west', 'ap-southeast')")
    type: Optional[str] = Field("cloud", description="Subnet type: 'cloud' (default), 'server', or 'vswitch'")
    vswitch_id: Optional[int] = Field(None, description="ID of the vSwitch (required only when type is 'vswitch')")

# Delete Subnet Parameter Model
class DeleteSubnetParams(BaseModel):
    network_id: int = Field(..., description="The ID of the network")
    ip_range: str = Field(..., description="IP range of the subnet to delete in CIDR notation")

# Add Route Parameter Model
class AddRouteParams(BaseModel):
    network_id: int = Field(..., description="The ID of the network")
    destination: str = Field(..., description="Destination network or host in CIDR notation (e.g., '10.100.1.0/24')")
    gateway: str = Field(..., description="Gateway IP address for the route (must be within the network range)")

# Delete Route Parameter Model
class DeleteRouteParams(BaseModel):
    network_id: int = Field(..., description="The ID of the network")
    destination: str = Field(..., description="Destination of the route to delete in CIDR notation")
    gateway: str = Field(..., description="Gateway IP address of the route to delete")

# Server<->Network Attach Parameter Model
class AttachServerToNetworkParams(BaseModel):
    network_id: int = Field(..., description="The ID of the network")
    server_id: int = Field(..., description="The ID of the server to attach")
    ip: Optional[str] = Field(None, description="Specific private IP to assign the server within the network (optional)")
    alias_ips: Optional[List[str]] = Field(None, description="Additional alias IPs to assign to the server (optional)")

# Server<->Network Detach Parameter Model
class DetachServerFromNetworkParams(BaseModel):
    network_id: int = Field(..., description="The ID of the network")
    server_id: int = Field(..., description="The ID of the server to detach")

# DNS Zone ID Parameter Model
class DNSZoneIdParam(BaseModel):
    zone_id: str = Field(..., description="The ID of the DNS zone")

# List DNS Zones Parameter Model
class ListDNSZonesParams(BaseModel):
    name: Optional[str] = Field(None, description="Full name of a zone to filter by (e.g., 'example.com')")
    search_name: Optional[str] = Field(None, description="Partial name to search zones by")

# Create DNS Zone Parameter Model
class CreateDNSZoneParams(BaseModel):
    name: str = Field(..., description="Name of the zone / domain (e.g., 'example.com')")
    ttl: Optional[int] = Field(None, description="Default TTL for records in the zone, in seconds")

# Update DNS Zone Parameter Model
class UpdateDNSZoneParams(BaseModel):
    zone_id: str = Field(..., description="The ID of the DNS zone")
    name: str = Field(..., description="Name of the zone / domain")
    ttl: Optional[int] = Field(None, description="Default TTL for records in the zone, in seconds")

# DNS Record ID Parameter Model
class DNSRecordIdParam(BaseModel):
    record_id: str = Field(..., description="The ID of the DNS record")

# List DNS Records Parameter Model
class ListDNSRecordsParams(BaseModel):
    zone_id: str = Field(..., description="The ID of the DNS zone whose records to list")

# Create DNS Record Parameter Model
class CreateDNSRecordParams(BaseModel):
    zone_id: str = Field(..., description="The ID of the DNS zone the record belongs to")
    type: str = Field(..., description="Record type (e.g., A, AAAA, CNAME, MX, TXT, NS, SRV, CAA)")
    name: str = Field(..., description="Record name; use '@' for the zone root (e.g., 'www', '@')")
    value: str = Field(..., description="Record value (e.g., an IP for A records, a hostname for CNAME)")
    ttl: Optional[int] = Field(None, description="Time to live in seconds (falls back to the zone default if omitted)")

# Update DNS Record Parameter Model
class UpdateDNSRecordParams(BaseModel):
    record_id: str = Field(..., description="The ID of the DNS record to update")
    zone_id: str = Field(..., description="The ID of the DNS zone the record belongs to")
    type: str = Field(..., description="Record type (e.g., A, AAAA, CNAME, MX, TXT, NS, SRV, CAA)")
    name: str = Field(..., description="Record name; use '@' for the zone root")
    value: str = Field(..., description="Record value")
    ttl: Optional[int] = Field(None, description="Time to live in seconds")

# Bulk Create DNS Records Parameter Model
class BulkDNSRecord(BaseModel):
    zone_id: str = Field(..., description="The ID of the DNS zone the record belongs to")
    type: str = Field(..., description="Record type (e.g., A, AAAA, CNAME, MX, TXT)")
    name: str = Field(..., description="Record name; use '@' for the zone root")
    value: str = Field(..., description="Record value")
    ttl: Optional[int] = Field(None, description="Time to live in seconds")

class BulkCreateDNSRecordsParams(BaseModel):
    records: List[BulkDNSRecord] = Field(..., description="List of DNS records to create in a single request")

# MCP Tools

@mcp.tool()
def list_servers() -> Dict[str, Any]:
    """
    List all servers in your Hetzner Cloud account.
    
    Returns a list of all server instances with their details.
    
    Example:
    - Basic list: list_servers()
    """
    try:
        servers = client.servers.get_all()
        return {
            "servers": [server_to_dict(server) for server in servers]
        }
    except Exception as e:
        return {"error": f"Failed to list servers: {str(e)}"}

@mcp.tool()
def get_server(params: ServerIdParam) -> Dict[str, Any]:
    """
    Get details about a specific server.
    
    Returns detailed information about a server identified by its ID.
    
    Example:
    - Get server details: {"server_id": 12345}
    """
    try:
        server = client.servers.get_by_id(params.server_id)
        if not server:
            return {"error": f"Server with ID {params.server_id} not found"}
        
        return {"server": server_to_dict(server)}
    except Exception as e:
        return {"error": f"Failed to get server: {str(e)}"}

@mcp.tool()
def create_server(params: CreateServerParams) -> Dict[str, Any]:
    """
    Create a new server.
    
    Creates a new server with the specified configuration.
    
    Examples:
    - Basic server: {"name": "web-server", "server_type": "cx11", "image": "ubuntu-22.04"}
    - With SSH keys: {"name": "app-server", "server_type": "cx21", "image": "debian-11", "ssh_keys": [123, 456]}
    - Custom location: {"name": "db-server", "server_type": "cx31", "image": "ubuntu-22.04", "location": "fsn1"}
    """
    try:
        # Get the objects needed for the API call
        try:
            # Debug the objects
            server_types = client.server_types.get_all()
            images = client.images.get_all()
            locations = client.locations.get_all()
            
            # Print available options for debugging
            server_type_names = [st.name for st in server_types]
            image_names = [img.name for img in images]
            location_names = [loc.name for loc in locations]
            
            # Try to get objects by name
            server_type_obj = client.server_types.get_by_name(params.server_type)
            image_obj = client.images.get_by_name(params.image)
            location_obj = client.locations.get_by_name(params.location)
            
            # Check if objects were found
            if server_type_obj is None:
                return {"error": f"Server type '{params.server_type}' not found. Available types: {server_type_names}"}
            if image_obj is None:
                return {"error": f"Image '{params.image}' not found. Available images: {image_names}"}
            if location_obj is None:
                return {"error": f"Location '{params.location}' not found. Available locations: {location_names}"}
            
            # Handle SSH keys if provided - convert IDs to objects or use names
            ssh_keys = []
            if params.ssh_keys:
                for ssh_key in params.ssh_keys:
                    # If SSH key is an integer ID, get the object
                    if isinstance(ssh_key, int):
                        ssh_key_obj = client.ssh_keys.get_by_id(ssh_key)
                        if ssh_key_obj:
                            ssh_keys.append(ssh_key_obj)
                    # If SSH key is a string name, get the object
                    elif isinstance(ssh_key, str):
                        ssh_key_obj = client.ssh_keys.get_by_name(ssh_key)
                        if ssh_key_obj:
                            ssh_keys.append(ssh_key_obj)
            
            # Create server with objects instead of strings
            response = client.servers.create(
                name=params.name,
                server_type=server_type_obj,
                image=image_obj,
                location=location_obj,
                ssh_keys=ssh_keys
            )
        except Exception as e:
            return {"error": f"Failed to create server: {str(e)}"}
        
        # Extract server and action information
        server = response.server
        action = response.action
        
        # Don't wait for the action to complete - the method doesn't exist
        return {
            "server": server_to_dict(server),
            "action": {
                "id": action.id,
                "status": action.status,
                "command": action.command,
                "progress": action.progress,
                "error": action.error,
                "started": action.started.isoformat() if action.started else None,
                "finished": action.finished.isoformat() if action.finished else None,
            } if action else None,
            "root_password": response.root_password,  # Only provided when no SSH keys are used
        }
    except Exception as e:
        return {"error": f"Failed to create server: {str(e)}"}

@mcp.tool()
def delete_server(params: ServerIdParam) -> Dict[str, Any]:
    """
    Delete a server.
    
    Permanently deletes a server identified by its ID.
    
    Example:
    - Delete server: {"server_id": 12345}
    """
    try:
        server = client.servers.get_by_id(params.server_id)
        if not server:
            return {"error": f"Server with ID {params.server_id} not found"}
            
        action = client.servers.delete(server)
        
        # Don't wait for the action to complete - the method doesn't exist
        return {
            "success": True,
            "action": {
                "id": action.id,
                "status": action.status,
                "command": action.command,
                "progress": action.progress,
                "error": action.error,
                "started": action.started.isoformat() if action.started else None,
                "finished": action.finished.isoformat() if action.finished else None,
            } if action else None,
        }
    except Exception as e:
        return {"error": f"Failed to delete server: {str(e)}"}

@mcp.tool()
def list_images() -> Dict[str, Any]:
    """
    List available images.
    
    Returns a list of all available OS images that can be used to create servers.
    
    Example:
    - List images: list_images()
    """
    try:
        images = client.images.get_all()
        return {
            "images": [
                {
                    "id": image.id,
                    "name": image.name,
                    "description": image.description,
                    "type": image.type,
                    "status": image.status,
                    "os_flavor": image.os_flavor,
                    "os_version": image.os_version,
                    "architecture": image.architecture,
                    "size_gb": image.disk_size,
                    "created": image.created.isoformat() if image.created else None
                }
                for image in images
            ]
        }
    except Exception as e:
        return {"error": f"Failed to list images: {str(e)}"}

@mcp.tool()
def list_server_types() -> Dict[str, Any]:
    """
    List available server types.
    
    Returns information about all available server configurations.
    
    Example:
    - List server types: list_server_types()
    """
    try:
        server_types = client.server_types.get_all()
        result = []
        
        for st in server_types:
            server_type_info = {
                "id": st.id,
                "name": st.name,
                "description": st.description,
                "cores": st.cores,
                "memory_gb": st.memory,
                "disk_gb": st.disk,
                "storage_type": st.storage_type,
                "cpu_type": st.cpu_type,
                "prices": []
            }
            
            if hasattr(st, 'prices') and st.prices:
                price_list = []
                for price in st.prices:
                    price_data = {}
                    if hasattr(price, 'price_hourly'):
                        price_data["price_hourly"] = price.price_hourly
                    if hasattr(price, 'price_monthly'):
                        price_data["price_monthly"] = price.price_monthly
                    # Safely add location if available
                    try:
                        if hasattr(price, 'location') and price.location and hasattr(price.location, 'name'):
                            price_data["location"] = price.location.name
                    except:
                        price_data["location"] = None
                        
                    price_list.append(price_data)
                server_type_info["prices"] = price_list
                
            result.append(server_type_info)
            
        return {
            "server_types": result
        }
    except Exception as e:
        return {"error": f"Failed to list server types: {str(e)}"}

@mcp.tool()
def list_locations() -> Dict[str, Any]:
    """
    List available locations.
    
    Returns information about all available datacenter locations.
    
    Example:
    - List locations: list_locations()
    """
    try:
        locations = client.locations.get_all()
        return {
            "locations": [
                {
                    "id": location.id,
                    "name": location.name,
                    "description": location.description,
                    "country": location.country,
                    "city": location.city,
                    "latitude": location.latitude,
                    "longitude": location.longitude,
                    "network_zone": location.network_zone
                }
                for location in locations
            ]
        }
    except Exception as e:
        return {"error": f"Failed to list locations: {str(e)}"}

@mcp.tool()
def power_on(params: ServerIdParam) -> Dict[str, Any]:
    """
    Power on a server.
    
    Powers on a server that is currently powered off.
    
    Example:
    - Power on server: {"server_id": 12345}
    """
    try:
        server = client.servers.get_by_id(params.server_id)
        if not server:
            return {"error": f"Server with ID {params.server_id} not found"}
            
        action = client.servers.power_on(server)
        
        # Don't wait for the action to complete - the method doesn't exist
        return {
            "success": True,
            "action": {
                "id": action.id,
                "status": action.status,
                "command": action.command,
                "progress": action.progress,
                "error": action.error,
                "started": action.started.isoformat() if action.started else None,
                "finished": action.finished.isoformat() if action.finished else None,
            } if action else None,
        }
    except Exception as e:
        return {"error": f"Failed to power on server: {str(e)}"}

@mcp.tool()
def power_off(params: ServerIdParam) -> Dict[str, Any]:
    """
    Power off a server.
    
    Powers off a server. Note: This is equivalent to pulling the power plug and may cause data loss.
    Consider using a graceful shutdown if possible.
    
    Example:
    - Power off server: {"server_id": 12345}
    """
    try:
        server = client.servers.get_by_id(params.server_id)
        if not server:
            return {"error": f"Server with ID {params.server_id} not found"}
            
        action = client.servers.power_off(server)
        
        # Don't wait for the action to complete - the method doesn't exist
        return {
            "success": True,
            "action": {
                "id": action.id,
                "status": action.status,
                "command": action.command,
                "progress": action.progress,
                "error": action.error,
                "started": action.started.isoformat() if action.started else None,
                "finished": action.finished.isoformat() if action.finished else None,
            } if action else None,
        }
    except Exception as e:
        return {"error": f"Failed to power off server: {str(e)}"}

@mcp.tool()
def reboot(params: ServerIdParam) -> Dict[str, Any]:
    """
    Reboot a server.
    
    Performs a soft reboot (graceful shutdown and restart) of the server.
    
    Example:
    - Reboot server: {"server_id": 12345}
    """
    try:
        server = client.servers.get_by_id(params.server_id)
        if not server:
            return {"error": f"Server with ID {params.server_id} not found"}
            
        action = client.servers.reboot(server)
        
        # Don't wait for the action to complete - the method doesn't exist
        return {
            "success": True,
            "action": {
                "id": action.id,
                "status": action.status,
                "command": action.command,
                "progress": action.progress,
                "error": action.error,
                "started": action.started.isoformat() if action.started else None,
                "finished": action.finished.isoformat() if action.finished else None,
            } if action else None,
        }
    except Exception as e:
        return {"error": f"Failed to reboot server: {str(e)}"}

# Firewall-related MCP tools

@mcp.tool()
def list_firewalls() -> Dict[str, Any]:
    """
    List all firewalls in your Hetzner Cloud account.
    
    Returns a list of all firewall instances with their details.
    
    Example:
    - Basic list: list_firewalls()
    """
    try:
        firewalls = client.firewalls.get_all()
        return {
            "firewalls": [firewall_to_dict(firewall) for firewall in firewalls]
        }
    except Exception as e:
        return {"error": f"Failed to list firewalls: {str(e)}"}

@mcp.tool()
def get_firewall(params: FirewallIdParam) -> Dict[str, Any]:
    """
    Get details about a specific firewall.
    
    Returns detailed information about a firewall identified by its ID.
    
    Example:
    - Get firewall details: {"firewall_id": 12345}
    """
    try:
        firewall = client.firewalls.get_by_id(params.firewall_id)
        if not firewall:
            return {"error": f"Firewall with ID {params.firewall_id} not found"}
        
        return {"firewall": firewall_to_dict(firewall)}
    except Exception as e:
        return {"error": f"Failed to get firewall: {str(e)}"}

@mcp.tool()
def create_firewall(params: CreateFirewallParams) -> Dict[str, Any]:
    """
    Create a new firewall.
    
    Creates a new firewall with the specified name, rules, and resources.
    
    Examples:
    - Basic firewall: {"name": "web-firewall"}
    - With rules: {"name": "web-firewall", "rules": [{"direction": "in", "protocol": "tcp", "port": "80", "source_ips": ["0.0.0.0/0"]}]}
    - With resources: {"name": "web-firewall", "rules": [...], "resources": [{"type": "server", "server_id": 123}]}
    """
    try:
        # Prepare rules if provided
        rules = None
        if params.rules:
            rules = []
            for rule_param in params.rules:
                rule = FirewallRule(
                    direction=rule_param.direction,
                    protocol=rule_param.protocol,
                    source_ips=rule_param.source_ips,
                    port=rule_param.port,
                    destination_ips=rule_param.destination_ips,
                    description=rule_param.description
                )
                rules.append(rule)
        
        # Prepare resources if provided
        resources = None
        if params.resources:
            resources = []
            for resource_param in params.resources:
                if resource_param.type == "server":
                    if not resource_param.server_id:
                        return {"error": "Server ID is required when resource type is 'server'"}
                    server = client.servers.get_by_id(resource_param.server_id)
                    if not server:
                        return {"error": f"Server with ID {resource_param.server_id} not found"}
                    resource = FirewallResource(type=resource_param.type, server=server)
                elif resource_param.type == "label_selector":
                    if not resource_param.label_selector:
                        return {"error": "Label selector is required when resource type is 'label_selector'"}
                    label_selector = FirewallResourceLabelSelector(selector=resource_param.label_selector)
                    resource = FirewallResource(type=resource_param.type, label_selector=label_selector)
                else:
                    return {"error": f"Invalid resource type: {resource_param.type}. Must be 'server' or 'label_selector'"}
                resources.append(resource)
        
        # Create the firewall
        response = client.firewalls.create(
            name=params.name,
            rules=rules,
            labels=params.labels,
            resources=resources
        )
        
        # Extract firewall and action information
        firewall = response.firewall
        actions = response.actions
        
        # Format the response
        return {
            "firewall": firewall_to_dict(firewall),
            "actions": [
                {
                    "id": action.id,
                    "status": action.status,
                    "command": action.command,
                    "progress": action.progress,
                    "error": action.error,
                    "started": action.started.isoformat() if action.started else None,
                    "finished": action.finished.isoformat() if action.finished else None,
                }
                for action in actions
            ] if actions else None,
        }
    except Exception as e:
        return {"error": f"Failed to create firewall: {str(e)}"}

@mcp.tool()
def update_firewall(params: UpdateFirewallParams) -> Dict[str, Any]:
    """
    Update a firewall.
    
    Updates the name or labels of an existing firewall.
    
    Example:
    - Update name: {"firewall_id": 12345, "name": "new-name"}
    - Update labels: {"firewall_id": 12345, "labels": {"key": "value"}}
    """
    try:
        firewall = client.firewalls.get_by_id(params.firewall_id)
        if not firewall:
            return {"error": f"Firewall with ID {params.firewall_id} not found"}
        
        updated_firewall = client.firewalls.update(
            firewall=firewall,
            name=params.name,
            labels=params.labels
        )
        
        return {"firewall": firewall_to_dict(updated_firewall)}
    except Exception as e:
        return {"error": f"Failed to update firewall: {str(e)}"}

@mcp.tool()
def delete_firewall(params: FirewallIdParam) -> Dict[str, Any]:
    """
    Delete a firewall.
    
    Permanently deletes a firewall identified by its ID.
    
    Example:
    - Delete firewall: {"firewall_id": 12345}
    """
    try:
        firewall = client.firewalls.get_by_id(params.firewall_id)
        if not firewall:
            return {"error": f"Firewall with ID {params.firewall_id} not found"}
            
        success = client.firewalls.delete(firewall)
        
        return {"success": success}
    except Exception as e:
        return {"error": f"Failed to delete firewall: {str(e)}"}

@mcp.tool()
def set_firewall_rules(params: SetFirewallRulesParams) -> Dict[str, Any]:
    """
    Set rules for a firewall.
    
    Sets the rules of a firewall. All existing rules will be overwritten.
    Pass an empty rules array to remove all rules.
    
    Example:
    - Set rules: {"firewall_id": 12345, "rules": [{"direction": "in", "protocol": "tcp", "port": "80", "source_ips": ["0.0.0.0/0"]}]}
    """
    try:
        firewall = client.firewalls.get_by_id(params.firewall_id)
        if not firewall:
            return {"error": f"Firewall with ID {params.firewall_id} not found"}
        
        # Convert rule parameters to FirewallRule objects
        rules = []
        for rule_param in params.rules:
            rule = FirewallRule(
                direction=rule_param.direction,
                protocol=rule_param.protocol,
                source_ips=rule_param.source_ips,
                port=rule_param.port,
                destination_ips=rule_param.destination_ips,
                description=rule_param.description
            )
            rules.append(rule)
        
        # Set the rules
        actions = client.firewalls.set_rules(firewall, rules)
        
        # Format the response
        return {
            "success": True,
            "actions": [
                {
                    "id": action.id,
                    "status": action.status,
                    "command": action.command,
                    "progress": action.progress,
                    "error": action.error,
                    "started": action.started.isoformat() if action.started else None,
                    "finished": action.finished.isoformat() if action.finished else None,
                }
                for action in actions
            ] if actions else None,
        }
    except Exception as e:
        return {"error": f"Failed to set firewall rules: {str(e)}"}

@mcp.tool()
def apply_firewall_to_resources(params: FirewallResourcesParams) -> Dict[str, Any]:
    """
    Apply a firewall to resources.
    
    Applies a firewall to multiple resources like servers or server groups by label.
    
    Examples:
    - Apply to server: {"firewall_id": 12345, "resources": [{"type": "server", "server_id": 123}]}
    - Apply by label: {"firewall_id": 12345, "resources": [{"type": "label_selector", "label_selector": "env=prod"}]}
    """
    try:
        firewall = client.firewalls.get_by_id(params.firewall_id)
        if not firewall:
            return {"error": f"Firewall with ID {params.firewall_id} not found"}
        
        # Convert resource parameters to FirewallResource objects
        resources = []
        for resource_param in params.resources:
            if resource_param.type == "server":
                if not resource_param.server_id:
                    return {"error": "Server ID is required when resource type is 'server'"}
                server = client.servers.get_by_id(resource_param.server_id)
                if not server:
                    return {"error": f"Server with ID {resource_param.server_id} not found"}
                resource = FirewallResource(type=resource_param.type, server=server)
            elif resource_param.type == "label_selector":
                if not resource_param.label_selector:
                    return {"error": "Label selector is required when resource type is 'label_selector'"}
                label_selector = FirewallResourceLabelSelector(selector=resource_param.label_selector)
                resource = FirewallResource(type=resource_param.type, label_selector=label_selector)
            else:
                return {"error": f"Invalid resource type: {resource_param.type}. Must be 'server' or 'label_selector'"}
            resources.append(resource)
        
        # Apply the firewall to the resources
        actions = client.firewalls.apply_to_resources(firewall, resources)
        
        # Format the response
        return {
            "success": True,
            "actions": [
                {
                    "id": action.id,
                    "status": action.status,
                    "command": action.command,
                    "progress": action.progress,
                    "error": action.error,
                    "started": action.started.isoformat() if action.started else None,
                    "finished": action.finished.isoformat() if action.finished else None,
                }
                for action in actions
            ] if actions else None,
        }
    except Exception as e:
        return {"error": f"Failed to apply firewall to resources: {str(e)}"}

@mcp.tool()
def remove_firewall_from_resources(params: FirewallResourcesParams) -> Dict[str, Any]:
    """
    Remove a firewall from resources.
    
    Removes a firewall from multiple resources.
    
    Examples:
    - Remove from server: {"firewall_id": 12345, "resources": [{"type": "server", "server_id": 123}]}
    - Remove by label: {"firewall_id": 12345, "resources": [{"type": "label_selector", "label_selector": "env=prod"}]}
    """
    try:
        firewall = client.firewalls.get_by_id(params.firewall_id)
        if not firewall:
            return {"error": f"Firewall with ID {params.firewall_id} not found"}
        
        # Convert resource parameters to FirewallResource objects
        resources = []
        for resource_param in params.resources:
            if resource_param.type == "server":
                if not resource_param.server_id:
                    return {"error": "Server ID is required when resource type is 'server'"}
                server = client.servers.get_by_id(resource_param.server_id)
                if not server:
                    return {"error": f"Server with ID {resource_param.server_id} not found"}
                resource = FirewallResource(type=resource_param.type, server=server)
            elif resource_param.type == "label_selector":
                if not resource_param.label_selector:
                    return {"error": "Label selector is required when resource type is 'label_selector'"}
                label_selector = FirewallResourceLabelSelector(selector=resource_param.label_selector)
                resource = FirewallResource(type=resource_param.type, label_selector=label_selector)
            else:
                return {"error": f"Invalid resource type: {resource_param.type}. Must be 'server' or 'label_selector'"}
            resources.append(resource)
        
        # Remove the firewall from the resources
        actions = client.firewalls.remove_from_resources(firewall, resources)
        
        # Format the response
        return {
            "success": True,
            "actions": [
                {
                    "id": action.id,
                    "status": action.status,
                    "command": action.command,
                    "progress": action.progress,
                    "error": action.error,
                    "started": action.started.isoformat() if action.started else None,
                    "finished": action.finished.isoformat() if action.finished else None,
                }
                for action in actions
            ] if actions else None,
        }
    except Exception as e:
        return {"error": f"Failed to remove firewall from resources: {str(e)}"}

# Volume-related MCP tools

@mcp.tool()
def list_volumes() -> Dict[str, Any]:
    """
    List all volumes in your Hetzner Cloud account.
    
    Returns a list of all volume instances with their details.
    
    Example:
    - Basic list: list_volumes()
    """
    try:
        volumes = client.volumes.get_all()
        return {
            "volumes": [volume_to_dict(volume) for volume in volumes]
        }
    except Exception as e:
        return {"error": f"Failed to list volumes: {str(e)}"}

@mcp.tool()
def get_volume(params: VolumeIdParam) -> Dict[str, Any]:
    """
    Get details about a specific volume.
    
    Returns detailed information about a volume identified by its ID.
    
    Example:
    - Get volume details: {"volume_id": 12345}
    """
    try:
        volume = client.volumes.get_by_id(params.volume_id)
        if not volume:
            return {"error": f"Volume with ID {params.volume_id} not found"}
        
        return {"volume": volume_to_dict(volume)}
    except Exception as e:
        return {"error": f"Failed to get volume: {str(e)}"}

@mcp.tool()
def create_volume(params: CreateVolumeParams) -> Dict[str, Any]:
    """
    Create a new volume.
    
    Creates a new volume with the specified configuration.
    
    Examples:
    - Basic volume: {"name": "data-volume", "size": 10}
    - With location: {"name": "db-volume", "size": 100, "location": "fsn1"}
    - Attached to server: {"name": "app-volume", "size": 50, "server": 123456, "automount": true}
    - With format: {"name": "log-volume", "size": 20, "format": "ext4"}
    """
    try:
        # Get location if provided
        location = None
        if params.location:
            location = client.locations.get_by_name(params.location)
            if not location:
                return {"error": f"Location '{params.location}' not found"}
        
        # Get server if provided
        server = None
        if params.server:
            server = client.servers.get_by_id(params.server)
            if not server:
                return {"error": f"Server with ID {params.server} not found"}
        
        # Create the volume
        response = client.volumes.create(
            name=params.name,
            size=params.size,
            location=location,
            server=server,
            automount=params.automount,
            format=params.format,
            labels=params.labels
        )
        
        # Extract volume and action information
        volume = response.volume
        action = response.action
        next_actions = response.next_actions
        
        # Format the response
        return {
            "volume": volume_to_dict(volume),
            "action": {
                "id": action.id,
                "status": action.status,
                "command": action.command,
                "progress": action.progress,
                "error": action.error,
                "started": action.started.isoformat() if action.started else None,
                "finished": action.finished.isoformat() if action.finished else None,
            } if action else None,
            "next_actions": [
                {
                    "id": next_action.id,
                    "status": next_action.status,
                    "command": next_action.command,
                    "progress": next_action.progress,
                    "error": next_action.error,
                    "started": next_action.started.isoformat() if next_action.started else None,
                    "finished": next_action.finished.isoformat() if next_action.finished else None,
                }
                for next_action in next_actions
            ] if next_actions else None,
        }
    except Exception as e:
        return {"error": f"Failed to create volume: {str(e)}"}

@mcp.tool()
def delete_volume(params: VolumeIdParam) -> Dict[str, Any]:
    """
    Delete a volume.
    
    Permanently deletes a volume identified by its ID.
    
    Example:
    - Delete volume: {"volume_id": 12345}
    """
    try:
        volume = client.volumes.get_by_id(params.volume_id)
        if not volume:
            return {"error": f"Volume with ID {params.volume_id} not found"}
            
        success = client.volumes.delete(volume)
        
        return {"success": success}
    except Exception as e:
        return {"error": f"Failed to delete volume: {str(e)}"}

@mcp.tool()
def attach_volume(params: AttachVolumeParams) -> Dict[str, Any]:
    """
    Attach a volume to a server.
    
    Attaches a volume to a server and optionally mounts it.
    
    Example:
    - Attach volume: {"volume_id": 12345, "server_id": 67890}
    - Attach and mount: {"volume_id": 12345, "server_id": 67890, "automount": true}
    """
    try:
        volume = client.volumes.get_by_id(params.volume_id)
        if not volume:
            return {"error": f"Volume with ID {params.volume_id} not found"}
        
        server = client.servers.get_by_id(params.server_id)
        if not server:
            return {"error": f"Server with ID {params.server_id} not found"}
        
        action = client.volumes.attach(volume, server, params.automount)
        
        # Format the response
        return {
            "success": True,
            "action": {
                "id": action.id,
                "status": action.status,
                "command": action.command,
                "progress": action.progress,
                "error": action.error,
                "started": action.started.isoformat() if action.started else None,
                "finished": action.finished.isoformat() if action.finished else None,
            } if action else None,
        }
    except Exception as e:
        return {"error": f"Failed to attach volume: {str(e)}"}

@mcp.tool()
def detach_volume(params: VolumeIdParam) -> Dict[str, Any]:
    """
    Detach a volume from a server.
    
    Detaches a volume from the server it's currently attached to.
    
    Example:
    - Detach volume: {"volume_id": 12345}
    """
    try:
        volume = client.volumes.get_by_id(params.volume_id)
        if not volume:
            return {"error": f"Volume with ID {params.volume_id} not found"}
        
        if not volume.server:
            return {"error": f"Volume with ID {params.volume_id} is not attached to any server"}
        
        action = client.volumes.detach(volume)
        
        # Format the response
        return {
            "success": True,
            "action": {
                "id": action.id,
                "status": action.status,
                "command": action.command,
                "progress": action.progress,
                "error": action.error,
                "started": action.started.isoformat() if action.started else None,
                "finished": action.finished.isoformat() if action.finished else None,
            } if action else None,
        }
    except Exception as e:
        return {"error": f"Failed to detach volume: {str(e)}"}

@mcp.tool()
def resize_volume(params: ResizeVolumeParams) -> Dict[str, Any]:
    """
    Resize a volume.
    
    Increases the size of a volume (size can only be increased, not decreased).
    
    Example:
    - Resize volume: {"volume_id": 12345, "size": 100}
    """
    try:
        volume = client.volumes.get_by_id(params.volume_id)
        if not volume:
            return {"error": f"Volume with ID {params.volume_id} not found"}
        
        if params.size <= volume.size:
            return {"error": f"New size ({params.size} GB) must be greater than current size ({volume.size} GB)"}
        
        action = client.volumes.resize(volume, params.size)
        
        # Format the response
        return {
            "success": True,
            "action": {
                "id": action.id,
                "status": action.status,
                "command": action.command,
                "progress": action.progress,
                "error": action.error,
                "started": action.started.isoformat() if action.started else None,
                "finished": action.finished.isoformat() if action.finished else None,
            } if action else None,
        }
    except Exception as e:
        return {"error": f"Failed to resize volume: {str(e)}"}

# SSH Key-related MCP tools

@mcp.tool()
def list_ssh_keys() -> Dict[str, Any]:
    """
    List all SSH keys in your Hetzner Cloud account.
    
    Returns a list of all SSH key instances with their details.
    
    Example:
    - Basic list: list_ssh_keys()
    """
    try:
        ssh_keys = client.ssh_keys.get_all()
        return {
            "ssh_keys": [ssh_key_to_dict(ssh_key) for ssh_key in ssh_keys]
        }
    except Exception as e:
        return {"error": f"Failed to list SSH keys: {str(e)}"}

@mcp.tool()
def get_ssh_key(params: SSHKeyIdParam) -> Dict[str, Any]:
    """
    Get details about a specific SSH key.
    
    Returns detailed information about an SSH key identified by its ID.
    
    Example:
    - Get SSH key details: {"ssh_key_id": 12345}
    """
    try:
        ssh_key = client.ssh_keys.get_by_id(params.ssh_key_id)
        if not ssh_key:
            return {"error": f"SSH key with ID {params.ssh_key_id} not found"}
        
        return {"ssh_key": ssh_key_to_dict(ssh_key)}
    except Exception as e:
        return {"error": f"Failed to get SSH key: {str(e)}"}

@mcp.tool()
def create_ssh_key(params: CreateSSHKeyParams) -> Dict[str, Any]:
    """
    Create a new SSH key.
    
    Creates a new SSH key with the specified name and public key data.
    
    Examples:
    - Basic SSH key: {"name": "my-ssh-key", "public_key": "ssh-rsa AAAAB3NzaC1..."}
    - With labels: {"name": "user-key", "public_key": "ssh-rsa AAAAB3NzaC1...", "labels": {"environment": "production"}}
    """
    try:
        ssh_key = client.ssh_keys.create(
            name=params.name,
            public_key=params.public_key,
            labels=params.labels
        )
        
        return {"ssh_key": ssh_key_to_dict(ssh_key)}
    except Exception as e:
        return {"error": f"Failed to create SSH key: {str(e)}"}

@mcp.tool()
def update_ssh_key(params: UpdateSSHKeyParams) -> Dict[str, Any]:
    """
    Update an SSH key.
    
    Updates the name or labels of an existing SSH key.
    
    Example:
    - Update name: {"ssh_key_id": 12345, "name": "new-key-name"}
    - Update labels: {"ssh_key_id": 12345, "name": "existing-name", "labels": {"environment": "staging"}}
    """
    try:
        ssh_key = client.ssh_keys.get_by_id(params.ssh_key_id)
        if not ssh_key:
            return {"error": f"SSH key with ID {params.ssh_key_id} not found"}
        
        updated_ssh_key = client.ssh_keys.update(
            ssh_key=ssh_key,
            name=params.name,
            labels=params.labels
        )
        
        return {"ssh_key": ssh_key_to_dict(updated_ssh_key)}
    except Exception as e:
        return {"error": f"Failed to update SSH key: {str(e)}"}

@mcp.tool()
def delete_ssh_key(params: SSHKeyIdParam) -> Dict[str, Any]:
    """
    Delete an SSH key.
    
    Permanently deletes an SSH key identified by its ID.
    
    Example:
    - Delete SSH key: {"ssh_key_id": 12345}
    """
    try:
        ssh_key = client.ssh_keys.get_by_id(params.ssh_key_id)
        if not ssh_key:
            return {"error": f"SSH key with ID {params.ssh_key_id} not found"}
            
        success = client.ssh_keys.delete(ssh_key)
        
        return {"success": success}
    except Exception as e:
        return {"error": f"Failed to delete SSH key: {str(e)}"}

# Network-related MCP tools

# Helper function to convert an Action object to dict
def _action_to_dict(action) -> Optional[Dict[str, Any]]:
    """Convert an hcloud Action object to a dictionary, or None if absent."""
    if not action:
        return None
    return {
        "id": action.id,
        "status": action.status,
        "command": action.command,
        "progress": action.progress,
        "error": action.error,
        "started": action.started.isoformat() if action.started else None,
        "finished": action.finished.isoformat() if action.finished else None,
    }

@mcp.tool()
def list_networks() -> Dict[str, Any]:
    """
    List all private networks in your Hetzner Cloud account.

    Returns a list of all networks with their subnets, routes, and attached servers.

    Example:
    - Basic list: list_networks()
    """
    try:
        networks = client.networks.get_all()
        return {
            "networks": [network_to_dict(network) for network in networks]
        }
    except Exception as e:
        return {"error": f"Failed to list networks: {str(e)}"}

@mcp.tool()
def get_network(params: NetworkIdParam) -> Dict[str, Any]:
    """
    Get details about a specific private network.

    Returns detailed information about a network identified by its ID.

    Example:
    - Get network details: {"network_id": 12345}
    """
    try:
        network = client.networks.get_by_id(params.network_id)
        if not network:
            return {"error": f"Network with ID {params.network_id} not found"}

        return {"network": network_to_dict(network)}
    except Exception as e:
        return {"error": f"Failed to get network: {str(e)}"}

@mcp.tool()
def create_network(params: CreateNetworkParams) -> Dict[str, Any]:
    """
    Create a new private network.

    Creates a private network spanning a given IP range. Add subnets afterwards
    with add_subnet before attaching servers.

    Examples:
    - Basic network: {"name": "internal", "ip_range": "10.0.0.0/16"}
    - With labels: {"name": "prod-net", "ip_range": "10.0.0.0/16", "labels": {"env": "production"}}
    """
    try:
        network = client.networks.create(
            name=params.name,
            ip_range=params.ip_range,
            expose_routes_to_vswitch=params.expose_routes_to_vswitch,
            labels=params.labels,
        )

        return {"network": network_to_dict(network)}
    except Exception as e:
        return {"error": f"Failed to create network: {str(e)}"}

@mcp.tool()
def update_network(params: UpdateNetworkParams) -> Dict[str, Any]:
    """
    Update a private network.

    Updates the name, labels, or vSwitch route exposure of an existing network.

    Examples:
    - Rename: {"network_id": 12345, "name": "new-name"}
    - Update labels: {"network_id": 12345, "labels": {"env": "staging"}}
    """
    try:
        network = client.networks.get_by_id(params.network_id)
        if not network:
            return {"error": f"Network with ID {params.network_id} not found"}

        updated = client.networks.update(
            network=network,
            name=params.name,
            expose_routes_to_vswitch=params.expose_routes_to_vswitch,
            labels=params.labels,
        )

        return {"network": network_to_dict(updated)}
    except Exception as e:
        return {"error": f"Failed to update network: {str(e)}"}

@mcp.tool()
def delete_network(params: NetworkIdParam) -> Dict[str, Any]:
    """
    Delete a private network.

    Permanently deletes a network identified by its ID. Servers must be detached first.

    Example:
    - Delete network: {"network_id": 12345}
    """
    try:
        network = client.networks.get_by_id(params.network_id)
        if not network:
            return {"error": f"Network with ID {params.network_id} not found"}

        success = client.networks.delete(network)

        return {"success": success}
    except Exception as e:
        return {"error": f"Failed to delete network: {str(e)}"}

@mcp.tool()
def add_subnet(params: AddSubnetParams) -> Dict[str, Any]:
    """
    Add a subnet to a private network.

    Servers can only be attached to a network that has at least one subnet.

    Examples:
    - Cloud subnet: {"network_id": 12345, "ip_range": "10.0.1.0/24", "network_zone": "eu-central"}
    - vSwitch subnet: {"network_id": 12345, "ip_range": "10.0.2.0/24", "network_zone": "eu-central", "type": "vswitch", "vswitch_id": 1000}
    """
    try:
        network = client.networks.get_by_id(params.network_id)
        if not network:
            return {"error": f"Network with ID {params.network_id} not found"}

        subnet = NetworkSubnet(
            ip_range=params.ip_range,
            type=params.type,
            network_zone=params.network_zone,
            vswitch_id=params.vswitch_id,
        )
        action = client.networks.add_subnet(network=network, subnet=subnet)

        return {"success": True, "action": _action_to_dict(action)}
    except Exception as e:
        return {"error": f"Failed to add subnet: {str(e)}"}

@mcp.tool()
def delete_subnet(params: DeleteSubnetParams) -> Dict[str, Any]:
    """
    Delete a subnet from a private network.

    The subnet must have no servers attached to it.

    Example:
    - Delete subnet: {"network_id": 12345, "ip_range": "10.0.1.0/24"}
    """
    try:
        network = client.networks.get_by_id(params.network_id)
        if not network:
            return {"error": f"Network with ID {params.network_id} not found"}

        subnet = NetworkSubnet(ip_range=params.ip_range)
        action = client.networks.delete_subnet(network=network, subnet=subnet)

        return {"success": True, "action": _action_to_dict(action)}
    except Exception as e:
        return {"error": f"Failed to delete subnet: {str(e)}"}

@mcp.tool()
def add_route(params: AddRouteParams) -> Dict[str, Any]:
    """
    Add a route to a private network.

    Routes direct traffic for a destination CIDR to a gateway inside the network.

    Example:
    - Add route: {"network_id": 12345, "destination": "10.100.1.0/24", "gateway": "10.0.1.1"}
    """
    try:
        network = client.networks.get_by_id(params.network_id)
        if not network:
            return {"error": f"Network with ID {params.network_id} not found"}

        route = NetworkRoute(destination=params.destination, gateway=params.gateway)
        action = client.networks.add_route(network=network, route=route)

        return {"success": True, "action": _action_to_dict(action)}
    except Exception as e:
        return {"error": f"Failed to add route: {str(e)}"}

@mcp.tool()
def delete_route(params: DeleteRouteParams) -> Dict[str, Any]:
    """
    Delete a route from a private network.

    Example:
    - Delete route: {"network_id": 12345, "destination": "10.100.1.0/24", "gateway": "10.0.1.1"}
    """
    try:
        network = client.networks.get_by_id(params.network_id)
        if not network:
            return {"error": f"Network with ID {params.network_id} not found"}

        route = NetworkRoute(destination=params.destination, gateway=params.gateway)
        action = client.networks.delete_route(network=network, route=route)

        return {"success": True, "action": _action_to_dict(action)}
    except Exception as e:
        return {"error": f"Failed to delete route: {str(e)}"}

@mcp.tool()
def attach_server_to_network(params: AttachServerToNetworkParams) -> Dict[str, Any]:
    """
    Attach a server to a private network.

    Gives the server a private interface on the network. Optionally pin a
    specific private IP and/or assign alias IPs.

    Examples:
    - Basic attach: {"network_id": 12345, "server_id": 67890}
    - With fixed IP: {"network_id": 12345, "server_id": 67890, "ip": "10.0.1.5"}
    """
    try:
        network = client.networks.get_by_id(params.network_id)
        if not network:
            return {"error": f"Network with ID {params.network_id} not found"}

        server = client.servers.get_by_id(params.server_id)
        if not server:
            return {"error": f"Server with ID {params.server_id} not found"}

        action = client.servers.attach_to_network(
            server=server,
            network=network,
            ip=params.ip,
            alias_ips=params.alias_ips,
        )

        return {"success": True, "action": _action_to_dict(action)}
    except Exception as e:
        return {"error": f"Failed to attach server to network: {str(e)}"}

@mcp.tool()
def detach_server_from_network(params: DetachServerFromNetworkParams) -> Dict[str, Any]:
    """
    Detach a server from a private network.

    Removes the server's private interface on the given network.

    Example:
    - Detach: {"network_id": 12345, "server_id": 67890}
    """
    try:
        network = client.networks.get_by_id(params.network_id)
        if not network:
            return {"error": f"Network with ID {params.network_id} not found"}

        server = client.servers.get_by_id(params.server_id)
        if not server:
            return {"error": f"Server with ID {params.server_id} not found"}

        action = client.servers.detach_from_network(server=server, network=network)

        return {"success": True, "action": _action_to_dict(action)}
    except Exception as e:
        return {"error": f"Failed to detach server from network: {str(e)}"}

# DNS-related MCP tools
#
# Hetzner DNS is a separate service with its own REST API and token, so these
# tools bypass the hcloud SDK and call the API directly.

def _dns_token() -> str:
    """Resolve the Hetzner DNS API token from the environment.

    Kept separate from the Cloud token (HCLOUD_TOKEN) because DNS uses a
    distinct credential. Raises RuntimeError with actionable guidance if unset.
    """
    token = os.environ.get("HETZNER_DNS_TOKEN")
    if not token:
        raise RuntimeError(
            "No Hetzner DNS API token found. Set the HETZNER_DNS_TOKEN "
            "environment variable (create one at "
            "https://dns.hetzner.com/settings/api-token). Note this is separate "
            "from the Hetzner Cloud HCLOUD_TOKEN."
        )
    return token


def _dns_request(
    method: str,
    path: str,
    params: Optional[Dict[str, Any]] = None,
    json_body: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Perform a request against the Hetzner DNS REST API.

    Rate-limit (HTTP 429) responses are retried transparently with the same
    exponential-backoff-with-jitter policy used for Cloud API calls. Returns the
    parsed JSON body (an empty dict for empty/no-content responses). Raises on
    non-2xx responses with the API's error message when available.
    """
    url = f"{DNS_API_BASE}{path}"
    headers = {"Auth-API-Token": _dns_token(), "Content-Type": "application/json"}

    attempt = 0
    while True:
        response = requests.request(
            method,
            url,
            headers=headers,
            params=params,
            json=json_body,
            timeout=DNS_REQUEST_TIMEOUT,
        )

        if response.status_code == 429 and attempt < RATE_LIMIT_MAX_RETRIES:
            ceiling = min(RATE_LIMIT_MAX_DELAY, RATE_LIMIT_BASE_DELAY * (2 ** attempt))
            delay = random.uniform(0, ceiling)
            attempt += 1
            logger.warning(
                "Hetzner DNS rate limit hit; retry %d/%d in %.2fs",
                attempt, RATE_LIMIT_MAX_RETRIES, delay,
            )
            time.sleep(delay)
            continue

        if not response.ok:
            # Surface the API's error message when it provides one.
            try:
                detail = response.json()
                message = detail.get("message") or detail.get("error") or detail
            except ValueError:
                message = response.text
            raise RuntimeError(f"HTTP {response.status_code}: {message}")

        if not response.content:
            return {}
        return response.json()

@mcp.tool()
def list_dns_zones(params: Optional[ListDNSZonesParams] = None) -> Dict[str, Any]:
    """
    List all DNS zones in your Hetzner DNS account.

    Optionally filter by exact name or partial search name.

    Examples:
    - All zones: list_dns_zones()
    - By name: {"name": "example.com"}
    - Search: {"search_name": "example"}
    """
    try:
        query: Dict[str, Any] = {}
        if params and params.name:
            query["name"] = params.name
        if params and params.search_name:
            query["search_name"] = params.search_name

        data = _dns_request("GET", "/zones", params=query or None)
        return {"zones": data.get("zones", [])}
    except Exception as e:
        return {"error": f"Failed to list DNS zones: {str(e)}"}

@mcp.tool()
def get_dns_zone(params: DNSZoneIdParam) -> Dict[str, Any]:
    """
    Get details about a specific DNS zone.

    Example:
    - Get zone: {"zone_id": "abc123"}
    """
    try:
        data = _dns_request("GET", f"/zones/{params.zone_id}")
        return {"zone": data.get("zone", data)}
    except Exception as e:
        return {"error": f"Failed to get DNS zone: {str(e)}"}

@mcp.tool()
def create_dns_zone(params: CreateDNSZoneParams) -> Dict[str, Any]:
    """
    Create a new DNS zone (domain).

    After creating the zone, point your domain's nameservers at Hetzner's
    (returned in the zone's 'ns' field) and add records with create_dns_record.

    Examples:
    - Basic: {"name": "example.com"}
    - With TTL: {"name": "example.com", "ttl": 86400}
    """
    try:
        body: Dict[str, Any] = {"name": params.name}
        if params.ttl is not None:
            body["ttl"] = params.ttl

        data = _dns_request("POST", "/zones", json_body=body)
        return {"zone": data.get("zone", data)}
    except Exception as e:
        return {"error": f"Failed to create DNS zone: {str(e)}"}

@mcp.tool()
def update_dns_zone(params: UpdateDNSZoneParams) -> Dict[str, Any]:
    """
    Update a DNS zone.

    Example:
    - Update TTL: {"zone_id": "abc123", "name": "example.com", "ttl": 3600}
    """
    try:
        body: Dict[str, Any] = {"name": params.name}
        if params.ttl is not None:
            body["ttl"] = params.ttl

        data = _dns_request("PUT", f"/zones/{params.zone_id}", json_body=body)
        return {"zone": data.get("zone", data)}
    except Exception as e:
        return {"error": f"Failed to update DNS zone: {str(e)}"}

@mcp.tool()
def delete_dns_zone(params: DNSZoneIdParam) -> Dict[str, Any]:
    """
    Delete a DNS zone.

    Permanently deletes the zone and all of its records.

    Example:
    - Delete zone: {"zone_id": "abc123"}
    """
    try:
        _dns_request("DELETE", f"/zones/{params.zone_id}")
        return {"success": True}
    except Exception as e:
        return {"error": f"Failed to delete DNS zone: {str(e)}"}

@mcp.tool()
def list_dns_records(params: ListDNSRecordsParams) -> Dict[str, Any]:
    """
    List all records in a DNS zone.

    Example:
    - List records: {"zone_id": "abc123"}
    """
    try:
        data = _dns_request("GET", "/records", params={"zone_id": params.zone_id})
        return {"records": data.get("records", [])}
    except Exception as e:
        return {"error": f"Failed to list DNS records: {str(e)}"}

@mcp.tool()
def get_dns_record(params: DNSRecordIdParam) -> Dict[str, Any]:
    """
    Get details about a specific DNS record.

    Example:
    - Get record: {"record_id": "rec123"}
    """
    try:
        data = _dns_request("GET", f"/records/{params.record_id}")
        return {"record": data.get("record", data)}
    except Exception as e:
        return {"error": f"Failed to get DNS record: {str(e)}"}

@mcp.tool()
def create_dns_record(params: CreateDNSRecordParams) -> Dict[str, Any]:
    """
    Create a new DNS record in a zone.

    Use '@' as the name for the zone root (apex).

    Examples:
    - A record: {"zone_id": "abc123", "type": "A", "name": "www", "value": "203.0.113.10"}
    - Root A record: {"zone_id": "abc123", "type": "A", "name": "@", "value": "203.0.113.10", "ttl": 3600}
    - CNAME: {"zone_id": "abc123", "type": "CNAME", "name": "blog", "value": "www.example.com."}
    """
    try:
        body: Dict[str, Any] = {
            "zone_id": params.zone_id,
            "type": params.type,
            "name": params.name,
            "value": params.value,
        }
        if params.ttl is not None:
            body["ttl"] = params.ttl

        data = _dns_request("POST", "/records", json_body=body)
        return {"record": data.get("record", data)}
    except Exception as e:
        return {"error": f"Failed to create DNS record: {str(e)}"}

@mcp.tool()
def update_dns_record(params: UpdateDNSRecordParams) -> Dict[str, Any]:
    """
    Update an existing DNS record.

    All core fields (zone_id, type, name, value) must be provided as the DNS API
    replaces the record.

    Example:
    - Update value: {"record_id": "rec123", "zone_id": "abc123", "type": "A", "name": "www", "value": "203.0.113.20"}
    """
    try:
        body: Dict[str, Any] = {
            "zone_id": params.zone_id,
            "type": params.type,
            "name": params.name,
            "value": params.value,
        }
        if params.ttl is not None:
            body["ttl"] = params.ttl

        data = _dns_request("PUT", f"/records/{params.record_id}", json_body=body)
        return {"record": data.get("record", data)}
    except Exception as e:
        return {"error": f"Failed to update DNS record: {str(e)}"}

@mcp.tool()
def delete_dns_record(params: DNSRecordIdParam) -> Dict[str, Any]:
    """
    Delete a DNS record.

    Example:
    - Delete record: {"record_id": "rec123"}
    """
    try:
        _dns_request("DELETE", f"/records/{params.record_id}")
        return {"success": True}
    except Exception as e:
        return {"error": f"Failed to delete DNS record: {str(e)}"}

@mcp.tool()
def bulk_create_dns_records(params: BulkCreateDNSRecordsParams) -> Dict[str, Any]:
    """
    Create multiple DNS records in a single request.

    Returns the created records along with any records the API rejected.

    Example:
    - {"records": [
        {"zone_id": "abc123", "type": "A", "name": "www", "value": "203.0.113.10"},
        {"zone_id": "abc123", "type": "A", "name": "api", "value": "203.0.113.11"}
      ]}
    """
    try:
        records = []
        for record in params.records:
            entry: Dict[str, Any] = {
                "zone_id": record.zone_id,
                "type": record.type,
                "name": record.name,
                "value": record.value,
            }
            if record.ttl is not None:
                entry["ttl"] = record.ttl
            records.append(entry)

        data = _dns_request("POST", "/records/bulk", json_body={"records": records})
        return {
            "records": data.get("records", []),
            "valid_records": data.get("valid_records", []),
            "invalid_records": data.get("invalid_records", []),
        }
    except Exception as e:
        return {"error": f"Failed to bulk create DNS records: {str(e)}"}

def start_server(transport="stdio", port=None):
    """Start the MCP server.
    
    Args:
        transport: The transport to use (stdio or sse)
        port: Optional port override
    """
    host = os.environ.get("MCP_HOST", "localhost")
    if port is None:
        port = int(os.environ.get("MCP_PORT", 8080))
    else:
        port = int(port)
        
    # Update the server port if it was specified (FastMCP reads its actual
    # runtime config from .settings, not a plain .port attribute)
    mcp.settings.host = host
    mcp.settings.port = port

    # Log to stderr only (stdout is reserved for the stdio JSON-RPC stream).
    logger.info("Starting Hetzner Cloud MCP server using %s transport%s",
                transport, f" on {host}:{port}" if transport == "sse" else "")
    # Run the server - this is a synchronous function that will block until the server stops
    mcp.run(transport=transport)

def main():
    """Entry point for the package."""
    import argparse

    # Parse command line arguments
    parser = argparse.ArgumentParser(description="Hetzner Cloud MCP Server")
    parser.add_argument("--transport", choices=["stdio", "sse"], default="stdio",
                        help="Transport to use (stdio or sse, default: stdio)")
    parser.add_argument("--port", type=int,
                        help="Port to use (overrides MCP_PORT environment variable)")
    parser.add_argument("--token",
                        help="Hetzner Cloud API token (overrides the HCLOUD_TOKEN environment variable)")
    args = parser.parse_args()

    # Resolve the API token and build the client before serving. Fail cleanly
    # (stderr + non-zero exit) rather than crashing mid-request if it's missing.
    try:
        resolve_client(args.token)
    except RuntimeError as exc:
        logger.error("%s", exc)
        sys.exit(1)

    # Run the MCP server - this is a blocking call
    start_server(transport=args.transport, port=args.port)

if __name__ == "__main__":
    main()