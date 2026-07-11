"""
Hermes Platform Adapter for Xiaomi AI Speaker (小爱同学).

This adapter bridges Hermes Agent with Xiaomi AI Speakers. Users say
"小爱同学" to wake the speaker, then include a trigger keyword (e.g. "阿峰")
to route their command to Hermes instead of XiaoAi's default handler.

Architecture:
    User → XiaoAi Speaker → Xiaomi Cloud API
                                ↓ (conversation poll)
                         This Adapter (detects trigger keyword)
                                ↓
                         Hermes Gateway → AI Agent
                                ↓
                         Agent Response → TTS on Speaker

Installation:
    1. Copy this directory to ~/.hermes/plugins/xiaomi-speaker/
    2. Set env vars: MI_USER, MI_PASS, XIAOMI_TRIGGER
    3. Run: hermes gateway (or restart)
    4. Say: "小爱同学，阿峰帮我播放周杰伦的《稻香》"
"""

import asyncio
import logging
import os
import uuid
from typing import Any, Optional

# Hermes gateway imports (available when running inside the gateway)
from gateway.platforms.base import (
    BasePlatformAdapter,
    SendResult,
    MessageEvent,
    MessageType,
)
from gateway.config import Platform, PlatformConfig

# Local imports
from .xiaomi import MinaClient, ConversationPoller, InterceptedMessage, XiaoAIDevice

log = logging.getLogger("xiaomi.adapter")

# How long to chunk TTS text (XiaoAi has a ~200 char TTS limit per call)
TTS_CHUNK_SIZE = 200
# Simulated chat ID for the speaker (single "chat" per device)
SPEAKER_CHAT_ID = "xiaomi_speaker"


