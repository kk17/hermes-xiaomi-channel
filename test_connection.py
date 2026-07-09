#!/usr/bin/env python3
"""
Standalone test script for the Xiaomi Speaker channel.

Tests the basic pipeline without requiring Hermes gateway:
  1. Login to Xiaomi cloud
  2. Discover devices
  3. Send a TTS message
  4. Start conversation polling (prints detected utterances)

Usage:
  source ~/.hermes/profiles/<profile>/.env  # or export MI_USER/MI_PASS
  python3 test_connection.py [--tts "你好世界"] [--poll 30]
"""

import argparse
import asyncio
import json
import logging
import os
import sys
import time

# Add parent dir to path
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from xiaomi import MinaClient, ConversationPoller, InterceptedMessage

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("test")


async def test_login(client: MinaClient) -> bool:
    """Test Xiaomi cloud login."""
    log.info("─" * 50)
    log.info("1. Testing Xiaomi login...")
    try:
        await client.login()
        log.info("✓ Login successful")
        return True
    except Exception as e:
        log.error("✗ Login failed: %s", e)
        return False


async def test_discover(client: MinaClient) -> list:
    """Test device discovery."""
    log.info("─" * 50)
    log.info("2. Discovering devices...")
    try:
        devices = await client.discover_devices()
        if not devices:
            log.warning("✗ No devices found")
            return []
        log.info("✓ Found %d device(s):", len(devices))
        for d in devices:
            log.info("  • %s | model=%s | DID=%s", d.name, d.model, d.device_id)
        return devices
    except Exception as e:
        log.error("✗ Discovery failed: %s", e)
        return []


async def test_tts(client: MinaClient, text: str) -> bool:
    """Test TTS playback."""
    log.info("─" * 50)
    log.info("3. Testing TTS: '%s'", text)
    try:
        await client.tts(text)
        log.info("✓ TTS sent — listen to your speaker!")
        return True
    except Exception as e:
        log.error("✗ TTS failed: %s", e)
        return False


async def test_conversation(client: MinaClient) -> None:
    """Test conversation polling."""
    log.info("─" * 50)
    log.info("4. Getting latest conversation...")
    try:
        entry = await client.get_latest_conversation()
        if entry:
            log.info("✓ Latest conversation:")
            log.info("  Query:   %s", entry.query[:80])
            log.info("  Answer:  %s", entry.answer[:80])
            log.info("  Time:    %s", entry.time_converted)
        else:
            log.info("  No conversation history found")
    except Exception as e:
        log.warning("Conversation API error: %s", e)


async def test_poll(client: MinaClient, trigger: str, duration: int) -> None:
    """Test conversation polling for a duration."""
    log.info("─" * 50)
    log.info("5. Polling for %ds (trigger='%s')...", duration, trigger)

    detected = []

    async def on_msg(msg: InterceptedMessage):
        log.info("🎯 TRIGGER DETECTED!")
        log.info("  Raw:     %s", msg.raw_text)
        log.info("  Command: %s", msg.text)
        log.info("  Device:  %s", msg.device.name if msg.device else "?")
        detected.append(msg)

    poller = ConversationPoller(
        client=client,
        trigger=trigger,
        poll_interval=0.5,
        mute_default=False,  # Don't mute during test
        on_message=on_msg,
    )

    task = asyncio.create_task(poller.start())
    log.info("  Say something to your speaker (include '%s')...", trigger)

    await asyncio.sleep(duration)
    poller.stop()
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass

    log.info("  Detected %d trigger(s) in %ds", len(detected), duration)


async def main():
    parser = argparse.ArgumentParser(description="Test Xiaomi Speaker connection")
    parser.add_argument("--tts", type=str, default="", help="TTS text to speak")
    parser.add_argument("--poll", type=int, default=0, help="Poll duration in seconds")
    parser.add_argument("--trigger", type=str, default="阿峰", help="Trigger keyword")
    parser.add_argument("--verbose", "-v", action="store_true", help="Debug logging")
    args = parser.parse_args()

    if args.verbose:
        logging.getLogger().setLevel(logging.DEBUG)

    # Load credentials from env
    user = os.environ.get("MI_USER", "")
    pw = os.environ.get("MI_PASS", "")
    did = os.environ.get("MI_DID", "")

    if not user or not pw:
        log.error("MI_USER and MI_PASS environment variables required")
        sys.exit(1)

    log.info("Xiaomi Speaker Test")
    log.info("User: %s, DID: %s", user, did or "(auto)")

    client = MinaClient(username=user, password=pw, did=did)

    # Run tests
    if not await test_login(client):
        sys.exit(1)

    await test_discover(client)

    if args.tts:
        await test_tts(client, args.tts)

    await test_conversation(client)

    if args.poll > 0:
        await test_poll(client, args.trigger, args.poll)

    await client.close()

    log.info("─" * 50)
    log.info("Done!")


if __name__ == "__main__":
    asyncio.run(main())
