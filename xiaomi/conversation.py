"""
Conversation interceptor — polls XiaoAi conversation records and detects
trigger keywords to route messages to Hermes instead of XiaoAi's default handler.

Supports multi-device polling: all XiaoAi speakers on the account are monitored
simultaneously, and TTS responses are routed back to the device that heard the trigger.
"""

import asyncio
import logging
import re
import time
from dataclasses import dataclass
from typing import Callable, Optional

from .mina_client import MinaClient, XiaoAIDevice, ConversationEntry

log = logging.getLogger("xiaomi.conversation")

@dataclass
class InterceptedMessage:
    """A user message intercepted from XiaoAi and routed to Hermes."""
    text: str                # the actual command (trigger word stripped)
    raw_text: str            # full utterance as spoken
    trigger: str             # the trigger keyword that was matched
    device: XiaoAIDevice     # which speaker it came from
    timestamp: float         # when detected
    conversation_id: str     # XiaoAi conversation ID


class ConversationPoller:
    """Polls XiaoAi conversation records and triggers on keyword detection.

    Supports monitoring multiple speakers simultaneously. Each device maintains
    its own conversation baseline so messages from any speaker are detected.

    Usage:
        poller = ConversationPoller(client, trigger="阿峰", devices=[dev1, dev2], on_message=callback)
        await poller.start()  # runs forever
    """

    def __init__(
        self,
        client: MinaClient,
        trigger: str = "阿峰",
        poll_interval: float = 0.5,
        mute_default: bool = True,
        on_message: Optional[Callable] = None,
        devices: Optional[list[XiaoAIDevice]] = None,
    ):
        self._client = client
        self._trigger = trigger
        self._poll_interval = poll_interval
        self._mute_default = mute_default
        self._on_message = on_message
        self._running = False

        # Multi-device: per-device tracking dicts keyed by device_id
        self._last_conversation_times: dict[str, float] = {}
        self._last_raw_queries: dict[str, str] = {}

        # Devices to poll — defaults to all discovered devices
        self._devices: list[XiaoAIDevice] = devices or []

    def _register_device(self, dev: XiaoAIDevice) -> None:
        """Start tracking a new device."""
        if dev.device_id not in self._last_conversation_times:
            self._last_conversation_times[dev.device_id] = 0
            self._last_raw_queries[dev.device_id] = ""
            if dev not in self._devices:
                self._devices.append(dev)

    async def start(self) -> None:
        """Start polling loop. Runs until stop() is called."""
        self._running = True
        dev_names = ", ".join(d.name for d in self._devices) or "(none)"
        log.info("Starting conversation poller (trigger='%s', interval=%.1fs, devices=[%s])",
                 self._trigger, self._poll_interval, dev_names)

        # Initialize baseline for each device to avoid replaying old messages
        for dev in self._devices:
            last_entry = await self._client.get_latest_conversation(dev)
            if last_entry:
                self._last_conversation_times[dev.device_id] = last_entry.timestamp
                log.info("Initialized baseline for %s: last conversation at %s",
                         dev.name, last_entry.time_converted)
            else:
                self._last_conversation_times[dev.device_id] = 0
            self._last_raw_queries[dev.device_id] = ""

        while self._running:
            try:
                await self._poll_once()
            except Exception as e:
                log.warning("Poll error: %s", e)
            await asyncio.sleep(self._poll_interval)

    def stop(self) -> None:
        """Stop the polling loop."""
        self._running = False
        log.info("Conversation poller stopped")

    async def _poll_once(self) -> None:
        """Single poll iteration: check all devices for new conversations."""
        for dev in self._devices:
            try:
                await self._poll_device(dev)
            except Exception as e:
                log.warning("Poll error for %s: %s", dev.name, e)

    async def _poll_device(self, dev: XiaoAIDevice) -> None:
        """Poll a single device for new conversation entries."""
        entry = await self._client.get_latest_conversation(dev)
        if not entry:
            return

        # Skip if we've already seen this conversation
        if entry.timestamp <= self._last_conversation_times.get(dev.device_id, 0):
            return
        if entry.query == self._last_raw_queries.get(dev.device_id, ""):
            return

        # New conversation detected on this device
        self._last_conversation_times[dev.device_id] = entry.timestamp
        self._last_raw_queries[dev.device_id] = entry.query
        log.info("New conversation on %s: '%s' → '%s'",
                 dev.name, entry.query[:50], entry.answer[:50])

        # Check if trigger keyword is present (fuzzy: match homophones)
        if not self._matches_trigger(entry.query):
            log.debug("No trigger '%s' in '%s' from %s — ignoring",
                      self._trigger, entry.query[:50], dev.name)
            return

        log.info("Trigger '%s' detected on %s: %s",
                 self._trigger, dev.name, entry.query)

        # Extract the actual command by removing the trigger word
        command = self._extract_command(entry.query)

        # Mute XiaoAi's default response if configured
        if self._mute_default:
            try:
                await self._client.stop_playback(dev)
                log.debug("Stopped XiaoAi default response on %s", dev.name)
            except Exception as e:
                log.warning("Failed to mute default response on %s: %s", dev.name, e)

        # Create intercepted message with source device info
        msg = InterceptedMessage(
            text=command,
            raw_text=entry.query,
            trigger=self._trigger,
            device=dev,
            timestamp=time.time(),
            conversation_id=entry.conversation_id,
        )

        if self._on_message:
            await self._on_message(msg)

    # ── Trigger matching ──────────────────────────────────

    # Common homophones for 阿风 that XiaoAi may transcribe differently
    _HOMOPHONES: dict[str, list[str]] = {
        "阿风": ["阿峰", "阿枫", "阿丰", "阿锋", "阿蜂", "阿烽", "阿葑", "阿风"],
        "阿峰": ["阿风", "阿枫", "阿丰", "阿锋", "阿蜂", "阿烽", "阿葑", "阿峰"],
    }

    def _matches_trigger(self, text: str) -> bool:
        """Check if trigger keyword (or homophone) appears in text."""
        if not text or not self._trigger:
            return False
        if self._trigger in text:
            return True
        # Check homophones
        homophones = self._HOMOPHONES.get(self._trigger, [])
        return any(h in text for h in homophones)

    def _extract_command(self, text: str) -> str:
        """Extract the actual command by removing wake word and trigger.

        Examples:
            "小爱同学，阿风帮我播放音乐" → "帮我播放音乐"
            "阿风今天天气怎么样"       → "今天天气怎么样"
        """
        result = text

        # Remove wake word "小爱同学" if present
        result = re.sub(r'^小爱同学[，,。.\s]*', '', result)

        # Remove trigger keyword and adjacent punctuation
        # Build pattern from trigger + homophones
        variants = set([self._trigger] + self._HOMOPHONES.get(self._trigger, []))
        for variant in variants:
            result = re.sub(
                rf'{re.escape(variant)}[，,。.\s]*',
                '', result, count=1
            )

        result = result.strip()
        return result if result else text  # fallback to original if nothing left
