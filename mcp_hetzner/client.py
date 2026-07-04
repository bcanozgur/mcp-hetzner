#!/usr/bin/env python
"""
Simple client for the Hetzner Cloud MCP server.
"""

import asyncio
import json
import dotenv
import os
import sys

from mcp.client import Client as MCPClient

# Load environment variables from the current working directory (not the
# installed package location), so a .env in the user's project is found.
dotenv.load_dotenv(dotenv.find_dotenv(usecwd=True))

# Check if Hetzner Cloud API token is configured
if "HCLOUD_TOKEN" not in os.environ:
    print("Error: HCLOUD_TOKEN environment variable not set. Please add it to your .env file.")
    sys.exit(1)

async def run_test_client():
    """Run a test client to verify MCP server functionality."""
    # Connect to the MCP server
    host = os.environ.get("MCP_HOST", "localhost")
    port = int(os.environ.get("MCP_PORT", 8080))
    print(f"Connecting to MCP server at {host}:{port}...")
    
    client = MCPClient(f"http://{host}:{port}")
    
    try:
        # Test list_server_types
        print("\nTesting list_server_types...")
        response = await client.invoke("list_server_types")
        print(f"Available server types: {len(response['server_types'])}")
        for st in response['server_types'][:3]:  # Print first 3 as example
            print(f"- {st['name']}: {st['cores']} cores, {st['memory_gb']} GB RAM, {st['disk_gb']} GB disk")
        
        # Test list_images
        print("\nTesting list_images...")
        response = await client.invoke("list_images")
        print(f"Available images: {len(response['images'])}")
        for img in response['images'][:3]:  # Print first 3 as example
            print(f"- {img['name'] or img['id']}: {img['description'] or 'No description'}")
        
        # Test list_locations
        print("\nTesting list_locations...")
        response = await client.invoke("list_locations")
        print(f"Available locations: {len(response['locations'])}")
        for loc in response['locations']:
            print(f"- {loc['name']}: {loc['description']} ({loc['city']}, {loc['country']})")
        
        # Test list_servers
        print("\nTesting list_servers...")
        response = await client.invoke("list_servers")
        print(f"Current servers: {len(response['servers'])}")
        for server in response['servers']:
            print(f"- {server['name']} (ID: {server['id']}): {server['status']}")
            
        print("\nAll tests completed successfully!")
    except Exception as e:
        print(f"Error during test: {e}")

def main():
    """Entry point for the client script."""
    asyncio.run(run_test_client())

if __name__ == "__main__":
    main()