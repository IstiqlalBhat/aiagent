# 🤖 Agentic AI - Voice Agent with ClawdBot Integration

An AI-powered phone call agent that speaks naturally using **OpenAI Realtime API** (with built-in Whisper for accurate transcription), connects via **Twilio**, and integrates with **ClawdBot** for executing commands through the **OpenClaw Gateway**.

## ✨ Features

- 📞 **Real-time voice calls** - Natural conversations powered by OpenAI Realtime API
- 🎯 **Accurate transcription** - Built-in Whisper STT handles proper nouns correctly
- 📲 **Incoming calls** - Receive calls on your Twilio number, AI answers automatically
- 🧠 **Intent understanding** - Analyzes what the user wants and routes to ClawdBot
- 🤖 **ClawdBot integration** - Execute commands via OpenClaw Gateway (send messages, check emails, play music, etc.)
- 🔄 **Bidirectional communication** - ClawdBot responses are spoken back to the caller
- 💬 **Telegram integration** - Live transcripts and command extraction
- 🖥️ **Background service** - Runs as a daemon, auto-starts on boot
- 🌐 **Tunnel support** - Built-in Cloudflare/ngrok tunnel for webhook URL

## 🏗️ Architecture

```
┌─────────────┐     ┌──────────────────────────────────┐     ┌─────────────────┐
│   Phone     │────▶│         AGENTIC AI SERVER        │────▶│   ClawdBot      │
│   (User)    │◀────│                                  │◀────│   Gateway       │
└─────────────┘     │  ┌─────────┐    ┌─────────────┐  │     └─────────────────┘
                    │  │ Twilio  │◀──▶│   OpenAI    │  │           │
┌─────────────┐     │  │ Handler │    │  Realtime   │  │     ┌─────▼─────┐
│   Twilio    │◀───▶│  └────┬────┘    └──────┬──────┘  │     │ Skills:   │
│   Cloud     │     │       │    Audio       │         │     │ - YouTube │
└─────────────┘     │       └────Bridge──────┘         │     │ - Spotify │
                    │              │                   │     │ - Email   │
                    │       ┌──────▼──────┐            │     │ - etc.    │
                    │       │ Conversation │────────────┼────▶└───────────┘
                    │       │    Brain     │            │
                    │       │  (Intent)    │            │
                    │       └─────────────┘            │
                    └──────────────────────────────────┘
```

## 🚀 Quick Start

### Step 1: Clone & Install

```bash
git clone https://github.com/IstiqlalBhat/aiagent.git
cd aiagent

# Create virtual environment
python -m venv venv
source venv/bin/activate  # Mac/Linux
# venv\Scripts\activate   # Windows

# Install
pip install -e .
```

### Step 2: Get Your API Keys

You'll need credentials from these services:

