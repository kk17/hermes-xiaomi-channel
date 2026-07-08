#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.10"
# dependencies = [
#     "miservice_fork",
# ]
# ///
"""Xiaomi MiService login with device verification support.

Run this on your Mac to login to Xiaomi and save the token file.

The script handles device verification by:
1. Starting the login flow from Python
2. Visiting the verification URL from the SAME Python session
3. Opening the browser for SMS verification
4. After verification, continuing the login from the Python session

Usage:
    uv run xiaomi_login.py
"""

import asyncio
import base64
import hashlib
import json
import os
import re
import sys
import webbrowser
from urllib import parse

import aiohttp


async def login_with_verification(session, user, pw_hash, sid):
    """Login to Xiaomi, handling device verification if needed."""

    # Step 1: serviceLogin
    async with session.get(
        f"https://account.xiaomi.com/pass/serviceLogin?sid={sid}&_json=true"
    ) as r:
        text = await r.text()
    resp1 = json.loads(text.replace("&&&START&&&", ""))

    # Step 2: serviceLoginAuth2
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

    if not resp2.get("notificationUrl"):
        print(f"  ❌ {sid}: Unexpected response: {resp2.get('description')}")
        return None

    # Device verification required
    notify_url = resp2["notificationUrl"]
    print(f"  ⚠️  {sid}: Device verification required")

    # Visit the notification URL from THIS Python session (sets session cookies)
    # This links the verification context to our session
    async with session.get(notify_url, allow_redirects=True) as r:
        verify_page = await r.text()
        final_url = str(r.url)

    print(f"     Verification page loaded (from Python session)")

    # Extract the verification context
    context_match = re.search(r'context=([^&"]+)', notify_url)
    context = context_match.group(1) if context_match else ""

    # Try to find the SMS send/verify API from the page
    # The SPA loads JS dynamically, but the API might be discoverable

    # Open the verification page in browser for Kyle to complete SMS
    print(f"     Opening verification page in browser...")
    print(f"     Please complete SMS verification in the browser.")
    webbrowser.open(notify_url)

    # Wait for Kyle to complete verification
    print(f"\n     After completing SMS verification in the browser,")
    print(f"     ALSO enter the SMS code here for double verification:")
    sms_code = input(f"     SMS code (or press Enter to skip): ").strip()

    if sms_code:
        # Try to submit the SMS code from the Python session
        # This uses the same session cookies set by visiting notify_url
        verify_endpoints = [
            ("POST", "https://account.xiaomi.com/identity/auth/verifyCode"),
            ("POST", "https://account.xiaomi.com/identity/verifyCode"),
            ("POST", "https://account.xiaomi.com/pass/identity/verifyCode"),
        ]

        for method, url in verify_endpoints:
            try:
                payload = {
                    "context": context,
                    "code": sms_code,
                    "_locale": "en_SG",
                }
                headers = {
                    "Content-Type": "application/x-www-form-urlencoded",
                    "Referer": notify_url,
                    "Origin": "https://account.xiaomi.com",
                }
                if method == "POST":
                    async with session.post(url, data=payload, headers=headers) as r:
                        body = await r.text()
                        if r.status != 404:
                            print(f"     {method} {url}: {r.status} - {body[:200]}")
            except Exception as e:
                pass

    print(f"\n     Press Enter to retry login...")
    input()

    # Retry serviceLoginAuth2 with the same session
    # The session now has cookies from visiting the verification page
    print(f"     Retrying login...")

    # Get fresh serviceLogin params
    async with session.get(
        f"https://account.xiaomi.com/pass/serviceLogin?sid={sid}&_json=true"
    ) as r:
        text = await r.text()
    resp3 = json.loads(text.replace("&&&START&&&", ""))

    # Check if serviceLogin now returns userId (already authenticated)
    if "userId" in resp3:
        print(f"  ✅ {sid}: Already authenticated via session cookies!")
        # Need to do serviceLoginAuth2 to get ssecurity
        data2 = {
            "_json": "true",
            "qs": resp3.get("qs", ""),
            "sid": sid,
            "_sign": resp3.get("_sign", ""),
            "callback": resp3.get("callback", ""),
            "user": user,
            "hash": pw_hash,
        }
        async with session.post(
            "https://account.xiaomi.com/pass/serviceLoginAuth2", data=data2
        ) as r:
            text = await r.text()
        resp4 = json.loads(text.replace("&&&START&&&", ""))
        if "userId" in resp4:
            return resp4
        # Even without ssecurity, try using the serviceLogin location
        if resp3.get("location"):
            return resp3

    # Try serviceLoginAuth2 again
    data_retry = {
        "_json": "true",
        "qs": resp3.get("qs", ""),
        "sid": sid,
        "_sign": resp3.get("_sign", ""),
        "callback": resp3.get("callback", ""),
        "user": user,
        "hash": pw_hash,
    }
    async with session.post(
        "https://account.xiaomi.com/pass/serviceLoginAuth2", data=data_retry
    ) as r:
        text = await r.text()
    resp5 = json.loads(text.replace("&&&START&&&", ""))

    if "userId" in resp5:
        print(f"  ✅ {sid}: Login successful after verification!")
        return resp5

    # Last resort: check if serviceLogin GET returns location (authenticated)
    if resp3.get("location"):
        print(f"  ℹ️  {sid}: Using serviceLogin session (no ssecurity)")
        return resp3

    print(f"  ❌ {sid}: Still failing after verification")
    desc = resp5.get("description", "unknown")
    has_notif = bool(resp5.get("notificationUrl"))
    print(f"     desc={desc}, still needs verification={has_notif}")
    return None