class XiaomiSpeakerAdapter(BasePlatformAdapter):
    """Hermes platform adapter for Xiaomi AI Speaker (小爱同学).

    Implements the polling-based conversation interception pattern:
    1. Polls XiaoAi cloud API for new conversation entries
    2. Detects trigger keyword (e.g. "阿峰")
    3. Forwards the command to Hermes via handle_message()
    4. Delivers Hermes response via TTS on the speaker
    """

    def __init__(self, config: PlatformConfig):
        super().__init__(config, Platform("xiaomi_speaker"))

        extra = config.extra or {}
        self._username = os.getenv("MI_USER", "") or extra.get("mi_user", "")
        self._password = os.getenv("MI_PASS", "") or extra.get("mi_pass", "")
        self._did = os.getenv("MI_DID", "") or extra.get("mi_did", "")
        self._trigger = (
            os.getenv("XIAOMI_TRIGGER", "阿风")
            or extra.get("trigger", "阿风")
        )
        self._poll_interval = float(
            os.getenv("XIAOMI_POLL_INTERVAL", "2.0")
            or extra.get("poll_interval", "2.0")
        )
        self._mute_default = (
            os.getenv("XIAOMI_MUTE_DEFAULT", "true").lower() == "true"
        )
        self._default_device_name = (
            os.getenv("XIAOMI_DEFAULT_DEVICE", "")
            or extra.get("default_device", "")
        )
        # Default model for voice sessions (lighter model = faster response)
        self._default_model = os.getenv("XIAOMI_DEFAULT_MODEL", "")

        # Runtime state
        self._client: Optional[MinaClient] = None
        self._poller: Optional[ConversationPoller] = None
        self._poll_task: Optional[asyncio.Task] = None
        self._device_name: str = "Xiaomi Speaker"
        self._last_active_device: Optional[XiaoAIDevice] = None

    # ── Lifecycle ──────────────────────────────────────────

    async def connect(self, *, is_reconnect: bool = False) -> bool:
        """Connect to Xiaomi cloud and start conversation polling."""
        if not self._username or not self._password:
            log.error("MI_USER and MI_PASS are required")
            return False

        log.info("Connecting Xiaomi Speaker channel (trigger='%s')", self._trigger)

        # Initialize MiNA client with OTP callback that logs and raises
        # (instead of silently retrying and triggering rate limits)
        async def _otp_cb(otp_method: str) -> str:
            log.error("Xiaomi OTP required (%s) — manual login needed. "
                      "Run login script on NAS to refresh token.", otp_method)
            raise RuntimeError(
                f"Xiaomi OTP required ({otp_method}). Token expired — "
                "run login script on NAS to refresh ~/.mi.token"
            )

        self._client = MinaClient(
            username=self._username,
            password=self._password,
            did=self._did,
            otp_callback=_otp_cb,
        )

        try:
            await self._client.login()
        except Exception as e:
            log.error("Xiaomi login failed: %s", e)
            return False

        # Discover devices
        devices = await self._client.discover_devices()
        if not devices:
            log.error("No Xiaomi AI speakers found on this account")
            return False

        # Select default device (for TTS fallback)
        dev = self._client.get_device(self._default_device_name)
        if dev:
            self._device_name = dev.name
            log.info("Default speaker: %s (%s) DID=%s", dev.name, dev.model, dev.device_id)

        # All devices for multi-speaker polling
        all_devices = self._client.devices
        dev_names = ", ".join(d.name for d in all_devices)
        log.info("Monitoring %d speaker(s): [%s]", len(all_devices), dev_names)

        # Start conversation poller with all devices
        self._poller = ConversationPoller(
            client=self._client,
            trigger=self._trigger,
            poll_interval=self._poll_interval,
            mute_default=self._mute_default,
            on_message=self._on_intercepted_message,
            devices=all_devices,
        )
        self._poll_task = asyncio.create_task(self._poller.start())

        self._mark_connected()
        log.info("Xiaomi Speaker channel connected ✓ (device=%s)", self._device_name)
        return True

    async def disconnect(self) -> None:
        """Stop polling and clean up."""
        if self._poller:
            self._poller.stop()
        if self._poll_task:
            self._poll_task.cancel()
            try:
                await self._poll_task
            except asyncio.CancelledError:
                pass
        self._poller = None
        self._poll_task = None
        self._client = None
        self._mark_disconnected()
        log.info("Xiaomi Speaker channel disconnected")

    # ── Inbound: conversation → Hermes ────────────────────

    # Voice model-switch keywords: maps spoken phrase → model name
    _MODEL_SWITCH_KEYWORDS: dict[str, str] = {
        "简单模型": "coding-low",
        "快速模型": "coding-low",
        "高级模型": "coding",
        "智能模型": "coding",
    }

    # Voice stop keywords: when detected, interrupt the running agent
    _STOP_KEYWORDS: set[str] = {"停止", "暂停", "停下", "停下来", "别说了"}

    # Voice status keywords: check if agent is running
    _STATUS_KEYWORDS: set[str] = {"状态", "在吗", "在干嘛", "在不在"}

    # Voice play music keywords: trigger music playback via yt-dlp + play_url
    _PLAY_KEYWORDS: set[str] = {"播放音乐", "放音乐", "播放歌曲", "放首歌", "听音乐", "播放"}
    _PLAY_STOP_KEYWORDS: set[str] = {"停止播放", "别放了", "关掉音乐", "停止音乐"}

    async def _on_intercepted_message(self, msg: InterceptedMessage) -> None:
        """Handle an intercepted voice command — forward to Hermes gateway.

        Sends a quick "阿峰收到" confirmation TTS immediately, then checks
        for voice-activated model switching keywords.
        """
        log.info("Forwarding to Hermes (from %s): '%s'",
                 msg.device.name if msg.device else "?", msg.text[:80])

        # Remember which device sent this message so send() can route TTS back
        self._last_active_device = msg.device

        # Immediate confirmation TTS so user knows the message was received
        if self._client and msg.device:
            try:
                await self._client.tts("阿峰收到", msg.device)
            except Exception as e:
                log.warning("Confirmation TTS failed: %s", e)

        command_text = msg.text.strip()

        # Build source early — needed for stop/interrupt before message dispatch
        dev_name = msg.device.name if msg.device else "Xiaomi Speaker"
        source = self.build_source(
            chat_id=SPEAKER_CHAT_ID,
            chat_name=dev_name,
            chat_type="dm",
            user_id="voice_user",
            user_name="Voice",
        )

        # Check for voice stop command — interrupt running agent immediately
        if any(kw in command_text for kw in self._STOP_KEYWORDS):
            log.info("Voice stop command: '%s'", command_text)
            try:
                runner = getattr(self, "gateway_runner", None)
                if runner:
                    from gateway.session import build_session_key
                    session_key = build_session_key(source)
                    # Stop TTS playback on the speaker
                    if self._client and msg.device:
                        await self._client.stop_playback(msg.device)
                    # Interrupt the running agent
                    if hasattr(runner, "_interrupt_and_clear_session"):
                        from gateway.run import _INTERRUPT_REASON_STOP
                        await runner._interrupt_and_clear_session(
                            session_key,
                            source,
                            interrupt_reason=_INTERRUPT_REASON_STOP,
                            invalidation_reason="voice_stop_command",
                        )
                    # Also use adapter's interrupt for good measure
                    await self.interrupt_session_activity(session_key, SPEAKER_CHAT_ID)
                    log.info("Agent interrupted by voice stop command")
            except Exception as e:
                log.warning("Voice stop failed: %s", e)
            return  # Don't forward to agent — just stop

        # Check for voice status command — report if agent is running
        if any(kw in command_text for kw in self._STATUS_KEYWORDS):
            is_running = False
            try:
                runner = getattr(self, "gateway_runner", None)
                if runner:
                    from gateway.session import build_session_key
                    session_key = build_session_key(source)
                    running = getattr(runner, "_running_agents", {})
                    is_running = session_key in running and running[session_key] is not None
            except Exception as e:
                log.warning("Status check failed: %s", e)

            reply = "阿峰在干活中" if is_running else "阿峰在休息"
            log.info("Voice status: %s (running=%s)", reply, is_running)
            if self._client and msg.device:
                try:
                    await self._client.tts(reply, msg.device)
                except Exception as e:
                    log.warning("Status TTS failed: %s", e)
            return  # Don't forward to agent

        # Check for music stop command
        if any(kw in command_text for kw in self._PLAY_STOP_KEYWORDS):
            log.info("Voice music stop: '%s'", command_text)
            if self._client and msg.device:
                try:
                    await self._client.stop_playback(msg.device)
                    await self._client.tts("好的，音乐已停止", msg.device)
                except Exception as e:
                    log.warning("Music stop failed: %s", e)
            return

        # Check for music play command — search YouTube + play on speaker
        if any(kw in command_text for kw in self._PLAY_KEYWORDS):
            # Extract the search query: remove trigger word and play keywords
            query = command_text
            for kw in self._PLAY_KEYWORDS:
                query = query.replace(kw, "")
            query = query.strip()
            if not query:
                if self._client and msg.device:
                    await self._client.tts("你想听什么歌？", msg.device)
                return

            log.info("Voice music play: query='%s'", query)
            if self._client and msg.device:
                try:
                    # Skip TTS — it blocks the speaker from playing the URL
                    # Search YouTube, proxy locally, play on speaker
                    success = await self._search_and_play_youtube(query, msg.device)
                    if success:
                        log.info("Playing YouTube music on %s", msg.device.name)
                    else:
                        await self._client.tts("抱歉，没找到这首歌", msg.device)
                except Exception as e:
                    log.warning("Music play failed: %s", e)
                    if self._client and msg.device:
                        await self._client.tts("播放失败，请稍后再试", msg.device)
            return  # Don't forward to agent

        # Voice-activated model switching (silent, via session_model_overrides)
        target_model = self._default_model  # start with default
        for phrase, model_name in self._MODEL_SWITCH_KEYWORDS.items():
            if phrase in command_text:
                target_model = model_name
                log.info("Voice model switch: '%s' → %s", command_text, model_name)
                break

        # Apply model override for this voice session (silent, no TTS)
        if target_model:
            try:
                runner = getattr(self, "gateway_runner", None)
                if runner and hasattr(runner, "_session_model_overrides"):
                    from gateway.session import build_session_key
                    session_key = build_session_key(source)
                    current = runner._session_model_overrides.get(session_key, {})
                    if current.get("model") != target_model:
                        runner._session_model_overrides[session_key] = {
                            "model": target_model,
                        }
                        # Evict cached agent so new model takes effect
                        if hasattr(runner, "_evict_cached_agent"):
                            runner._evict_cached_agent(session_key)
                        log.info("Voice session model set: %s → %s (silent)",
                                 session_key, target_model)
            except Exception as e:
                log.warning("Failed to set model override: %s", e)

        event = MessageEvent(
            text=command_text,
            message_type=MessageType.TEXT,
            source=source,
            message_id=str(uuid.uuid4()),
            raw_message={
                "raw_text": msg.raw_text,
                "trigger": msg.trigger,
                "device": dev_name,
                "device_id": msg.device.device_id if msg.device else "",
                "timestamp": msg.timestamp,
            },
        )

        await self.handle_message(event)

    # ── Music playback helpers ────────────────────────────

    # Webapps FastAPI server (Docker container, port 8093)
    _WEBAPPS_URL = "http://192.168.31.222:8093"

    async def _search_and_play_youtube(self, query: str, device) -> bool:
        """Search YouTube via webapps API, download audio, play on speaker.

        Flow:
        1. GET /api/music/search?q=<query> → top results (SG-filtered)
        2. Try each result: POST /api/music/download {"video_id": ...}
           Skip results that fail to download (geo-blocked, unavailable, etc.)
        3. On first successful download, play on speaker immediately
        """
        import aiohttp
        from urllib.parse import quote

        timeout = aiohttp.ClientTimeout(total=60)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            # Step 1: Search
            search_url = f"{self._WEBAPPS_URL}/api/music/search?q={quote(query)}"
            log.info("Music search: %s", query)
            try:
                async with session.get(search_url) as resp:
                    if resp.status != 200:
                        log.warning("Search API returned %d", resp.status)
                        return False
                    data = await resp.json()
            except Exception as e:
                log.warning("Search API failed: %s", e)
                return False

            results = data.get("results", [])
            if not results:
                log.warning("No results for: %s", query)
                return False

            log.info("Found %d results for: %s", len(results), query)

            # Step 2: Try each result until one downloads successfully
            for i, result in enumerate(results):
                video_id = result["video_id"]
                title = result.get("title", "")

                try:
                    async with session.post(
                        f"{self._WEBAPPS_URL}/api/music/download",
                        json={"video_id": video_id},
                    ) as resp:
                        if resp.status != 200:
                            error_detail = await resp.text()
                            log.warning("Download failed for %s (%s): %s",
                                        video_id, title[:40], error_detail[:100])
                            continue  # Try next result
                        dl_data = await resp.json()
                except Exception as e:
                    log.warning("Download API failed for %s: %s", video_id, e)
                    continue  # Try next result

                cached = dl_data.get("cached", False)
                log.info("Downloaded %s: %s (cached=%s, size=%d, attempt %d/%d)",
                         video_id, title[:40], cached, dl_data.get("size", 0),
                         i + 1, len(results))

                # Step 3: Play on speaker
                play_url = f"{self._WEBAPPS_URL}{dl_data['url']}"
                log.info("Playing: %s (%d chars)", play_url[:60], len(play_url))
                result = await self._client.play_url(play_url, device)
                log.info("play_by_url result: %s", result)
                return result

            # All results failed
            log.warning("All %d results failed to download for: %s", len(results), query)
            return False

    # ── Outbound: Hermes → speaker TTS ────────────────────

    async def send(
        self,
        chat_id: str,
        content: str,
        reply_to: Optional[str] = None,
        metadata: Optional[dict] = None,
    ) -> SendResult:
        """Send Hermes response to the speaker via TTS.

        Routes TTS to the device that the user spoke to (tracked via
        _last_active_device). Falls back to the default device.

        For long responses, text is chunked into segments ≤200 chars and
        each chunk is spoken sequentially with a small delay.
        """
        if not self._client:
            return SendResult(success=False, error="Not connected")

        if not content or not content.strip():
            return SendResult(success=True, message_id="empty")

        # Route TTS to the device the user spoke to
        target_device = self._last_active_device or self._client.get_device()
        text = content.strip()
        dev_label = target_device.name if target_device else "default"
        log.info("TTS response (%d chars) → %s: %s", len(text), dev_label, text[:80])

        # Handle special actions from metadata
        if metadata:
            action = metadata.get("action")
            if action == "play_music":
                keyword = metadata.get("keyword", text)
                await self._client.play_music(keyword, target_device)
                return SendResult(success=True, message_id="play_music")
            if action == "play_url":
                url = metadata.get("url", "")
                if url:
                    await self._client.play_url(url, target_device)
                    return SendResult(success=True, message_id="play_url")

        # Chunk and speak via TTS
        chunks = self._chunk_text(text, TTS_CHUNK_SIZE)
        for i, chunk in enumerate(chunks):
            try:
                await self._client.tts(chunk, target_device)
                # Small pause between chunks for natural speech
                if i < len(chunks) - 1:
                    await asyncio.sleep(1.5)
            except Exception as e:
                log.error("TTS error on chunk %d (%s): %s", i, dev_label, e)
                return SendResult(success=False, error=str(e))

        return SendResult(success=True, message_id=str(uuid.uuid4()))

    async def send_typing(self, chat_id: str) -> None:
        """Play a brief 'thinking' sound on the speaker.

        Uses a short TTS prompt like "让我想想" to indicate the agent
        is processing, since there's no visual typing indicator.
        """
        if self._client:
            try:
                target = self._last_active_device or None
                await self._client.tts_silent("让我想想", target)
            except Exception:
                pass

    async def get_chat_info(self, chat_id: str) -> dict[str, Any]:
        """Return metadata about the speaker 'chat'."""
        return {
            "name": self._device_name,
            "type": "voice",
            "platform": "xiaomi_speaker",
        }

    # ── Helpers ───────────────────────────────────────────

    @staticmethod
    def _chunk_text(text: str, max_size: int) -> list[str]:
        """Split text into chunks at sentence boundaries."""
        if len(text) <= max_size:
            return [text]

        chunks = []
        current = ""

        # Split by sentence-ending punctuation first
        import re
        sentences = re.split(r'(?<=[。！？\.\!\?\n])', text)

        for sentence in sentences:
            if not sentence:
                continue
            if len(current) + len(sentence) <= max_size:
                current += sentence
            else:
                if current:
                    chunks.append(current)
                # If single sentence is too long, hard-split
                while len(sentence) > max_size:
                    chunks.append(sentence[:max_size])
                    sentence = sentence[max_size:]
                current = sentence

        if current:
            chunks.append(current)

        return chunks