| Service | Get it from | What you need |
|---------|-------------|---------------|
| **Twilio** | [console.twilio.com](https://console.twilio.com/) | Account SID, Auth Token, Phone Number |
| **OpenAI** | [platform.openai.com/api-keys](https://platform.openai.com/api-keys) | API Key (for Realtime API + Whisper) |
| **Gemini** | [aistudio.google.com/apikey](https://aistudio.google.com/apikey) | API Key (for intent analysis) |
| **Telegram** | [@BotFather](https://t.me/BotFather) on Telegram | Bot Token |
| **Telegram Chat ID** | See instructions below | Your Chat ID |

#### How to get Telegram Chat ID:
1. Message your bot on Telegram (say "hi")
2. Visit: `https://api.telegram.org/bot<YOUR_BOT_TOKEN>/getUpdates`
3. Find `"chat":{"id":123456789}` - that number is your Chat ID

### Step 3: Configure Environment

```bash
# Copy the example file
cp .env.example .env

# Edit with your credentials
nano .env  # or use any editor
```

Fill in your `.env`:
```env
# Twilio (from console.twilio.com)
TWILIO_ACCOUNT_SID=ACxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx
TWILIO_AUTH_TOKEN=your_auth_token_here
TWILIO_PHONE_NUMBER=+1XXXXXXXXXX

# OpenAI (for Realtime API + Whisper STT)
OPENAI_API_KEY=sk-proj-your_openai_api_key_here

# Gemini (for intent analysis)
GEMINI_API_KEY=your_gemini_api_key_here

# Telegram (from @BotFather)
TELEGRAM_BOT_TOKEN=your_bot_token_here
TELEGRAM_CHAT_ID=your_chat_id_here
```

### Step 4: Start a Tunnel (ngrok)

Twilio needs to reach your local server. Use ngrok:

```bash
# Install ngrok if needed
brew install ngrok  # Mac
# or download from https://ngrok.com/download

# Start tunnel
agenticai tunnel start
# or directly: ngrok http 8080
```

Copy the public URL that appears (e.g., `https://xxxx.ngrok.io`).

### Step 5: Install as Background Service

Run the agent as a daemon that auto-starts on boot:

```bash
# Install the service with your ngrok URL
agenticai service install --webhook-url https://xxxx.ngrok.io

# Start it
agenticai service start

# Check status
agenticai service status
```

The server now runs in the background! View logs with:
```bash
agenticai service logs -f
```

### Step 6: Make a Call!

```bash
agenticai trigger --to +1YOURNUMBER --webhook-url https://xxxx.ngrok.io
```

Your phone will ring, and the AI will answer! 🎉

### Receive Incoming Calls

Configure your Twilio number to point to your webhook:

1. Go to [Twilio Console](https://console.twilio.com/) → Phone Numbers → Your Number
2. Under "Voice & Fax", set:
   - "A call comes in" → Webhook → `https://xxxx.ngrok.io/twilio/voice`
3. Save

Now when someone calls your Twilio number, the AI will answer!

## 🤖 ClawdBot Integration

This agent connects to [ClawdBot](https://github.com/AceDZN/clawdbot) via the OpenClaw Gateway for executing commands:

### Supported Commands (via ClawdBot skills)

| Command | Example |
|---------|---------|
| **YouTube** | "Open YouTube and search for Zayn Dusk Till Dawn" |
| **Spotify** | "Play Shape of You on Spotify" |
| **Email** | "Check my emails" |
| **Messages** | "Send hi to John on WhatsApp" |
| **Web Search** | "Search for nearby restaurants" |
| **And more...** | Any ClawdBot skill |

### How it Works

1. **User speaks** → OpenAI Realtime transcribes with Whisper
2. **Brain analyzes** → Determines if it's an actionable command
3. **ClawdBot executes** → Runs the command via OpenClaw Gateway
4. **Response spoken** → AI speaks the result back to the caller

## 🔧 Configuration

### config.yaml

```yaml
# OpenAI Realtime API (primary voice)
openai_realtime:
  enabled: true
  api_key: ${OPENAI_API_KEY}
  model: "gpt-4o-realtime-preview-2024-12-17"
  voice: "alloy"  # Options: alloy, echo, fable, onyx, nova, shimmer

# Gemini (for intent analysis only)
gemini:
  api_key: ${GEMINI_API_KEY}
  model: "models/gemini-2.5-flash-native-audio-latest"

# Telegram integration
telegram:
  enabled: true
  bot_token: ${TELEGRAM_BOT_TOKEN}
  chat_id: ${TELEGRAM_CHAT_ID}

# OpenClaw Gateway (for ClawdBot)
gateway:
  url: "ws://127.0.0.1:18789"

server:
  host: "0.0.0.0"
  port: 8080
```

### Voice Options (OpenAI)

| Voice | Description |
|-------|-------------|
| alloy | Neutral, balanced |
| echo | Warm, conversational |
| fable | Expressive, storytelling |
| onyx | Deep, authoritative |
| nova | Friendly, upbeat |
| shimmer | Soft, gentle |

## 📋 CLI Commands

```bash
# Start the server
agenticai server

# Trigger a call
agenticai trigger --to +1234567890 --webhook-url https://xxx.ngrok.io

# Check server health
agenticai status

# View help
agenticai --help
```

## 🧠 How the Brain Works

The **ConversationBrain** does:

1. **Receives transcripts** - From OpenAI Whisper (accurate proper nouns!)
2. **Analyzes intent** - Determines if user wants to DO something
3. **Routes to ClawdBot** - Sends actionable commands via OpenClaw Gateway
4. **Handles responses** - Feeds ClawdBot responses back to OpenAI to speak

### Example Flow

```
User: "Check my emails"
  ↓
Brain: Detects actionable intent (YES)
  ↓
ClawdBot: Executes gog skill for Gmail
  ↓
Response: "You have 3 unread emails: 1 from John about..."
  ↓
OpenAI: Speaks the response to the caller
```

## 🔒 Security

- ✅ All secrets in `.env` (gitignored)
- ✅ No hardcoded credentials
- ✅ Config uses `${VAR_NAME}` expansion
- ✅ `.env.example` has only placeholders

**Never commit your `.env` file!**

## 🐛 Troubleshooting

### Call not connecting
- ✅ Check Twilio credentials in `.env`
- ✅ Verify ngrok is running and URL is correct
- ✅ Ensure your Twilio number is configured for voice

### No audio / AI not speaking
- ✅ Check OPENAI_API_KEY is valid and has Realtime API access
- ✅ Verify the model name is correct
- ✅ Check server logs for WebSocket errors

### ClawdBot not responding
- ✅ Ensure OpenClaw Gateway is running on port 18789
- ✅ Check ClawdBot agent is started: `clawdbot agent --session-id agent:main:main`
- ✅ Verify the skill you're using is configured

### No Telegram messages
- ✅ Verify TELEGRAM_BOT_TOKEN is correct
- ✅ Check TELEGRAM_CHAT_ID is your actual chat ID
- ✅ Make sure you've messaged the bot at least once

### Server won't start
- ✅ Check port 8080 is not in use
- ✅ Verify all required env vars are set
- ✅ Check Python version is 3.11+

## 📁 Project Structure

```
aiagent/
├── .env.example          # Template for secrets
├── config.yaml           # Main configuration
├── src/agenticai/
│   ├── cli.py            # Command-line interface
│   ├── core/
│   │   ├── config.py           # Config loading
│   │   ├── call_manager.py     # Call lifecycle
│   │   ├── audio_bridge.py     # Audio routing
│   │   └── conversation_brain.py  # Intent analysis + ClawdBot
│   ├── openai/
│   │   └── realtime_handler.py # OpenAI Realtime API
│   ├── audio/
│   │   ├── converter.py        # Audio format conversion
│   │   └── whisper_stt.py      # Whisper STT (optional)
│   ├── twilio/
│   │   ├── client.py           # REST API
│   │   └── websocket.py        # Media Streams
│   ├── telegram/
│   │   └── direct_client.py    # Telegram Bot API
│   └── server/
│       └── app.py              # FastAPI server
└── tests/
```

## 🤝 Contributing

1. Fork the repo
2. Create a feature branch
3. Make your changes
4. Submit a PR

## 📄 License

MIT

---

Made with ❤️ by [Istiqlal](https://github.com/IstiqlalBhat)
