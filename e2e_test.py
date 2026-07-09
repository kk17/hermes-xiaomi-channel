#!/usr/bin/env python3
"""
Standalone E2E test: poll → detect trigger → TTS response.

Usage:
    set -a && source ~/.hermes/profiles/feng-family/.env && set +a
    MI_DID=7acc0060-a4e5-404f-b1c4-85baca0ee7f2 \
    python3 e2e_test.py --trigger 阿风 --duration 60
"""
import argparse
import asyncio
import logging
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from xiaomi import MinaClient
from xiaomi.conversation import ConversationPoller, InterceptedMessage

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("e2e")


async def main():
    parser = argparse.ArgumentParser(description="E2E Xiaomi Speaker test")
    parser.add_argument("--trigger", type=str, default="阿风", help="Trigger keyword")
    parser.add_argument("--duration", type=int, default=60, help="Test duration in seconds")
    parser.add_argument("--device", type=str, default="客厅", help="Device name to use")
    args = parser.parse_args()

    user = os.environ.get("MI_USER", "")
    pw = os.environ.get("MI_PASS", "")
    if not user or not pw:
        log.error("MI_USER and MI_PASS required")
        sys.exit(1)

    client = MinaClient(username=user, password=pw)
    log.info("Logging in...")
    await client.login()
    await client.discover_devices()

    dev = client.get_device(args.device)
    if not dev:
        log.error(f"Device '{args.device}' not found")
        await client.close()
        sys.exit(1)
    client._default_device = dev
    log.info(f"Using device: {dev.name} (deviceID={dev.device_id})")

    # Initialize baseline
    baseline = await client.get_latest_conversation(dev)
    if baseline:
        log.info(f"Baseline conversation: '{baseline.query[:50]}' at {baseline.time_converted}")

    detected_count = 0

    async def on_message(msg: InterceptedMessage):
        nonlocal detected_count
        detected_count += 1
        log.info("=" * 50)
        log.info("🎯 TRIGGER DETECTED!")
        log.info(f"  Raw:     {msg.raw_text}")
        log.info(f"  Command: {msg.text}")
        log.info("=" * 50)

        # Simulate AI response + TTS
        response = f"收到，你说的是{msg.text}。我是阿风。"
        log.info(f"  Replying via TTS: {response}")
        await client.tts(response, dev)

    poller = ConversationPoller(
        client=client,
        trigger=args.trigger,
        poll_interval=1.0,
        mute_default=True,
        on_message=on_message,
    )

    task = asyncio.create_task(poller.start())
    log.info(f"Listening for '{args.trigger}' on {dev.name} for {args.duration}s...")
    log.info(f"Say: 小爱同学，{args.trigger}[你的问题]")
    log.info("(Example: 小爱同学，阿风今天天气怎么样)")

    await asyncio.sleep(args.duration)
    poller.stop()
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass

    await client.close()
    log.info(f"Done. Detected {detected_count} trigger(s) in {args.duration}s.")


if __name__ == "__main__":
    asyncio.run(main())