# ── Plugin Registration ────────────────────────────────────


def check_requirements() -> bool:
    """Check if minimum required env vars are present."""
    return bool(os.getenv("MI_USER") and os.getenv("MI_PASS"))


def validate_config(config) -> bool:
    """Validate adapter configuration."""
    extra = getattr(config, "extra", {}) or {}
    return bool(
        os.getenv("MI_USER") or extra.get("mi_user")
    ) and bool(
        os.getenv("MI_PASS") or extra.get("mi_pass")
    )


def _env_enablement() -> dict | None:
    """Seed PlatformConfig.extra from env vars for auto-configuration."""
    user = os.getenv("MI_USER", "").strip()
    pw = os.getenv("MI_PASS", "").strip()
    if not (user and pw):
        return None

    seed: dict[str, Any] = {
        "mi_user": user,
        "mi_pass": pw,
        "trigger": os.getenv("XIAOMI_TRIGGER", "阿风"),
    }

    did = os.getenv("MI_DID", "").strip()
    if did:
        seed["mi_did"] = did

    home = os.getenv("XIAOMI_DEFAULT_DEVICE", "").strip()
    if home:
        seed["home_channel"] = {"chat_id": SPEAKER_CHAT_ID, "name": home}

    return seed


def register(ctx):
    """Plugin entry point — called by Hermes plugin system on load."""
    ctx.register_platform(
        name="xiaomi_speaker",
        label="Xiaomi AI Speaker (小爱同学)",
        adapter_factory=lambda cfg: XiaomiSpeakerAdapter(cfg),
        check_fn=check_requirements,
        validate_config=validate_config,
        required_env=["MI_USER", "MI_PASS", "XIAOMI_TRIGGER"],
        install_hint="pip install 'miservice>=3.0.0'",
        env_enablement_fn=_env_enablement,
        allow_all_env="XIAOMI_ALLOW_ALL_USERS",
    )
