#!/usr/bin/env python3
"""
Xiaomi MiNA authentication bypass / workaround.

Problem: micoapi serviceToken STS exchange fails (401) from NAS IP.
         SMS verification is rate-limited (24h cooldown).

We have: valid passToken, userId, and working xiaomiio serviceToken.

Approaches tried:
  A. passToken-based autologin for micoapi (no password, no SMS)
  B. Cross-use xiaomiio serviceToken on MiNA API
  C. MiIO home_request RPC to speaker (works via xiaomiio token)
  D. Direct MiNA API call with manually constructed cookies
"""

import asyncio
import base64
import hashlib
import json
import logging
import os
import random
import string
import sys
from urllib import parse

import aiohttp

logging.basicConfig(
    level=logging.DEBUG,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("auth")

# ── Credentials ──────────────────────────────────────────────
MI_USER = os.environ.get("MI_USER", "")
MI_PASS = os.environ.get("MI_PASS", "")

USER_ID = "158764675"
PASS_TOKEN = (
    "V1:DXmurwq2/R1BHTELu6obCeUBar0R+Q4PP+z0lbfsRdtfIeaRczwev/jwmQXNrSz46"
    "M1ikj2x78tZnHJW1z5wMQ/sJD5SGMznO8FPnROFzvTHsEeFhAn19pAVGPblhNJMPvyf+"
    "SgyPZ8abD9QZoeD6uqZXXTY0ntNoF4IRbenFWoamDZ/P1idYExnQvH1MpTZYo/fwqFCu"
    "il25elDYqgJ997iHmZsGF51cSj58EhMRfjL+dPZ4b6WJwpsteEb1NUMWauyANsWE6XIE"
    "u3YE5IVdLojOO6xxNoLF5Yrrbw8LF2QpFsQPoZyMhYmpknp97uGsZyvbfcf0QmrmCoye"
    "7sTrg=="
)

DEVICE_ID = "AA:BB:CC:DD:EE:FF"  # will be overridden

def get_random(length=16):
    return "".join(random.sample(string.ascii_letters + string.digits, length))


def gen_device_id():
    """Generate a device ID like the Xiaomi SDK does."""
    return get_random(16).upper()


async def approach_a_passtoken_autologin(session: aiohttp.ClientSession):
    """
    Approach A: Try passToken-based autologin for micoapi.
    
    The serviceLogin endpoint should accept passToken cookie for autologin
    without needing password or SMS verification.
    """
    log.info("=" * 60)
    log.info("APPROACH A: passToken autologin for micoapi")
    log.info("=" * 60)
    
    device_id = gen_device_id()
    cookies = {
        "userId": USER_ID,
        "passToken": PASS_TOKEN,
        "sdkVersion": "accountsdk-18.8.15",
        "deviceId": device_id,
    }
    headers = {
        "User-Agent": "Android-7.1.1-1.0.0-AndroidOne-Android",
    }
    
    # Step 1: serviceLogin with passToken
    sid = "micoapi"
    url = f"https://account.xiaomi.com/pass/serviceLogin?sid={sid}&_json=true"
    log.info("Step 1: GET %s", url)
    
    async with session.get(url, cookies=cookies, headers=headers, ssl=False) as r:
        raw = await r.read()
        log.info("Response status: %d", r.status)
        log.debug("Response cookies: %s", dict(r.cookies))
    
    text = raw.decode("utf-8", errors="replace")
    # Strip &&&START&&&
    if text.startswith("&&&START&&&"):
        text = text[11:]
    
    try:
        resp = json.loads(text)
    except json.JSONDecodeError:
        log.error("Failed to parse JSON: %s", text[:500])
        return None
    
    log.info("serviceLogin response code=%s, keys=%s", resp.get("code"), list(resp.keys()))
    log.debug("Full response: %s", json.dumps(resp, indent=2)[:1000])
    
    if resp.get("code") == 0:
        log.info("✅ Autologin succeeded! Attempting STS exchange...")
        location = resp.get("location", "")
        nonce = resp.get("nonce", "")
        ssecurity = resp.get("ssecurity", "")
        
        if location and nonce and ssecurity:
            service_token = await sts_exchange(session, location, nonce, ssecurity)
            if service_token:
                log.info("🎉 Got micoapi serviceToken: %s...", service_token[:30])
                return service_token
        else:
            log.warning("Missing location/nonce/ssecurity in response")
    else:
        log.warning("❌ Autologin failed: code=%s desc=%s", resp.get("code"), resp.get("description", ""))
        # Check if it's a security verification issue
        sec_status = resp.get("securityStatus", 0)
        if sec_status:
            log.warning("securityStatus=%s (SMS verification would be needed)", sec_status)
    
    return None


async def sts_exchange(session, location, nonce, ssecurity):
    """Exchange nonce+ssecurity for serviceToken via STS."""
    nsec = f"nonce={nonce}&{ssecurity}"
    client_sign = base64.b64encode(hashlib.sha1(nsec.encode()).digest()).decode()
    
    url = location + "&clientSign=" + parse.quote(client_sign)
    log.info("STS exchange: GET %s", url[:100])
    
    async with session.get(url, ssl=False) as r:
        log.info("STS status: %d", r.status)
        service_token = r.cookies.get("serviceToken")
        if service_token:
            return service_token.value
        body = await r.text()
        log.error("STS failed, no serviceToken cookie. Body: %s", body[:500])
        return None


async def approach_b_cross_service_token(session, xiaomiio_token):
    """
    Approach B: Try using xiaomiio serviceToken for MiNA API.
    Probably won't work (tokens are per-sid), but worth trying.
    """
    log.info("=" * 60)
    log.info("APPROACH B: Cross-use xiaomiio serviceToken on MiNA API")
    log.info("=" * 60)
    
    cookies = {
        "userId": USER_ID,
        "serviceToken": xiaomiio_token,
    }
    headers = {
        "User-Agent": "MiHome/6.0.103 (com.xiaomi.mihome; build:6.0.103.1; iOS 14.4.0) Alamofire/6.0.103 MICO/iOSApp/appStore/6.0.103",
    }
    
    request_id = "app_ios_" + get_random(30)
    url = f"https://api2.mina.mi.com/admin/v2/device_list?master=0&requestId={request_id}"
    log.info("GET %s", url)
    
    async with session.get(url, cookies=cookies, headers=headers, ssl=False) as r:
        status = r.status
        body = await r.text()
    
    log.info("Response: status=%d body=%s", status, body[:500])
    if status == 200:
        try:
            data = json.loads(body)
            if data.get("code") == 0:
                log.info("🎉 Cross-token WORKED! Devices: %s", json.dumps(data.get("data", {}), indent=2)[:500])
                return data
        except:
            pass
    log.warning("❌ Cross-token approach failed")
    return None


async def approach_c_miio_rpc(session, xiaomiio_token, ssecurity):
    """
    Approach C: Use MiIO home_request to send RPC to speaker.
    The MiIO API works via xiaomiio token. We can try to control
    the speaker through miot RPC calls.
    """
    log.info("=" * 60)
    log.info("APPROACH C: MiIO home_request RPC to speaker")
    log.info("=" * 60)
    
    # First, get device list from MiIO to find the speaker DID
    device_id_cookie = gen_device_id()
    
    url = "https://api.io.mi.com/app/home/device_list"
    
    nonce_bytes = gen_device_id().encode()
    nonce = base64.b64encode(nonce_bytes).decode()
    
    # Sign data the way MiIOService.sign_data does
    import hashlib, hmac
    
    def sign_nonce(namespace):
        sha = hashlib.sha256()
        sha.update(namespace.encode())
        sha.update(nonce.encode())
        sha.update(ssecurity.encode())
        return base64.b64encode(sha.digest()).decode()
    
    signed_nonce = sign_nonce("miio-signing-5b3a94d4ad0c08f8")
    
    # Build and sign the request body
    params = {"getVirtualModel": False, "getHuamiDevices": 0}
    body = json.dumps(params, separators=(",", ":"))
    
    signature = hmac.new(
        base64.b64decode(signed_nonce),
        f"&{url.split('/app')[1]}{body}".encode(),
        hashlib.sha256,
    ).digest()
    sig_b64 = base64.b64encode(signature).decode()
    
    post_data = {
        "data": body,
        "signature": sig_b64,
        "rc4_hash__": "",  # not needed for this
        "_nonce": nonce,
        "sid": "xiaomiio",
    }
    
    cookies = {
        "userId": USER_ID,
        "serviceToken": xiaomiio_token,
        "PassportDeviceId": device_id_cookie,
    }
    headers = {
        "User-Agent": "Android-7.1.1-1.0.0-AndroidOne-Android",
        "x-xiaomi-protocal-flag-cli": "PROTOCAL-HTTP2",
    }
    
    log.info("Getting MiIO device list...")
    async with session.post(url, data=post_data, cookies=cookies, headers=headers, ssl=False) as r:
        status = r.status
        body_text = await r.text()
    
    log.info("MiIO device_list: status=%d body=%s", status, body_text[:1000])
    return body_text


async def approach_d_mina_with_miservice():
    """
    Approach D: Use miservice_fork library directly but pre-load the token.
    Inject the passToken into the token dict and see if it can do autologin.
    """
    log.info("=" * 60)
    log.info("APPROACH D: miservice_fork with pre-loaded passToken")
    log.info("=" * 60)
    
    # Add miservice to path
    sys.path.insert(0, "/home/kk17/xiaomi-venv/lib/python3.14/site-packages")
    from miservice import MiAccount, MiNAService, MiIOService
    
    async with aiohttp.ClientSession() as session:
        account = MiAccount(
            session,
            MI_USER,
            MI_PASS,
            None,  # no token store
        )
        # Pre-load our known token data
        account.token = {
            "deviceId": gen_device_id(),
            "userId": USER_ID,
            "passToken": PASS_TOKEN,
        }
        
        mina = MiNAService(account)
        
        try:
            log.info("Trying MiNA device_list...")
            result = await mina.device_list()
            log.info("🎉 MiNA device_list SUCCESS: %s", json.dumps(result, indent=2)[:500])
            return result
        except Exception as e:
            log.warning("❌ MiNA device_list failed: %s", e)
        
        # Try MiIO as a sanity check
        try:
            miio = MiIOService(account)
            log.info("Trying MiIO device_list (sanity check)...")
            result = await miio.device_list()
            log.info("✅ MiIO device_list works: %d devices", len(result.get("list", [])))
        except Exception as e:
            log.warning("MiIO device_list also failed: %s", e)
    
    return None


async def approach_e_login_with_passtoken_manual(session):
    """
    Approach E: Manual full login flow using passToken.
    serviceLogin → if code!=0 but we have passToken, try serviceLoginAuth2
    with passToken only (no password hash).
    """
    log.info("=" * 60)
    log.info("APPROACH E: Manual login flow with passToken cookies")
    log.info("=" * 60)
    
    device_id = gen_device_id()
    base_cookies = {
        "userId": USER_ID,
        "passToken": PASS_TOKEN,
        "sdkVersion": "accountsdk-18.8.15",
        "deviceId": device_id,
    }
    
    for sid in ["micoapi", "xiaomiio"]:
        log.info("\n--- Trying sid=%s ---", sid)
        cookies = dict(base_cookies)
        
        # Step 1: serviceLogin
        url = f"https://account.xiaomi.com/pass/serviceLogin?sid={sid}&_json=true"
        async with session.get(url, cookies=cookies, headers={"User-Agent": "Android-7.1.1-1.0.0-AndroidOne-Android"}, ssl=False) as r:
            raw = await r.read()
            # Merge response cookies
            for k, v in r.cookies.items():
                cookies[k] = v.value
        
        text = raw.decode("utf-8", errors="replace")
        if text.startswith("&&&START&&&"):
            text = text[11:]
        
        try:
            resp1 = json.loads(text)
        except:
            log.error("Parse error for %s: %s", sid, text[:200])
            continue
        
        log.info("serviceLogin: code=%s desc=%s secStatus=%s",
                 resp1.get("code"), resp1.get("description", ""),
                 resp1.get("securityStatus", "N/A"))
        
        if resp1.get("code") == 0:
            log.info("  ✅ Autologin OK for %s!", sid)
            location = resp1.get("location", "")
            nonce = resp1.get("nonce", "")
            ssecurity = resp1.get("ssecurity", "")
            if location and nonce and ssecurity:
                st = await sts_exchange(session, location, nonce, ssecurity)
                if st:
                    log.info("  🎉 Got %s serviceToken!", sid)
                    if sid == "micoapi":
                        return await test_mina_api(session, st)
            continue
        
        # Step 2: Try serviceLoginAuth2 with passToken (no password)
        if resp1.get("code") != 0:
            log.info("  Trying serviceLoginAuth2 with passToken only...")
            auth_url = "https://account.xiaomi.com/pass/serviceLoginAuth2"
            data = {
                "_json": "true",
                "qs": resp1.get("qs", ""),
                "sid": resp1.get("sid", sid),
                "_sign": resp1.get("_sign", ""),
                "callback": resp1.get("callback", ""),
                "user": MI_USER,
                "hash": hashlib.md5(MI_PASS.encode()).hexdigest().upper() if MI_PASS else "",
            }
            log.debug("  Auth data: %s", {k: v[:50] if isinstance(v, str) else v for k, v in data.items()})
            
            async with session.post(auth_url, data=data, cookies=cookies,
                                    headers={"User-Agent": "Android-7.1.1-1.0.0-AndroidOne-Android"},
                                    ssl=False) as r:
                raw2 = await r.read()
            
            text2 = raw2.decode("utf-8", errors="replace")
            if text2.startswith("&&&START&&&"):
                text2 = text2[11:]
            
            try:
                resp2 = json.loads(text2)
            except:
                log.error("  Parse error: %s", text2[:200])
                continue
            
            log.info("  serviceLoginAuth2: code=%s desc=%s secStatus=%s keys=%s",
                     resp2.get("code"), resp2.get("description", ""),
                     resp2.get("securityStatus", "N/A"),
                     list(resp2.keys()))
            
            if resp2.get("code") == 0 or "location" in resp2:
                location = resp2.get("location", "")
                nonce = resp2.get("nonce", "")
                ssecurity = resp2.get("ssecurity", "")
                if location and nonce and ssecurity:
                    st = await sts_exchange(session, location, nonce, ssecurity)
                    if st:
                        log.info("  🎉 Got %s serviceToken via Auth2!", sid)
                        if sid == "micoapi":
                            return await test_mina_api(session, st)
            else:
                log.warning("  ❌ Auth2 failed: %s", resp2.get("description", ""))
                if resp2.get("notificationUrl"):
                    log.warning("  Device verification required: %s", resp2["notificationUrl"][:100])
    
    return None


async def test_mina_api(session, service_token):
    """Test MiNA device_list API with a given serviceToken."""
    log.info("Testing MiNA API with serviceToken: %s...", service_token[:30])
    
    cookies = {
        "userId": USER_ID,
        "serviceToken": service_token,
    }
    headers = {
        "User-Agent": "MiHome/6.0.103 (com.xiaomi.mihome; build:6.0.103.1; iOS 14.4.0) Alamofire/6.0.103 MICO/iOSApp/appStore/6.0.103",
    }
    
    request_id = "app_ios_" + get_random(30)
    url = f"https://api2.mina.mi.com/admin/v2/device_list?master=0&requestId={request_id}"
    
    async with session.get(url, cookies=cookies, headers=headers, ssl=False) as r:
        status = r.status
        body = await r.text()
    
    log.info("MiNA device_list: status=%d", status)
    if status == 200:
        try:
            data = json.loads(body)
            log.info("MiNA response: code=%s", data.get("code"))
            if data.get("code") == 0:
                devices = data.get("data", {}).get("data", [])
                for d in (devices if isinstance(devices, list) else []):
                    log.info("  📢 Device: %s | model=%s | did=%s",
                             d.get("name"), d.get("model"), d.get("miotDID", d.get("deviceID")))
                log.info("🎉 SUCCESS: MiNA device_list works!")
                return data
            else:
                log.warning("MiNA error: %s", json.dumps(data, indent=2)[:500])
        except:
            pass
    else:
        log.warning("MiNA HTTP error: %s", body[:500])
    return None


async def main():
    log.info("Xiaomi MiNA Auth Fix - Testing approaches")
    log.info("User: %s, UserId: %s", MI_USER or "(none)", USER_ID)
    
    timeout = aiohttp.ClientTimeout(total=30)
    async with aiohttp.ClientSession(timeout=timeout) as session:
        
        # Approach A: passToken autologin
        token_a = await approach_a_passtoken_autologin(session)
        if token_a:
            result = await test_mina_api(session, token_a)
            if result:
                log.info("\n✅ SOLUTION: Approach A (passToken autologin) works!")
                return
        
        # Approach E: Full manual login flow (most comprehensive)
        result_e = await approach_e_login_with_passtoken_manual(session)
        if result_e:
            log.info("\n✅ SOLUTION: Approach E (manual login) works!")
            return
        
        # Approach D: miservice_fork library
        result_d = await approach_d_mina_with_miservice()
        if result_d:
            log.info("\n✅ SOLUTION: Approach D (miservice_fork) works!")
            return
        
        # Approach B: Cross-use xiaomiio token
        # First we need the xiaomiio token - try to get it via login
        log.warning("\nAll micoapi approaches failed. Attempting fallback...")


if __name__ == "__main__":
    asyncio.run(main())
