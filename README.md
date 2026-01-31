# 🤖 Agentic AI - Voice Agent with Telegram Control

An AI-powered phone call agent that speaks naturally using **Gemini Live API**, connects via **Twilio**, and sends real-time transcripts with intent analysis to **Telegram**.

## ✨ Features

- 📞 **Real-time voice calls** - Natural conversations powered by Gemini's native audio
- 🧠 **Intent understanding** - Gemini 3 Flash analyzes what the user wants
- 💬 **Telegram integration** - Live transcripts and command extraction sent to your bot
- 🔄 **Memory** - Conversation context preserved throughout the call
- 🎯 **Command extraction** - Identifies actionable requests (send message, make call, etc.)

## 🏗️ Architecture

```
┌─────────────┐     ┌──────────────────────────────────┐     ┌─────────────┐
│   Phone     │────▶│         AGENTIC AI SERVER        │────▶│  Telegram   │
│   (User)    │◀────│                                  │     │    Bot      │
└─────────────┘     │  ┌─────────┐    ┌─────────────┐  │     └─────────────┘
                    │  │ Twilio  │◀──▶│   Gemini    │  │
┌─────────────┐     │  │ Handler │    │ Live Audio  │  │
│   Twilio    │◀───▶│  └────┬────┘    └──────┬──────┘  │
│   Cloud     │     │       │    Audio       │        │
└─────────────┘     │       └────Bridge──────┘        │
                    │              │                   │
                    │       ┌──────▼──────┐           │
                    │       │ Conversation │           │
                    │       │    Brain     │───────────┼──▶ Telegram
                    │       │ (Gemini 3)   │           │
                    │       └─────────────┘           │
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

You'll need credentials from 4 services:

| Service | Get it from | What you need |
|---------|-------------|---------------|
| **Twilio** | [console.twilio.com](https://console.twilio.com/) | Account SID, Auth Token, Phone Number |
| **Gemini** | [aistudio.google.com/apikey](https://aistudio.google.com/apikey) | API Key |
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

# Gemini (from aistudio.google.com)
GEMINI_API_KEY=your_gemini_api_key_here

# Telegram (from @BotFather)
TELEGRAM_BOT_TOKEN=your_bot_token_here
TELEGRAM_CHAT_ID=your_chat_id_here
```

### Step 4: Expose Your Server (ngrok)

Twilio needs to reach your local server. Use ngrok:

```bash
# Install ngrok (if not already)
brew install ngrok  # Mac
# or download from ngrok.com

# Start tunnel
ngrok http 8080
```

Copy the `https://xxxxx.ngrok.io` URL - you'll need it.

### Step 5: Start the Server

```bash
agenticai server
```

You should see:
```
Starting server on 0.0.0.0:8080
Webhook path: /twilio/voice
WebSocket path: /twilio/media-stream
INFO: Uvicorn running on http://0.0.0.0:8080
```

### Step 6: Make a Call!

In a new terminal:

```bash
agenticai trigger --to +1YOURNUMBER --webhook-url https://xxxxx.ngrok.io
```

Your phone will ring, and you'll see transcripts in Telegram! 🎉

## 📱 What You'll See in Telegram

```
📞 Call started to +1234567890

👤 User: Can you send hi to my WhatsApp group chat?
📌 Intent: send_message
💬 Message: hi
📍 To: WhatsApp group chat

🤖 Clawdy: I'll send that message now. What's the name of the group?

👤 User: It's called Family Chat
📌 Intent: send_message

📋 Call Summary (45s)
• send_message: hi → WhatsApp group chat
📎 Extracted: names: Family Chat
```

## 🔧 Configuration

### config.yaml

Customize the AI behavior:

```yaml
gemini:
  model: "models/gemini-2.5-flash-native-audio-latest"
  voice: "Zephyr"  # Options: Zephyr, Puck, Charon, Kore, Fenrir, Aoede
  system_instruction: |
    You are Clawdy, an AI agent assistant.
    You can send messages, make calls, search the web, and more.
    Be helpful and proactive.

telegram:
  enabled: true
  bot_token: ${TELEGRAM_BOT_TOKEN}
  chat_id: ${TELEGRAM_CHAT_ID}

server:
  host: "0.0.0.0"
  port: 8080
```

### Voice Options

| Voice | Description |
|-------|-------------|
| Zephyr | Warm, friendly |
| Puck | Energetic, playful |
| Charon | Deep, authoritative |
| Kore | Soft, gentle |
| Fenrir | Strong, bold |
| Aoede | Musical, expressive |

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

The **ConversationBrain** (powered by Gemini 3 Flash) does:

1. **Buffers transcripts** - Collects word-by-word audio into complete sentences
2. **Analyzes intent** - Understands what the user wants
3. **Extracts entities** - Pulls out names, numbers, dates, etc.
4. **Sends to Telegram** - Clean, formatted messages (not word-by-word spam)

### Supported Intents

| Intent | Example |
|--------|---------|
| `send_message` | "Send hi to John on WhatsApp" |
| `make_call` | "Call my mom" |
| `search_web` | "Search for nearby restaurants" |
| `set_reminder` | "Remind me to buy milk" |
| `take_note` | "Take a note: meeting at 3pm" |
| `get_info` | "What's the weather today?" |
| `conversation` | General chat |

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

### No audio / Gemini not speaking
- ✅ Check GEMINI_API_KEY is valid
- ✅ Verify the model name supports audio
- ✅ Check server logs for WebSocket errors

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
│   │   └── conversation_brain.py  # Intent analysis
│   ├── gemini/
│   │   └── realtime_handler.py # Gemini Live API
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
