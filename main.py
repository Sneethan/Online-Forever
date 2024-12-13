import os
import sys
import json
import asyncio
import platform
from typing import Literal, Optional, Dict, Any

import requests
import websockets
import aiohttp
import backoff
from colorama import init, Fore
from dotenv import load_dotenv

from config import Config
from keep_alive import keep_alive

init(autoreset=True)
load_dotenv()

# Constants
API_BASE_URL = "https://discord.com/api/v9"
GATEWAY_URL = "wss://gateway.discord.gg/?v=9&encoding=json"
STATUS_TYPES = Literal["online", "dnd", "idle"]

class DiscordClient:
    def __init__(self, token: str):
        self.token = token
        self.headers = {
            "Authorization": token,
            "Content-Type": "application/json",
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
        }
        self.user_info: Optional[Dict[str, Any]] = None
        self.session: Optional[aiohttp.ClientSession] = None
        self.heartbeat_interval: Optional[float] = None
        self.last_sequence: Optional[int] = None
        self._shutdown_event = asyncio.Event()
        self._tasks = set()

    async def __aenter__(self):
        self.session = aiohttp.ClientSession()
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        await self.cleanup()

    async def cleanup(self):
        """Cleanup resources and cancel running tasks"""
        self._shutdown_event.set()
        
        # Cancel all running tasks
        for task in self._tasks:
            task.cancel()
        
        if self._tasks:
            await asyncio.gather(*self._tasks, return_exceptions=True)
        
        if self.session:
            await self.session.close()

    @backoff.on_exception(backoff.expo, aiohttp.ClientError, max_tries=5)
    async def validate_token(self) -> bool:
        if not self.session:
            self.session = aiohttp.ClientSession()
            
        try:
            async with self.session.get(f"{API_BASE_URL}/users/@me", headers=self.headers) as response:
                response.raise_for_status()
                self.user_info = await response.json()
                return True
        except aiohttp.ClientError as e:
            print(f"{Fore.WHITE}[{Fore.RED}-{Fore.WHITE}] Token validation failed: {str(e)}")
            return False

    @backoff.on_exception(backoff.expo, aiohttp.ClientError, max_tries=3)
    async def update_display_name(self, new_name: str) -> bool:
        if not self.session:
            self.session = aiohttp.ClientSession()
            
        try:
            headers = {
                "accept": "*/*",
                "accept-language": "en-US,en;q=0.9",
                "authorization": self.token,
                "content-type": "application/json",
                "cookie": "__dcfduid=1ab54f40029411efa8ae3934cfc72c02; __sdcfduid=1ab54f41029411efa8ae3934cfc72c026fe59ef729296da960d5edf097cb676b6d3a10380b40caae7bb21089a7d939d8; locale=en-US",
                "origin": "https://discord.com",
                "referer": "https://discord.com/channels/@me",
                "sec-ch-ua": '"Not_A Brand";v="8", "Chromium";v="120", "Google Chrome";v="120"',
                "sec-ch-ua-mobile": "?0",
                "sec-ch-ua-platform": '"Windows"',
                "sec-fetch-dest": "empty",
                "sec-fetch-mode": "cors",
                "sec-fetch-site": "same-origin",
                "user-agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
                "x-debug-options": "bugReporterEnabled",
                "x-discord-locale": "en-US",
                "x-discord-timezone": "America/New_York",
                "x-super-properties": "eyJvcyI6IldpbmRvd3MiLCJicm93c2VyIjoiQ2hyb21lIiwiZGV2aWNlIjoiIiwic3lzdGVtX2xvY2FsZSI6ImVuLVVTIiwiYnJvd3Nlcl91c2VyX2FnZW50IjoiTW96aWxsYS81LjAgKFdpbmRvd3MgTlQgMTAuMDsgV2luNjQ7IHg2NCkgQXBwbGVXZWJLaXQvNTM3LjM2IChLSFRNTCwgbGlrZSBHZWNrbykgQ2hyb21lLzEyMC4wLjAuMCBTYWZhcmkvNTM3LjM2IiwiYnJvd3Nlcl92ZXJzaW9uIjoiMTIwLjAuMC4wIiwib3NfdmVyc2lvbiI6IjEwIiwicmVmZXJyZXIiOiIiLCJyZWZlcnJpbmdfZG9tYWluIjoiIiwicmVmZXJyZXJfY3VycmVudCI6IiIsInJlZmVycmluZ19kb21haW5fY3VycmVudCI6IiIsInJlbGVhc2VfY2hhbm5lbCI6InN0YWJsZSIsImNsaWVudF9idWlsZF9udW1iZXIiOjI1MDgzMiwiY2xpZW50X2V2ZW50X3NvdXJjZSI6bnVsbH0="
            }
            
            payload = {
                "global_name": new_name
            }
            
            async with self.session.patch(
                f"{API_BASE_URL}/users/@me",
                headers=headers,
                json=payload
            ) as response:
                if response.status == 400:
                    error_data = await response.json()
                    print(f"{Fore.WHITE}[{Fore.RED}-{Fore.WHITE}] Failed to update display name: {error_data.get('message', 'Unknown error')}")
                    return False
                    
                response.raise_for_status()
                response_data = await response.json()
                print(f"{Fore.WHITE}[{Fore.LIGHTGREEN_EX}+{Fore.WHITE}] Display name updated to {Fore.LIGHTBLUE_EX}{new_name}{Fore.WHITE}!")
                return True
                
        except aiohttp.ClientError as e:
            print(f"{Fore.WHITE}[{Fore.RED}-{Fore.WHITE}] Failed to update display name: {str(e)}")
            return False

    async def maintain_presence(self, status: STATUS_TYPES, custom_status: str):
        retry_count = 0
        max_retries = 5
        
        while retry_count < max_retries and not self._shutdown_event.is_set():
            try:
                async with websockets.connect(GATEWAY_URL) as ws:
                    payload = json.loads(await ws.recv())
                    self.heartbeat_interval = payload["d"]["heartbeat_interval"] / 1000
                    
                    # Handle identification
                    await self._send_auth(ws, status)
                    await self._send_custom_status(ws, status, custom_status)
                    
                    # Start heartbeat in separate task
                    heartbeat_task = asyncio.create_task(self._heartbeat_loop(ws))
                    self._tasks.add(heartbeat_task)
                    
                    try:
                        while not self._shutdown_event.is_set():
                            try:
                                msg = await asyncio.wait_for(ws.recv(), timeout=30)
                                data = json.loads(msg)
                                
                                if data["op"] == 11:  # Heartbeat ACK
                                    continue
                                
                                if data["op"] == 7:  # Reconnect
                                    break
                                    
                                if data["s"] is not None:
                                    self.last_sequence = data["s"]
                                    
                            except asyncio.TimeoutError:
                                continue
                                
                    except websockets.ConnectionClosed:
                        if not self._shutdown_event.is_set():
                            print(f"{Fore.WHITE}[{Fore.YELLOW}!{Fore.WHITE}] Connection closed, attempting to reconnect...")
                    finally:
                        self._tasks.remove(heartbeat_task)
                        heartbeat_task.cancel()
                        
            except Exception as e:
                if self._shutdown_event.is_set():
                    break
                    
                retry_count += 1
                wait_time = min(retry_count * 5, 30)
                print(f"{Fore.WHITE}[{Fore.RED}-{Fore.WHITE}] Connection error: {str(e)}")
                print(f"{Fore.WHITE}[{Fore.YELLOW}!{Fore.WHITE}] Retrying in {wait_time} seconds...")
                await asyncio.sleep(wait_time)
        
        if not self._shutdown_event.is_set():
            print(f"{Fore.WHITE}[{Fore.RED}-{Fore.WHITE}] Max retries reached. Exiting...")

    async def _heartbeat_loop(self, ws):
        try:
            while not self._shutdown_event.is_set():
                await self._send_heartbeat(ws)
                await asyncio.sleep(self.heartbeat_interval)
        except asyncio.CancelledError:
            pass

    async def _send_heartbeat(self, ws):
        await ws.send(json.dumps({
            "op": 1,
            "d": self.last_sequence
        }))

    async def _send_auth(self, ws, status: STATUS_TYPES):
        auth = {
            "op": 2,
            "d": {
                "token": self.token,
                "properties": {
                    "$os": "Windows 10",
                    "$browser": "Google Chrome",
                    "$device": "Windows",
                },
                "presence": {"status": status, "afk": False},
            },
        }
        await ws.send(json.dumps(auth))

    async def _send_custom_status(self, ws, status: STATUS_TYPES, custom_status: str):
        payload = {
            "op": 3,
            "d": {
                "since": 0,
                "activities": [
                    {
                        "type": 4,
                        "state": custom_status,
                        "name": "Custom Status",
                        "id": "custom",
                    }
                ],
                "status": status,
                "afk": False,
            },
        }
        await ws.send(json.dumps(payload))

