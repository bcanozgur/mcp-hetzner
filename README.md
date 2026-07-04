# Hetzner Cloud MCP Server

[![PyPI version](https://img.shields.io/pypi/v/mcp-hetzner.svg)](https://pypi.org/project/mcp-hetzner/)
[![Python versions](https://img.shields.io/pypi/pyversions/mcp-hetzner.svg)](https://pypi.org/project/mcp-hetzner/)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](https://opensource.org/licenses/MIT)

A Model Context Protocol (MCP) server for interacting with the Hetzner Cloud API. This server allows language models to manage Hetzner Cloud resources through structured functions.

![MCP Hetzner Demo](media/mcp-hetzner.gif)

## Features

- List, create, and manage Hetzner Cloud servers
- Create, attach, detach, and resize volumes
- Manage firewall rules and apply them to servers
- Create and manage private networks — subnets, routes, and server attachment
- Manage Hetzner DNS zones and records (separate DNS API token)
- Create and manage SSH keys for secure server access
- View available images, server types, and locations
- Power on/off and reboot servers
- Automatic retry with exponential backoff on API rate limits (HTTP 429)
- Works with any MCP client — Claude Code, Codex, Cursor, VS Code, Claude Desktop, and more
- Runs with a single `uvx` command; no manual install or virtualenv required

## Requirements

- A Hetzner Cloud API token ([how to get one](#getting-a-hetzner-cloud-api-token))
- [`uv`](https://docs.astral.sh/uv/) (provides `uvx`) — the recommended way to run the server.
  Install it with `curl -LsSf https://astral.sh/uv/install.sh | sh` (macOS/Linux) or
  `powershell -c "irm https://astral.sh/uv/install.ps1 | iex"` (Windows).
  Python 3.11+ is also supported if you prefer `pip`/`pipx`.

## Getting a Hetzner Cloud API token

1. Open the [Hetzner Cloud Console](https://console.hetzner.cloud).
2. Select your project → **Security** → **API Tokens**.
3. Click **Generate API Token**, give it **Read & Write** permission, and copy the token
   (it is shown only once).

You will pass this token to the MCP server through the `HCLOUD_TOKEN` environment variable.

## Installation

The server is published to [PyPI](https://pypi.org/project/mcp-hetzner/) and runs with `uvx`
(the Python equivalent of `npx`). `uvx` downloads the package into an isolated, cached
environment and runs it — nothing is installed globally:

```bash
uvx mcp-hetzner
```

> **Tip:** To run the very latest unreleased code straight from Git instead of PyPI, replace
> `mcp-hetzner` with `--from git+https://github.com/bcanozgur/mcp-hetzner.git mcp-hetzner` in any
> command or config below.

In every example, replace `your_token_here` with your real Hetzner Cloud API token
(see [Getting a Hetzner Cloud API token](#getting-a-hetzner-cloud-api-token)).

### Claude Code

Run this from the project where you actually want to use the server — by default `claude mcp
add` scopes the server to your current working directory, so add it from inside whichever repo
you'll be driving Hetzner from (you don't need to clone this repo to do that; `uvx mcp-hetzner`
fetches the published package). Inject your token via `-e`:

```bash
claude mcp add hetzner -e HCLOUD_TOKEN=your_token_here -- uvx mcp-hetzner
```

- Add `--scope user` to make it available across all your projects regardless of which directory
  you run the command from, or `--scope project` to commit it to a shared `.mcp.json` for your team.
- Verify it connected with `claude mcp list` (should report `hetzner … ✔ Connected`).
- In a session, `/mcp` lists the active servers and their tools.

### Codex

Add the server with the `codex mcp add` command:

```bash
codex mcp add hetzner --env HCLOUD_TOKEN=your_token_here -- uvx mcp-hetzner
```

Or edit `~/.codex/config.toml` directly (note the underscore in `mcp_servers`):

```toml
[mcp_servers.hetzner]
command = "uvx"
args = ["mcp-hetzner"]

[mcp_servers.hetzner.env]
HCLOUD_TOKEN = "your_token_here"
```

In the Codex TUI, run `/mcp` to confirm the server is active.

### Cursor

Open **Cursor Settings → MCP → Add new MCP Server**, or add this to
`~/.cursor/mcp.json` (global) or `<project>/.cursor/mcp.json` (per-project):

```json
{
  "mcpServers": {
    "hetzner": {
      "command": "uvx",
      "args": ["mcp-hetzner"],
      "env": {
        "HCLOUD_TOKEN": "your_token_here"
      }
    }
  }
}
```

### Other MCP clients (VS Code, Claude Desktop, Windsurf, …)

Every MCP client that supports stdio servers uses the same shape — a `command`, `args`, and an
`env` block. Drop the JSON above into the client's MCP config file:

- **VS Code** (GitHub Copilot / MCP extensions): `.vscode/mcp.json`
- **Claude Desktop**: `claude_desktop_config.json` (Settings → Developer → Edit Config)
- **Windsurf**: `~/.codeium/windsurf/mcp_config.json`

> **PATH note:** MCP clients launched from a GUI may not inherit your shell's `PATH`, so a bare
> `"command": "uvx"` can fail with `spawn uvx ENOENT`. If that happens, use the absolute path —
> find it with `which uvx` (macOS/Linux) or `where uvx` (Windows), e.g.
> `/opt/homebrew/bin/uvx`.

## Configuration

| Environment variable    | Required | Default     | Description                                                        |
| ----------------------- | -------- | ----------- | ------------------------------------------------------------------ |
| `HCLOUD_TOKEN`          | **Yes**  | –           | Hetzner Cloud API token (Read & Write).                            |
| `HETZNER_DNS_TOKEN`     | For DNS  | –           | Hetzner DNS API token — required only for the DNS tools. This is a **separate** credential from `HCLOUD_TOKEN`; create one at <https://dns.hetzner.com/settings/api-token>. |
| `MCP_HETZNER_LOG_LEVEL` | No       | `INFO`      | Log verbosity (`DEBUG`, `INFO`, `WARNING`, `ERROR`). Logs go to stderr. |
| `MCP_HETZNER_MAX_RETRIES` | No     | `5`         | Max retries for rate-limited (HTTP 429) API calls.                 |
| `MCP_HOST`              | No       | `localhost` | Bind host, used only by the `sse` transport.                       |
| `MCP_PORT`              | No       | `8080`      | Bind port, used only by the `sse` transport.                       |

> **Note:** DNS tools use Hetzner's separate DNS API, so they need `HETZNER_DNS_TOKEN`
> in addition to `HCLOUD_TOKEN`. The Cloud tools (servers, volumes, firewalls, networks,
> SSH keys) work with `HCLOUD_TOKEN` alone.

The token can be supplied three ways, in order of precedence:

1. The `--token` command-line flag.
2. The `HCLOUD_TOKEN` environment variable — **recommended**; inject it via your MCP client's
   `env` block as shown above.
3. A `.env` file in the working directory containing `HCLOUD_TOKEN=…` — convenient for local
   development. Copy `.env.example` to `.env` to get started.

## Running from source (development)

```bash
# Clone your fork and install in editable mode
git clone https://github.com/bcanozgur/mcp-hetzner.git
cd mcp-hetzner
pip install -e .

# Provide a token for local runs
cp .env.example .env   # then edit .env and set HCLOUD_TOKEN

# Run the server (stdio is the default transport used by MCP clients)
mcp-hetzner
# or as a module
python -m mcp_hetzner
```

Command-line options:

```bash
mcp-hetzner --help
mcp-hetzner --token <TOKEN>          # override HCLOUD_TOKEN
mcp-hetzner --transport sse --port 8000   # HTTP/SSE transport instead of stdio
```

To point an MCP client at your local checkout without installing, use `uvx --from .`:

```bash
claude mcp add hetzner -e HCLOUD_TOKEN=your_token_here -- uvx --from /path/to/mcp-hetzner mcp-hetzner
```

The repository also ships a ready-to-use `.mcp.json` that runs the server from the checkout via
`uvx --from .` and reads `HCLOUD_TOKEN` from your shell environment.

## Publishing to PyPI

The package uses a single-sourced version (`mcp_hetzner.__version__`) and is ready to publish so
that `uvx mcp-hetzner` works for everyone:

```bash
pip install -e ".[dev]"      # installs build + twine
python -m build              # produces dist/*.whl and dist/*.tar.gz
python -m twine upload dist/*
```

Bump `__version__` in `mcp_hetzner/__init__.py` before each release.

## Example Workflows

### Basic Server Management

```
# List all your servers
list_servers

# Create a new server
create_server {
  "name": "web-server", 
  "server_type": "cx11", 
  "image": "ubuntu-22.04"
}

# Power operations
power_off {"server_id": 12345}
power_on {"server_id": 12345}
reboot {"server_id": 12345}

# Delete a server when no longer needed
delete_server {"server_id": 12345}
```

### Volume Management

```
# List all volumes
list_volumes

# Create a new volume
create_volume {
  "name": "data-volume",
  "size": 10,
  "location": "nbg1",
  "format": "ext4"
}

# Attach volume to a server
attach_volume {
  "volume_id": 12345,
  "server_id": 67890,
  "automount": true
}

# Detach volume from server
detach_volume {
  "volume_id": 12345
}

# Resize a volume (can only increase size)
resize_volume {
  "volume_id": 12345,
  "size": 50
}

# Delete a volume when no longer needed
delete_volume {
  "volume_id": 12345
}
```

### Firewall Management

```
# List all firewalls
list_firewalls

# Create a firewall for web servers
create_firewall {
  "name": "web-firewall",
  "rules": [
    {
      "direction": "in",
      "protocol": "tcp",
      "port": "80",
      "source_ips": ["0.0.0.0/0", "::/0"]
    },
    {
      "direction": "in",
      "protocol": "tcp",
      "port": "443",
      "source_ips": ["0.0.0.0/0", "::/0"]
    }
  ]
}

# Apply firewall to a server
apply_firewall_to_resources {
  "firewall_id": 12345,
  "resources": [
    {
      "type": "server",
      "server_id": 67890
    }
  ]
}
```

### SSH Key Management

```
# List all SSH keys
list_ssh_keys

# Create a new SSH key
create_ssh_key {
  "name": "my-laptop",
  "public_key": "ssh-rsa AAAAB3NzaC1yc2EAAA... user@laptop"
}

# Use the SSH key when creating a server
create_server {
  "name": "secure-server",
  "server_type": "cx11",
  "image": "ubuntu-22.04",
  "ssh_keys": [12345]
}

# Update an SSH key's name
update_ssh_key {
  "ssh_key_id": 12345,
  "name": "work-laptop"
}

# Delete an SSH key
delete_ssh_key {
  "ssh_key_id": 12345
}
```

### Private Network Management

```
# Create a private network
create_network {
  "name": "internal",
  "ip_range": "10.0.0.0/16"
}

# Add a subnet (required before attaching servers)
add_subnet {
  "network_id": 12345,
  "ip_range": "10.0.1.0/24",
  "network_zone": "eu-central"
}

# Attach a server to the network
attach_server_to_network {
  "network_id": 12345,
  "server_id": 67890,
  "ip": "10.0.1.5"
}

# Add a route
add_route {
  "network_id": 12345,
  "destination": "10.100.1.0/24",
  "gateway": "10.0.1.1"
}
```

### DNS Management

> Requires `HETZNER_DNS_TOKEN` (separate from `HCLOUD_TOKEN`).

```
# List DNS zones
list_dns_zones

# Create a zone (domain)
create_dns_zone {
  "name": "example.com"
}

# Add an A record (use "@" for the zone root)
create_dns_record {
  "zone_id": "abc123",
  "type": "A",
  "name": "www",
  "value": "203.0.113.10",
  "ttl": 3600
}

# Create several records at once
bulk_create_dns_records {
  "records": [
    { "zone_id": "abc123", "type": "A", "name": "@", "value": "203.0.113.10" },
    { "zone_id": "abc123", "type": "CNAME", "name": "blog", "value": "www.example.com." }
  ]
}
```

### Infrastructure Planning

```
# Explore available resources
list_server_types
list_images
list_locations

# Get specific server information
get_server {"server_id": 12345}
```

## Available Functions

The MCP server provides the following functions:

### Server Management
- `list_servers`: List all servers in your Hetzner Cloud account
- `get_server`: Get details about a specific server
- `create_server`: Create a new server
- `delete_server`: Delete a server
- `power_on`: Power on a server
- `power_off`: Power off a server
- `reboot`: Reboot a server

### Volume Management
- `list_volumes`: List all volumes in your Hetzner Cloud account
- `get_volume`: Get details about a specific volume
- `create_volume`: Create a new volume
- `delete_volume`: Delete a volume
- `attach_volume`: Attach a volume to a server
- `detach_volume`: Detach a volume from a server
- `resize_volume`: Increase the size of a volume

### Firewall Management
- `list_firewalls`: List all firewalls in your Hetzner Cloud account
- `get_firewall`: Get details about a specific firewall
- `create_firewall`: Create a new firewall
- `update_firewall`: Update firewall name or labels
- `delete_firewall`: Delete a firewall
- `set_firewall_rules`: Set or update firewall rules
- `apply_firewall_to_resources`: Apply a firewall to servers or server groups
- `remove_firewall_from_resources`: Remove a firewall from servers or server groups

### SSH Key Management
- `list_ssh_keys`: List all SSH keys in your Hetzner Cloud account
- `get_ssh_key`: Get details about a specific SSH key
- `create_ssh_key`: Create a new SSH key
- `update_ssh_key`: Update SSH key name or labels
- `delete_ssh_key`: Delete an SSH key

### Information
- `list_images`: List available OS images
- `list_server_types`: List available server types
- `list_locations`: List available datacenter locations

## License

MIT