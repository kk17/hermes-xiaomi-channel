"""
Conversation interceptor — polls XiaoAi conversation records and detects
trigger keywords to route messages to Hermes instead of XiaoAi's default handler.
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

    Usage:
        poller = ConversationPoller(client, trigger="阿峰", on_message=callback)
        await poller.start()  # runs forever
    """

    def __init__(
        self,
        client: MinaClient,
        trigger: str = "阿峰",
        poll_interval: float = 0.5,
        mute_default: bool = True,
        on_message: Optional[Callable] = None,
    ):
        self._client = client
        self._trigger = trigger
        self._poll_interval = poll_interval
        self._mute_default = mute_default
        self._on_message = on_message
        self._running = False
        self._last_conversation_time: float = 0
        self._last_raw_query: str = ""

    async def start(self) -> None:
        """Start polling loop. Runs until stop() is called."""
        self._running = True
        log.info("Starting conversation poller (trigger='%s', interval=%.1fs)",
                 self._trigger, self._poll_interval)

        # Initialize last conversation time to avoid replaying old messages
        last_entry = await self._client.get_latest_conversation()
        if last_entry:
            self._last_conversation_time = last_entry.timestamp
            log.info("Initialized poller baseline: last conversation at %s",
                     last_entry.time_converted)

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
        """Single poll iteration: check for new conversation, detect trigger."""
        entry = await self._client.get_latest_conversation()
        if not entry:
            return

        # Skip if we've already seen this conversation
        if entry.timestamp <= self._last_conversation_time:
            return
        if entry.query == self._last_raw_query:
            return

        # New conversation detected
        self._last_conversation_time = entry.timestamp
        self._last_raw_query = entry.query
        log.info("New conversation: '%s' → '%s'",
                 entry.query[:50], entry.answer[:50])

        # Check if trigger keyword is present (fuzzy: match homophones)
        if not self._matches_trigger(entry.query):
            log.debug("No trigger '%s' in '%s' — ignoring", self._trigger, entry.query[:50])
            return

        log.info("Trigger '%s' detected in: %s", self._trigger, entry.query)

        # Extract the actual command by removing the trigger word
        command = self._extract_command(entry.query)

        # Mute XiaoAi's default response if configured
        if self._mute_default:
            try:
                await self._client.stop_playback()
                log.debug("Stopped XiaoAi default response playback")
            except Exception as e:
                log.warning("Failed to mute default response: %s", e)

        # Create intercepted message
        device = self._client.get_device()
        msg = InterceptedMessage(
            text=command,
            raw_text=entry.query,
            trigger=self._trigger,
            device=device,
            timestamp=time.time(),
            conversation_id=entry.conversation_id,
        )

        # Fire callback
        if self._on_message:
            try:
                result = self._on_message(msg)
                if asyncio.iscoroutine(result):
                    await result
            except Exception as e:
                log.error("on_message callback error: %s", e)

    def _matches_trigger(self, query: str) -> bool:
        """Check if trigger keyword is present, allowing for homophone variants.

        XiaoAi's speech recognition often returns homophones for the trigger
        word (e.g. 阿峰/阿枫/阿疯 instead of 阿风). We match by:
        1. Exact match (fast path)
        2. Pinyin match — convert both to pinyin and compare
        """
        q = query.lower()
        t = self._trigger.lower()
        # Fast path: exact match
        if t in q:
            return True
        # Pinyin fuzzy match
        try:
            from xpinyin import Pinyin
            p = Pinyin()
            q_pinyin = p.get_pinyin(q, splitter="").lower()
            t_pinyin = p.get_pinyin(t, splitter="").lower()
            return t_pinyin in q_pinyin
        except ImportError:
            # No xpinyin — try simple homophone map for common 阿风 variants
            homophones = {"风": "风峰枫疯烽锋丰封", "峰": "风峰枫疯烽锋丰封",
                          "枫": "风峰枫疯烽锋丰封", "疯": "风峰枫疯烽锋丰封"}
            import re as _re
            for orig_char, variants in homophones.items():
                if orig_char in t:
                    for v in variants:
                        if t.replace(orig_char, v) in q:
                            return True
        return False

    def _extract_command(self, raw: str) -> str:
        """Extract the actual command from the raw utterance.

        Removes trigger keyword and common filler words.
        Tolerates homophone variants of the trigger word.
        """
        text = raw

        # Remove "小爱同学" prefix (the hardware wake word)
        text = re.sub(r'^小爱同学[,，\s]*', '', text)

        # Build a regex pattern that matches the trigger OR its homophone variants
        trigger_chars = list(self._trigger)
        # For each char, build [风峰枫疯烽锋丰封] pattern if it's a homophone char
        homophone_map = {"风": "风峰枫疯烽锋丰封", "峰": "风峰枫疯烽锋丰封",
                         "枫": "风峰枫疯烽锋丰封", "疯": "风峰枫疯烽锋丰封"}
        trigger_pattern = ""
        for ch in trigger_chars:
            if ch in homophone_map:
                trigger_pattern += f"[{homophone_map[ch]}]"
            else:
                trigger_pattern += re.escape(ch)

        patterns = [
            rf'^问?{trigger_pattern}[,，\s]*',
            rf'^.*?{trigger_pattern}[,，\s]*',
        ]
        for pattern in patterns:
            new_text = re.sub(pattern, '', text, count=1)
            if new_text != text:
                text = new_text
                break

        # Also remove any remaining trigger variants from the text
        text = re.sub(trigger_pattern, '', text)

        # Clean up extra punctuation/whitespace
        text = re.sub(r'^[,，\s]+', '', text).strip()

        return text if text else raw