async def main():
    client = None
    try:
        token = os.getenv("TOKEN")
        if not token:
            print(f"{Fore.WHITE}[{Fore.RED}-{Fore.WHITE}] Please add a token inside Secrets.")
            return

        # Clear screen
        os.system("cls" if platform.system() == "Windows" else "clear")

        async with DiscordClient(token) as client:
            # First validate token and get user info
            if not await client.validate_token():
                return

            print(f"{Fore.WHITE}[{Fore.LIGHTGREEN_EX}+{Fore.WHITE}] Logged in as "
                  f"{Fore.LIGHTBLUE_EX}{client.user_info['username']}"
                  f"{Fore.WHITE}({client.user_info['id']})!")

            # Wait a short moment to ensure we're fully logged in
            await asyncio.sleep(1)

            # Then attempt to update display name
            if Config.DISPLAY_NAME:
                if await client.update_display_name(Config.DISPLAY_NAME):
                    print(f"{Fore.WHITE}[{Fore.LIGHTGREEN_EX}+{Fore.WHITE}] Successfully updated display name!")
                else:
                    print(f"{Fore.WHITE}[{Fore.YELLOW}!{Fore.WHITE}] Continuing without updating display name...")

            # Finally start presence maintenance
            await client.maintain_presence(Config.STATUS, Config.CUSTOM_STATUS)

    except KeyboardInterrupt:
        print(f"\n{Fore.WHITE}[{Fore.YELLOW}!{Fore.WHITE}] Shutting down gracefully...")
    except Exception as e:
        print(f"{Fore.WHITE}[{Fore.RED}-{Fore.WHITE}] Fatal error: {str(e)}")
    finally:
        if client:
            await client.cleanup()

if __name__ == "__main__":
    keep_alive()
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print(f"\n{Fore.WHITE}[{Fore.YELLOW}!{Fore.WHITE}] Shutting down...")
    except Exception as e:
        print(f"{Fore.WHITE}[{Fore.RED}-{Fore.WHITE}] Fatal error: {str(e)}")
