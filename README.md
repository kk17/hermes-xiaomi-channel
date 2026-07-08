# Hermes Xiaomi Speaker Channel 🎙️

> Voice-activated Hermes Agent through Xiaomi AI Speaker (小爱同学)

Talk to your AI agent hands-free: say **"小爱同学"** to wake the speaker, then include your trigger keyword (e.g. **"阿峰"**) to route commands to [Hermes Agent](https://github.com/NousResearch/hermes-agent) instead of XiaoAi's default handler.

```
You: "小爱同学，阿峰帮我播放周杰伦的《稻香》"
                    ↓
         XiaoAi Speaker → Xiaomi Cloud
                    ↓ (conversation poll)
         Xiaomi Channel detects "阿峰"
                    ↓
         Hermes Agent processes: "帮我播放周杰伦的《稻香》"
                    ↓
         Agent responds → TTS plays on speaker 🎵
```

## Features

- 🎙️ **Voice activation** — Say "小爱同学" + trigger keyword to talk to Hermes
- 🔊 **TTS responses** — Hermes replies are spoken through the speaker
- 🎵 **Music playback** — Play music via XiaoAi's built-in player
- 📱 **Multi-speaker** — Support for multiple Xiaomi speakers on one account
- 🔌 **Plugin architecture** — Drops into Hermes as a plugin, zero core changes
- 🔒 **Privacy-safe** — No private data in the repo; credentials via env vars

## How It Works

Xiaomi AI Speakers don't support custom wake words or direct webhook integrations. This project uses the **conversation polling** pattern (same approach as [xiaogpt](https://github.com/yihong0618/xiaogpt) and [MiGPT](https://github.com/idootop/mi-gpt)):

1. **Poll** the MiNA cloud API (0.5s interval) for new conversation entries
2. **Detect** the trigger keyword in the user's utterance
3. **Intercept** — stop XiaoAi's default response, extract the actual command
4. **Forward** the command to Hermes via `handle_message()`
5. **Respond** — Hermes processes the command and sends TTS back through the speaker

### Limitations

| Limitation | Detail |
|---|---|
| **Wake word** | Must say "小爱同学" first (hardware-level, cannot be changed without rooting) |
| **Latency** | 1-2 seconds polling delay between speaking and Hermes activation |
| **XiaoAi抢答** | XiaoAi may start its default response before interception kicks in; we mute it as fast as possible |
| **Cloud dependency** | Requires Xiaomi cloud account; internet connection needed |

## Quick Start

### Prerequisites

- A Xiaomi AI Speaker (any model that supports MiNA API)
- A Xiaomi account linked to the speaker (via Mi Home app)
- [Hermes Agent](https://hermes-agent.nousresearch.com/docs/getting-started/installation) installed and running
- Python 3.9+

### 1. Install

```bash
# Clone the repo
git clone https://github.com/kk17/hermes-xiaomi-channel.git
cd hermes-xiaomi-channel

# Install dependencies
pip install -r requirements.txt
```

### 2. Configure

```bash
# Copy env template
cp .env.example .env

# Edit .env with your Xiaomi credentials
MI_USER=your_xiaomi_account@example.com
MI_PASS=your_password
XIAOMI_TRIGGER=阿峰  # or any keyword you want
```

> **Finding your Device DID (optional):**
> ```bash
> export MI_USER=xxx MI_PASS=xxx
> micli mina  # lists all MiNA devices with DIDs
> ```

### 3. Test Connection

Before installing as a Hermes plugin, verify your setup:

```bash
# Test login + device discovery
python3 test_connection.py

# Test TTS (listen to your speaker!)
python3 test_connection.py --tts "你好，我是阿峰"

# Test conversation polling (say something to your speaker)
python3 test_connection.py --poll 30 --trigger 阿峰
```

### 4. Install as Hermes Plugin

```bash
# Copy to Hermes plugins directory
cp -r . ~/.hermes/plugins/xiaomi-speaker/

# Add credentials to Hermes env
echo 'MI_USER=your_account' >> ~/.hermes/.env
echo 'MI_PASS=your_password' >> ~/.hermes/.env
echo 'XIAOMI_TRIGGER=阿峰' >> ~/.hermes/.env

# Restart Hermes gateway
hermes gateway restart
```

### 5. Talk to Your Agent

```
You: "小爱同学，阿峰，今天天气怎么样？"
Speaker: (TTS) "今天新加坡晴天，最高温度32度..."
```

## Configuration

All configuration is via environment variables:

| Variable | Required | Default | Description |
|---|---|---|---|
| `MI_USER` | ✅ | — | Xiaomi account username (email or phone) |
| `MI_PASS` | ✅ | — | Xiaomi account password |
| `XIAOMI_TRIGGER` | ✅ | `阿峰` | Keyword to trigger Hermes (any word/phrase) |
| `MI_DID` | ❌ | auto | Device DID (auto-selects first speaker if empty) |
| `XIAOMI_POLL_INTERVAL` | ❌ | `0.5` | Polling interval in seconds |
| `XIAOMI_MUTE_DEFAULT` | ❌ | `true` | Suppress XiaoAi's default response when triggered |
| `XIAOMI_DEFAULT_DEVICE` | ❌ | — | Default speaker name for multi-speaker setups |

## Project Structure

```
hermes-xiaomi-channel/
├── plugin.yaml              # Hermes plugin metadata
├── adapter.py               # Hermes Platform Adapter (core)
├── xiaomi/
│   ├── __init__.py
│   ├── mina_client.py       # MiNA API client (login, TTS, playback, polling)
│   └── conversation.py      # Conversation interceptor (trigger detection)
├── test_connection.py       # Standalone test script
├── requirements.txt
├── .env.example             # Configuration template (no secrets)
├── .gitignore
├── LICENSE                  # MIT
└── README.md
```

## Supported Speakers

Any Xiaomi AI Speaker that appears in the MiNA device list. Tested models include:

| Model | Name | Status |
|---|---|---|
| `xiaomi.wifispeaker.l15a` | 小米AI音箱2 | ✅ Tested |
| `xiaomi.wifispeaker.lx06` | 小爱音箱Pro | ✅ Community |
| `xiaomi.wifispeaker.lx05` | 小爱音箱Play | ✅ Community |
| `xiaomi.wifispeaker.lx04` | 小爱音箱 | ✅ Community |

## How It Works (Technical Details)

### MiNA API

Uses [Yonsm/MiService](https://github.com/Yonsm/MiService) Python library to interact with Xiaomi's MiNA (Mi AI) cloud service:

- **`device_list()`** — enumerate speakers on the account
- **`send_message(did, text)`** — TTS broadcast to speaker
- **`play(url)`** / **`pause()`** / **`stop()`** — playback control
- **Conversation tracking** — poll for latest user utterances

### Hermes Plugin API

Implements `BasePlatformAdapter` from Hermes gateway:

- **`connect()`** — login + start polling loop
- **`send()`** — TTS the agent's response to the speaker
- **`send_typing()`** — play "让我想想" thinking sound
- **`handle_message()`** — inject intercepted voice commands into Hermes

### Conversation Interception Flow

```
┌─────────────────────────────────────────────────────┐
│  Polling Loop (every 0.5s)                          │
│                                                     │
│  1. GET /device/conversation → latest entry         │
│  2. Is this new? (timestamp > last_seen)            │
│  3. Contains trigger keyword?                       │
│     NO  → ignore, let XiaoAi handle normally        │
│     YES → 4. Stop playback (mute XiaoAi)            │
│            5. Extract command (strip trigger word)   │
│            6. Forward to Hermes via handle_message() │
└─────────────────────────────────────────────────────┘
```

## Troubleshooting

### Login fails with "securityStatus: 16"

Xiaomi requires 2FA verification for new devices/IPs. Open the notification URL in a browser and complete the verification. After verification, subsequent logins should succeed.

### No devices found

Ensure your speaker is set up in the Mi Home (米家) app and linked to the same Xiaomi account.

### TTS doesn't play

Check that the speaker is powered on and connected to the internet. The MiNA API requires cloud connectivity.

### Trigger not detected

- Ensure you say "小爱同学" first to wake the speaker
- The trigger keyword appears in XiaoAi's conversation record
- Try a unique trigger word to avoid conflicts

## Credits

- [Yonsm/MiService](https://github.com/Yonsm/MiService) — MiNA/MiIO cloud service library
- [yihong0618/xiaogpt](https://github.com/yihong0618/xiaogpt) — Inspiration for the polling pattern
- [Hermes Agent](https://github.com/NousResearch/hermes-agent) — AI agent framework

## License

MIT — see [LICENSE](LICENSE)
