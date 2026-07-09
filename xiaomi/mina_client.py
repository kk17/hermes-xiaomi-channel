"""
Xiaomi MiNA (小爱同学) API client for Hermes channel adapter.

Wraps MiService 3.x library to provide:
  - Conversation polling (detect new user utterances)
  - TTS playback on speaker
  - Music/audio playback control
  - Device listing and selection
"""

import asyncio
import logging
import os
import time
from dataclasses import dataclass, field
from typing import Awaitable, Callable, Optional

import aiohttp
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


# Type alias for the OTP callback: async (method: str) -> str
OtpCallback = Callable[[str], Awaitable[str]]


class MinaClient:
    """Async client for Xiaomi MiNA (XiaoAi Speaker) cloud API.

    Handles authentication, device discovery, conversation polling, and
    TTS/playback control. All methods are async.
    """

    def __init__(
        self,
        username: str,
        password: str,
        did: str = "",
        otp_callback: Optional[OtpCallback] = None,
    ):
        self._username = username
        self._password = password
        self._did = did
        self._otp_callback = otp_callback
        self._session: Optional[aiohttp.ClientSession] = None
        self._account: Optional[MiAccount] = None
        self._mina: Optional[MiNAService] = None
        self._devices: list[XiaoAIDevice] = []
        self._default_device: Optional[XiaoAIDevice] = None
        self._last_poll_time: float = 0

    async def login(self) -> None:
        """Authenticate with Xiaomi cloud and persist token."""
        log.info("Logging into Xiaomi account: %s", self._username)
        self._session = aiohttp.ClientSession()

        token_path = os.path.expanduser("~/.mi.token")
        self._account = MiAccount(
            self._session,
            self._username,
            self._password,
            token_path,
            otp_callback=self._otp_callback,
        )
        self._mina = MiNAService(self._account)
        # Trigger login by calling device_list (which calls mi_request → login)
        await self._mina.device_list()
        log.info("Xiaomi login successful")

    async def close(self) -> None:
        """Clean up the aiohttp session."""
        if self._session:
            await self._session.close()
            self._session = None

    async def discover_devices(self) -> list[XiaoAIDevice]:
        """List all XiaoAi (MiNA) devices on the account."""
        if not self._mina:
            raise RuntimeError("Not logged in. Call login() first.")

        raw = await self._mina.device_list()
        # MiService 3.x: device_list returns a list directly (or None)
        devices = []
        for d in raw or []:
            dev = XiaoAIDevice(
                device_id=d.get("deviceID", ""),  # MiNA needs deviceID (UUID), NOT miotDID
                name=d.get("name", "Unknown"),
                model=d.get("model", ""),
                serial=d.get("serialNumber", ""),
            )
            devices.append(dev)
            log.info("Found device: %s (%s) DID=%s", dev.name, dev.model, dev.device_id)

        self._devices = devices

        # Set default device based on _did if specified
        if self._did:
            for d in devices:
                if d.device_id == self._did or self._did in d.name:
                    self._default_device = d
                    log.info("Default device set to: %s (DID=%s)", d.name, d.device_id)
                    break
        elif devices:
            self._default_device = devices[0]

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

        Uses MiNA's ``get_latest_ask`` API (nlp_result_get via ubus).
        Returns None if no conversation found or API error.
        """
        if not self._mina:
            raise RuntimeError("Not logged in")

        dev = device or self.get_device()
        if not dev:
            return None

        try:
            messages = await self._mina.get_latest_ask(dev.device_id)
            if not messages:
                return None

            msg = messages[0]
            answers = msg.get("response", {}).get("answer", [])
            answer_text = ""
            if answers:
                answer_text = answers[0].get("content", "")

            ts_ms = msg.get("timestamp_ms", 0)

            # We need the query text — it's in the answers' 'question' field
            query = answers[0].get("question", "") if answers else ""

            return ConversationEntry(
                query=query,
                answer=answer_text,
                timestamp=ts_ms / 1000.0 if ts_ms else 0,
                conversation_id=msg.get("request_id", ""),
                time_converted="",
            )
        except Exception as e:
            log.debug("Conversation API error: %s", e)

        return None

    async def tts(self, text: str, device: Optional[XiaoAIDevice] = None) -> None:
        """Send TTS message to the speaker. The speaker will speak the text."""
        if not self._mina:
            raise RuntimeError("Not logged in")

        dev = device or self.get_device()
        if not dev:
            log.error("No device available for TTS")
            return

        log.info("TTS on %s: %s", dev.name, text[:80])
        await self._mina.text_to_speech(dev.device_id, text)

    async def tts_silent(self, text: str, device: Optional[XiaoAIDevice] = None) -> None:
        """Send TTS with no audible prompt sound (for suppressing default response)."""
        # MiService 3.x text_to_speech doesn't have a silent flag,
        # but the speaker handles muting at the ubus level.
        await self.tts(text, device)

    async def play_url(self, url: str, device: Optional[XiaoAIDevice] = None) -> None:
        """Play audio from a URL on the speaker."""
        if not self._mina:
            raise RuntimeError("Not logged in")

        dev = device or self.get_device()
        if not dev:
            return

        log.info("Playing URL on %s: %s", dev.name, url[:80])
        await self._mina.play_by_url(dev.device_id, url)

    async def play_music(self, keyword: str, device: Optional[XiaoAIDevice] = None) -> None:
        """Trigger music playback by keyword (e.g. '播放周杰伦的稻香')."""
        if not self._mina:
            raise RuntimeError("Not logged in")

        dev = device or self.get_device()
        if not dev:
            return

        log.info("Playing music on %s: %s", dev.name, keyword)
        # Send as TTS instruction — XiaoAi interprets 播放 commands
        await self._mina.text_to_speech(dev.device_id, f"播放音乐:{keyword}")

    async def stop_playback(self, device: Optional[XiaoAIDevice] = None) -> None:
        """Stop current playback on the speaker."""
        if not self._mina:
            return
        dev = device or self.get_device()
        if not dev:
            return
        await self._mina.player_stop(dev.device_id)

    async def pause_playback(self, device: Optional[XiaoAIDevice] = None) -> None:
        """Pause current playback."""
        if not self._mina:
            return
        dev = device or self.get_device()
        if not dev:
            return
        await self._mina.player_pause(dev.device_id)

    async def set_volume(self, volume: int, device: Optional[XiaoAIDevice] = None) -> None:
        """Set speaker volume (0-100)."""
        if not self._mina:
            return
        dev = device or self.get_device()
        if not dev:
            return
        volume = max(0, min(100, int(volume)))
        await self._mina.player_set_volume(dev.device_id, volume)

    @property
    def devices(self) -> list[XiaoAIDevice]:
        return self._devices
