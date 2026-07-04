# Hetzner Cloud MCP Server

A Model Context Protocol (MCP) server for interacting with the Hetzner Cloud API. This server allows language models to manage Hetzner Cloud resources through structured functions.

![MCP Hetzner Demo](media/mcp-hetzner.gif)

## Features

- List, create, and manage Hetzner Cloud servers
- Create, attach, detach, and resize volumes
- Manage firewall rules and apply them to servers
- Create and manage SSH keys for secure server access
- View available images, server types, and locations
- Power on/off and reboot servers
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

The server is distributed as a Python package and runs with `uvx` (the Python equivalent of
`npx`). `uvx` downloads the package into an isolated, cached environment and runs it — nothing
is installed globally. Once published to PyPI you can reference it simply as `mcp-hetzner`; until
then (or to always track the latest code), reference it straight from the Git repository:

```bash
# From PyPI (once published)
uvx mcp-hetzner

# Straight from the Git repository (works today, no PyPI needed)
uvx --from git+https://github.com/bcanozgur/mcp-hetzner.git mcp-hetzner
```

The sections below show how to register it with each MCP client. Everywhere you see
`"args": ["mcp-hetzner"]` (or `-- uvx mcp-hetzner`), that is the **PyPI form**.

> **⚠️ Not on PyPI yet?** Until the package is published, `uvx mcp-hetzner` will fail with
> "package not found". Use the **Git form** instead — swap `mcp-hetzner` for
> `--from git+https://github.com/bcanozgur/mcp-hetzner.git mcp-hetzner` in any command or config
> below. Each section shows the exact Git-form command so you can copy‑paste it directly.

In every example, replace `your_token_here` with your real Hetzner Cloud API token
(see [Getting a Hetzner Cloud API token](#getting-a-hetzner-cloud-api-token)).

### Claude Code

Add the server with the built-in `claude mcp add` command, injecting your token via `-e`:

```bash
# PyPI form (once published)
claude mcp add hetzner -e HCLOUD_TOKEN=your_token_here -- uvx mcp-hetzner

# Git form (works right now, before publishing)
claude mcp add hetzner -e HCLOUD_TOKEN=your_token_here -- \
  uvx --from git+https://github.com/bcanozgur/mcp-hetzner.git mcp-hetzner
```

- Add `--scope user` to make it available across all your projects, or `--scope project` to
  commit it to a shared `.mcp.json` for your team.
- Verify it connected with `claude mcp list` (should report `hetzner … ✔ Connected`).
- In a session, `/mcp` lists the active servers and their tools.

### Codex

Add the server with the `codex mcp add` command:

```bash
# PyPI form (once published)
codex mcp add hetzner --env HCLOUD_TOKEN=your_token_here -- uvx mcp-hetzner

# Git form (works right now, before publishing)
codex mcp add hetzner --env HCLOUD_TOKEN=your_token_here -- \
  uvx --from git+https://github.com/bcanozgur/mcp-hetzner.git mcp-hetzner
```

Or edit `~/.codex/config.toml` directly (note the underscore in `mcp_servers`):

```toml
[mcp_servers.hetzner]
command = "uvx"
# PyPI form:  args = ["mcp-hetzner"]
# Git form (before publishing):
args = ["--from", "git+https://github.com/bcanozgur/mcp-hetzner.git", "mcp-hetzner"]

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
      "args": ["--from", "git+https://github.com/bcanozgur/mcp-hetzner.git", "mcp-hetzner"],
      "env": {
        "HCLOUD_TOKEN": "your_token_here"
      }
    }
  }
}
```

> Once the package is on PyPI, simplify `args` to just `["mcp-hetzner"]`.

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
| `MCP_HETZNER_LOG_LEVEL` | No       | `INFO`      | Log verbosity (`DEBUG`, `INFO`, `WARNING`, `ERROR`). Logs go to stderr. |
| `MCP_HOST`              | No       | `localhost` | Bind host, used only by the `sse` transport.                       |
| `MCP_PORT`              | No       | `8080`      | Bind port, used only by the `sse` transport.                       |

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