async def get_service_token(session, location, nonce=None, ssecurity=None):
    """Exchange STS location URL for serviceToken cookie."""
    if nonce and ssecurity:
        # Full token exchange with clientSign
        nsec = "nonce=" + str(nonce) + "&" + ssecurity
        client_sign = base64.b64encode(hashlib.sha1(nsec.encode()).digest()).decode()
        sts_url = location + "&clientSign=" + parse.quote(client_sign)
    else:
        # Try without clientSign (from serviceLogin GET)
        sts_url = location

    print(f"     STS URL: {sts_url[:80]}...")
    async with session.get(sts_url) as r:
        print(f"     STS response: {r.status}")
        for cookie in r.cookies:
            if cookie.key == "serviceToken":
                return cookie.value
        # Also check session cookie jar
        for cookie in session.cookie_jar:
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
        for sid in ["xiaomiio", "micoapi"]:
            print(f"\n--- Logging into {sid} ---")
            resp = await login_with_verification(session, user, pw_hash, sid)

            if not resp:
                print(f"\n❌ Failed to login to {sid}")
                sys.exit(1)

            if "userId" in resp:
                token["userId"] = str(resp["userId"])
            if "passToken" in resp:
                token["passToken"] = resp["passToken"]

            # Get serviceToken via STS
            nonce = resp.get("nonce")
            ssecurity = resp.get("ssecurity")
            location = resp.get("location")

            if location:
                service_token = await get_service_token(
                    session, location, nonce, ssecurity
                )
                if service_token:
                    token[sid] = [ssecurity or "", service_token]
                    print(f"  ✅ {sid}: serviceToken obtained!")
                else:
                    print(f"  ⚠️  {sid}: STS didn't return serviceToken")
                    if ssecurity:
                        print(f"     (ssecurity available, token incomplete)")
                    else:
                        print(f"     (no ssecurity — may still work for MiNA)")

        # Save token
        with open(token_path, "w") as f:
            json.dump(token, f, indent=2)
        print(f"\n✅ Token saved to {token_path}")

        # Test MiNA
        from miservice import MiAccount, MiNAService

        account = MiAccount(session, user, pw, token_path)
        mina = MiNAService(account)
        try:
            devs = await mina.device_list()
            if devs:
                print(f"\n📱 Found {len(devs)} MiNA device(s):")
                for d in devs:
                    print(f"  • {d.get('name','?')} | {d.get('model','?')} | DID={d.get('miotDID','?')}")
            else:
                print("\nNo MiNA devices found.")
        except Exception as e:
            print(f"\n⚠️  MiNA test error: {e}")

        # Print token
        print("\n" + "=" * 60)
        print("TOKEN FILE CONTENT (send this to Feng):")
        print("=" * 60)
        with open(token_path) as f:
            print(f.read())
        print("=" * 60)


if __name__ == "__main__":
    asyncio.run(main())
