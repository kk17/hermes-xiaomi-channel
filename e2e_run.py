#!/usr/bin/env python3
"""E2E test that logs to file."""
import asyncio
import os
import sys
import logging
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
LOG_FILE = os.path.expanduser("~/xiaomi_e2e.log")

# Set up file logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
    handlers=[logging.FileHandler(LOG_FILE, mode="a"), logging.StreamHandler(sys.stdout)],
)
log = logging.getLogger("e2e")

from xiaomi import MinaClient
from xiaomi.conversation import ConversationPoller, InterceptedMessage

client = None


async def on_msg(msg: InterceptedMessage):
    log.info("=" * 50)
    log.info("🎯 TRIGGER DETECTED!")
    log.info("  Raw:     %s", msg.raw_text)
    log.info("  Command: %s", msg.text)
    log.info("  Device:  %s", msg.device.name if msg.device else "?")
    response = f"收到。你说的是{msg.text}。我是阿风。"
    log.info("  TTS:     %s", response)
    await client.tts(response, msg.device)
    log.info("  TTS sent!")
    log.info("=" * 50)


async def main():
    global client
    user = os.environ["MI_USER"]
    pw = os.environ["MI_PASS"]

    client = MinaClient(username=user, password=pw)
    log.info("Logging in...")
    await client.login()
    await client.discover_devices()
    dev = client.get_device("客厅")
    client._default_device = dev
    log.info("Listening on %s (deviceID=%s) for 5 min", dev.name, dev.device_id)
    log.info("Say: 小爱同学，阿风+你的问题")

    poller = ConversationPoller(
        client=client,
        trigger="阿风",
        poll_interval=2.0,
        mute_default=False,
        on_message=on_msg,
    )
    task = asyncio.create_task(poller.start())
    await asyncio.sleep(300)
    poller.stop()
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass
    await client.close()
    log.info("Done")


if __name__ == "__main__":
    asyncio.run(main())
