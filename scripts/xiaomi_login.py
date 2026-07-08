#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.10"
# dependencies = [
#     "miservice_fork",
# ]
# ///
"""Xiaomi MiService login with 2FA/device verification support.

Run this on your Mac to login to Xiaomi and save the token file.
If device verification is triggered, it will open the verification
URL in your browser and wait for you to complete it.

Usage:
    uv run xiaomi_login.py
"""

import asyncio
import base64
import hashlib
import json
import os
import sys
import webbrowser
from urllib import parse

import aiohttp


async def login_with_2fa(session, user, pw_hash, sid):
    """Login to Xiaomi, handling device verification if needed.

    Returns (token_dict) with userId, passToken, ssecurity, nonce, serviceToken.
    """
    # Step 1: serviceLogin
    async with session.get(
        f"https://account.xiaomi.com/pass/serviceLogin?sid={sid}&_json=true"
    ) as r:
        text = await r.text()
    resp1 = json.loads(text.replace("&&&START&&&", ""))

    # Step 2: serviceLoginAuth2 with credentials
    data = {
        "_json": "true",
        "qs": resp1.get("qs", ""),
        "sid": sid,
        "_sign": resp1.get("_sign", ""),
        "callback": resp1.get("callback", ""),
        "user": user,
        "hash": pw_hash,
    }

    async with session.post(
        "https://account.xiaomi.com/pass/serviceLoginAuth2", data=data
    ) as r:
        text = await r.text()
    resp2 = json.loads(text.replace("&&&START&&&", ""))

    if "userId" in resp2:
        print(f"  ✅ {sid}: Login successful (no verification needed)")
        return resp2

    # 2FA / device verification triggered
    if resp2.get("notificationUrl"):
        print(f"  ⚠️  {sid}: Device verification required")
        print(f"     Opening verification page in browser...")
        verify_url = resp2["notificationUrl"]
        webbrowser.open(verify_url)

        print(f"     Please complete the SMS verification in your browser.")
        print(f"     Press Enter after verification is complete...")
        input()

        # Retry serviceLoginAuth2
        print(f"     Retrying login...")
        async with session.post(
            "https://account.xiaomi.com/pass/serviceLoginAuth2", data=data
        ) as r:
            text = await r.text()
        resp3 = json.loads(text.replace("&&&START&&&", ""))

        if "userId" in resp3:
            print(f"  ✅ {sid}: Login successful after verification!")
            return resp3
        else:
            print(f"  ❌ {sid}: Still failing after verification")
            print(f"     Response: {json.dumps(resp3, ensure_ascii=False)[:200]}")
            return None

    print(f"  ❌ {sid}: Unknown login response: {resp2.get('description', 'unknown')}")
    return None


async def get_service_token(session, location, nonce, ssecurity):
    """Exchange STS location URL for serviceToken cookie."""
    nsec = "nonce=" + str(nonce) + "&" + ssecurity
    client_sign = base64.b64encode(hashlib.sha1(nsec.encode()).digest()).decode()
    sts_url = location + "&clientSign=" + parse.quote(client_sign)

    async with session.get(sts_url) as r:
        for cookie in r.cookies:
            if cookie.key == "serviceToken":
                return cookie.value
    return None


async def main():
    user = "zk.chan007@gmail.com"
    pw = input("Xiaomi password: ").strip()
    pw_hash = hashlib.md5(pw.encode()).hexdigest().upper()

    token_path = os.path.expanduser("~/.mi.token")
    device_id = "AABBCCDDEEFF0011"
    token = {"deviceId": device_id}

    async with aiohttp.ClientSession() as session:
        # Login to both services
        for sid in ["xiaomiio", "micoapi"]:
            print(f"\n--- Logging into {sid} ---")
            resp = await login_with_2fa(session, user, pw_hash, sid)

            if not resp:
                print(f"\n❌ Failed to login to {sid}")
                sys.exit(1)

            if "passToken" in resp:
                token["userId"] = str(resp["userId"])
                token["passToken"] = resp["passToken"]

            # Get serviceToken via STS
            service_token = await get_service_token(
                session, resp["location"], resp["nonce"], resp["ssecurity"]
            )

            if service_token:
                token[sid] = [resp["ssecurity"], service_token]
                print(f"  ✅ {sid}: serviceToken obtained")
            else:
                print(f"  ❌ {sid}: Failed to get serviceToken from STS")
                sys.exit(1)

        # Save token file
        with open(token_path, "w") as f:
            json.dump(token, f, indent=2)
        print(f"\n✅ Token saved to {token_path}")

        # Test: get device list
        from miservice import MiAccount, MiNAService

        account = MiAccount(session, user, pw, token_path)
        mina = MiNAService(account)

        try:
            devs = await mina.device_list()
            if devs:
                print(f"\n📱 Found {len(devs)} MiNA device(s):")
                for d in devs:
                    name = d.get("name", "?")
                    model = d.get("model", "?")
                    did = d.get("miotDID", d.get("deviceID", "?"))
                    print(f"  • {name} | {model} | DID={did}")
            else:
                print("\nNo MiNA devices found.")
        except Exception as e:
            print(f"\n⚠️  Device list error: {e}")

        # Print token for copying to NAS
        print("\n" + "=" * 60)
        print("TOKEN FILE CONTENT (send this to Feng):")
        print("=" * 60)
        with open(token_path) as f:
            print(f.read())
        print("=" * 60)


if __name__ == "__main__":
    asyncio.run(main())
