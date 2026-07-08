#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.10"
# dependencies = [
#     "miservice_fork",
# ]
# ///
"""Xiaomi MiService login & token extractor.

Run this on your Mac to login to Xiaomi and save the token file.
The token can then be used on the NAS without triggering 2FA.

Usage:
    uv run xiaomi_login.py
"""

import asyncio
import json
import os
import sys

import aiohttp
from miservice import MiAccount, MiNAService


async def main():
    user = "zk.chan007@gmail.com"
    pw = input("Xiaomi password: ").strip()

    token_path = os.path.expanduser("~/.mi.token")

    async with aiohttp.ClientSession() as session:
        account = MiAccount(session, user, pw, token_path)
        mina = MiNAService(account)

        try:
            devs = await mina.device_list()
            if not devs:
                print("\nNo MiNA devices found.")
                return

            print(f"\n✅ Login successful! Found {len(devs)} device(s):")
            for d in devs:
                name = d.get("name", "?")
                model = d.get("model", "?")
                did = d.get("miotDID", d.get("deviceID", "?"))
                print(f"  • {name} | {model} | DID={did}")

            # Print the token file for copying to NAS
            if os.path.exists(token_path):
                with open(token_path) as f:
                    token = f.read()
                print("\n" + "=" * 60)
                print("TOKEN FILE CONTENT (send this to Feng):")
                print("=" * 60)
                print(token)
                print("=" * 60)
            else:
                print("\n⚠️  Token file was not created. Check login logs above.")

        except Exception as e:
            print(f"\n❌ Error: {e}")
            print("\nIf login failed with 2FA/device verification:")
            print("  1. Check your phone for SMS verification code")
            print("  2. Complete verification in browser")
            print("  3. Run this script again")
            sys.exit(1)


if __name__ == "__main__":
    asyncio.run(main())
