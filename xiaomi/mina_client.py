"""
Xiaomi MiNA (小爱同学) API client for Hermes channel adapter.

Wraps MiService library to provide:
  - Conversation polling (detect new user utterances)
  - TTS playback on speaker
  - Music/audio playback control
  - Device listing and selection
"""

import asyncio
import logging
import time
from dataclasses import dataclass, field
from typing import Optional

from miservice import MiAccount, MiNAService

log = logging.getLogger("xiaomi.mina")

@dataclass
class XiaoAIDevice:
    """Represents a Xiaomi AI Speaker device."""
    device_id: str       # miotDID / DID
    name: str            # display name
    model: str           # hardware model (e.g. xiaomi.wifispeaker.l15a)
    serial: str          # serial number


@dataclass
class ConversationEntry:
    """A conversation entry from XiaoAi."""
    query: str           # what the user said
    answer: str          # XiaoAi's original answer
    timestamp: float     # when it happened
    conversation_id: str # conversation track ID
    time_converted: str  # human-readable timestamp from API


class MinaClient:
    """Async client for Xiaomi MiNA (XiaoAi Speaker) cloud API.

    Handles authentication, device discovery, conversation polling, and
    TTS/playback control. All methods are async.
    """

    def __init__(self, username: str, password: str, did: str = ""):
        self._username = username
        self._password = password
        self._did = did
        self._account: Optional[MiAccount] = None
        self._mina: Optional[MiNAService] = None
        self._devices: list[XiaoAIDevice] = []
        self._default_device: Optional[XiaoAIDevice] = None
        self._last_poll_time: float = 0

    async def login(self) -> None:
        """Authenticate with Xiaomi cloud and persist token."""
        log.info("Logging into Xiaomi account: %s", self._username)
        self._account = MiAccount(
            self._username,
            self._password,
            str(_token_path()),
        )
        self._mina = MiNAService(self._account)
        # Trigger login
        await self._mina.device_list()
        log.info("Xiaomi login successful")

    async def discover_devices(self) -> list[XiaoAIDevice]:
        """List all XiaoAi (MiNA) devices on the account."""
        if not self._mina:
            raise RuntimeError("Not logged in. Call login() first.")

        raw = await self._mina.device_list()
        devices = []
        for d in raw.get("data", []):
            dev = XiaoAIDevice(
                device_id=d.get("miotDID", d.get("deviceID", "")),
                name=d.get("name", "Unknown"),
                model=d.get("model", ""),
                serial=d.get("serialNumber", d.get("deviceID", "")),
            )
            devices.append(dev)
            log.info("Found device: %s (%s) DID=%s", dev.name, dev.model, dev.device_id)

        self._devices = devices
        return devices

    def get_device(self, name_or_did: str = "") -> Optional[XiaoAIDevice]:
        """Find device by DID or name (fuzzy). Falls back to first device."""
        if not name_or_did and self._default_device:
            return self._default_device

        for d in self._devices:
            if d.device_id == name_or_did:
                return d
            if name_or_did and name_or_did in d.name:
                return d

        # Fallback: first device
        if self._devices:
            return self._devices[0]
        return None

    async def get_latest_conversation(self, device: Optional[XiaoAIDevice] = None) -> Optional[ConversationEntry]:
        """Get the latest conversation entry from the speaker.

        Uses MiNA's `ubus` conversation tracking API. Returns None if no
        conversation found or API error.
        """
        if not self._mina:
            raise RuntimeError("Not logged in")

        dev = device or self.get_device()
        if not dev:
            return None

        try:
            # Get conversation records via the MiNA ask API
            conversations = await self._mina._send(
                "/device/conversation",
                {
                    "hardware": dev.model,
                    "device_id": dev.device_id,
                    "limit": 1,
                },
            )
            if conversations and conversations.get("data"):
                items = conversations["data"]
                if isinstance(items, list) and len(items) > 0:
                    item = items[0]
                    return ConversationEntry(
                        query=item.get("request", item.get("query", "")),
                        answer=item.get("response", item.get("answer", "")),
                        timestamp=float(item.get("time", 0)),
                        conversation_id=item.get("conversationId", ""),
                        time_converted=item.get("time_converted", ""),
                    )
        except Exception as e:
            log.debug("Conversation API error: %s", e)

        return None

    async def get_ai_response(self, device: Optional[XiaoAIDevice] = None) -> str:
        """Get the latest AI response text from the speaker.
        This uses the 'ask' endpoint which returns XiaoAi's last answer.
        """
        if not self._mina:
            raise RuntimeError("Not logged in")

        dev = device or self.get_device()
        if not dev:
            return ""

        try:
            resp = await self._mina.ubus_request(
                dev.device_id,
                "mibrain",
                "get_audioplayer_state",
            )
            return str(resp)
        except Exception:
            pass

        # Fallback: use ask endpoint
        try:
            raw = await self._mina._send(
                "/device/ai/status",
                {"hardware": dev.model, "device_id": dev.device_id},
            )
            if raw and raw.get("data"):
                return raw["data"].get("answer", "")
        except Exception as e:
            log.debug("AI status API error: %s", e)

        return ""

    async def tts(self, text: str, device: Optional[XiaoAIDevice] = None) -> None:
        """Send TTS message to the speaker. The speaker will speak the text."""
        if not self._mina:
            raise RuntimeError("Not logged in")

        dev = device or self.get_device()
        if not dev:
            log.error("No device available for TTS")
            return

        log.info("TTS on %s: %s", dev.name, text[:80])
        await self._mina.send_message(
            dev.device_id,
            model=dev.model,
            text=text,
            silent=False,
        )

    async def tts_silent(self, text: str, device: Optional[XiaoAIDevice] = None) -> None:
        """Send TTS with no audible prompt sound (for suppressing default response)."""
        if not self._mina:
            raise RuntimeError("Not logged in")

        dev = device or self.get_device()
        if not dev:
            return

        await self._mina.send_message(
            dev.device_id,
            model=dev.model,
            text=text,
            silent=True,
        )

    async def play_url(self, url: str, device: Optional[XiaoAIDevice] = None) -> None:
        """Play audio from a URL on the speaker."""
        if not self._mina:
            raise RuntimeError("Not logged in")

        dev = device or self.get_device()
        if not dev:
            return

        log.info("Playing URL on %s: %s", dev.name, url[:80])
        await self._mina.play(self._mina, url=url, device_id=dev.device_id)

    async def play_music(self, keyword: str, device: Optional[XiaoAIDevice] = None) -> None:
        """Trigger music playback by keyword (e.g. '播放周杰伦的稻香')."""
        if not self._mina:
            raise RuntimeError("Not logged in")

        dev = device or self.get_device()
        if not dev:
            return

        log.info("Playing music on %s: %s", dev.name, keyword)
        # MiNA has a play_music endpoint that uses XiaoAi's built-in music search
        await self._mina.send_message(
            dev.device_id,
            model=dev.model,
            text=f"播放音乐:{keyword}",
            silent=True,
        )

    async def stop_playback(self, device: Optional[XiaoAIDevice] = None) -> None:
        """Stop current playback on the speaker."""
        dev = device or self.get_device()
        if not dev:
            return
        await self._mina.stop(self._mina, device_id=dev.device_id)

    async def pause_playback(self, device: Optional[XiaoAIDevice] = None) -> None:
        """Pause current playback."""
        dev = device or self.get_device()
        if not dev:
            return
        await self._mina.pause(self._mina, device_id=dev.device_id)

    async def set_volume(self, volume: int, device: Optional[XiaoAIDevice] = None) -> None:
        """Set speaker volume (0-100)."""
        dev = device or self.get_device()
        if not dev:
            return
        volume = max(0, min(100, int(volume)))
        await self._mina.player_set_volume(self._mina, dev.device_id, volume)

    @property
    def devices(self) -> list[XiaoAIDevice]:
        return self._devices


def _token_path():
    """Return path for MiService token file."""
    import os
    return os.path.expanduser("~/.mi.token")
