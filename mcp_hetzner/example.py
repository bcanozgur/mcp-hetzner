#!/usr/bin/env python
"""
Hetzner Cloud MCP Example - Examples for using the Hetzner Cloud MCP
"""

import asyncio
import logging
import os
import sys
from pprint import pprint

import dotenv
from mcp.client import Client

# Setup logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# Load environment variables from the current working directory (not the
# installed package location), so a .env in the user's project is found.
dotenv.load_dotenv(dotenv.find_dotenv(usecwd=True))

# Check if Hetzner Cloud API token is configured
HCLOUD_TOKEN = os.environ.get("HCLOUD_TOKEN")
if not HCLOUD_TOKEN:
    print("Error: HCLOUD_TOKEN environment variable not set. Please add it to your .env file.")
    sys.exit(1)

async def run_example():
    """Run examples of Hetzner Cloud MCP usage."""
    # Connect to the MCP server
    host = os.environ.get("MCP_HOST", "localhost")
    port = int(os.environ.get("MCP_PORT", 8080))
    client = Client(f"http://{host}:{port}")
    
    # Get available server types
    server_types = await client.invoke("list_server_types")
    print("Available server types:")
    for server_type in server_types["server_types"][:3]:  # Show first 3 for brevity
        print(f"- {server_type['name']}: {server_type['cores']} Cores, {server_type['memory_gb']} GB RAM, {server_type['disk_gb']} GB Disk")
    print()
    
    # Get available locations
    locations = await client.invoke("list_locations")
    print("Available locations:")
    for location in locations["locations"]:
        print(f"- {location['name']}: {location['description']} ({location['country']}, {location['city']})")
    print()
    
    # List servers
    servers = await client.invoke("list_servers")
    print(f"Current servers: {len(servers['servers'])}")
    for server in servers["servers"]:
        print(f"- {server['name']} (ID: {server['id']}): {server['status']}, IP: {server['public_net']['ipv4']}")
    print()
    
    # List volumes
    volumes = await client.invoke("list_volumes")
    print(f"Current volumes: {len(volumes.get('volumes', []))}")
    for volume in volumes.get('volumes', []):
        print(f"- {volume['name']} (ID: {volume['id']}): {volume['size']} GB, Server: {volume['server']}")
    print()
    
    # Create a volume example (commented out to prevent actual creation)
    """
    print("Creating a new volume...")
    new_volume = await client.invoke("create_volume", {
        "name": "example-volume",
        "size": 10,  # 10 GB
        "location": "nbg1",  # Nuremberg
        "format": "ext4"
    })
    
    if "error" in new_volume:
        print(f"Error creating volume: {new_volume['error']}")
    else:
        print(f"Volume created: {new_volume['volume']['name']} (ID: {new_volume['volume']['id']})")
        
        # Attach volume to a server (if you have a server)
        if servers['servers']:
            server_id = servers['servers'][0]['id']
            print(f"Attaching volume to server {server_id}...")
            
            attach_result = await client.invoke("attach_volume", {
                "volume_id": new_volume['volume']['id'],
                "server_id": server_id,
                "automount": True
            })
            
            if "error" in attach_result:
                print(f"Error attaching volume: {attach_result['error']}")
            else:
                print("Volume attached successfully")
                
            # Wait for a moment before detaching
            await asyncio.sleep(10)
            
            # Detach the volume
            print("Detaching volume...")
            detach_result = await client.invoke("detach_volume", {
                "volume_id": new_volume['volume']['id']
            })
            
            if "error" in detach_result:
                print(f"Error detaching volume: {detach_result['error']}")
            else:
                print("Volume detached successfully")
            
        # Delete the volume
        print("Deleting volume...")
        delete_result = await client.invoke("delete_volume", {
            "volume_id": new_volume['volume']['id']
        })
        
        if "error" in delete_result:
            print(f"Error deleting volume: {delete_result['error']}")
        else:
            print("Volume deleted successfully")
    """

def main():
    """Entry point for the example script."""
    asyncio.run(run_example())

if __name__ == "__main__":
    main()