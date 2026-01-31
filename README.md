# Agentic AI

Automated phone call system that conducts real-time voice conversations using **Twilio** for telephony, **Google Gemini Live API** for AI voice, and sends structured results to **OpenClaw Gateway**.

## Architecture

```
+----------------+     +----------------------------------+     +----------------+
|  CLI / Cron    |     |        AGENTIC AI SERVER         |     |   OpenClaw     |
|  Scheduler     |---->|                                  |---->|   Gateway      |
+----------------+     |  +------------+  +------------+  |     | ws://127.0.0.1 |
                       |  | Twilio WS  |  | Gemini WS  |  |     |    :18789      |
+----------------+     |  | Handler    |<>| Handler    |  |     +----------------+
|  Twilio Cloud  |<--->|  +-----+------+  +------+-----+  |
|  Media Streams |     |        |    Audio       |        |
|  (mulaw 8kHz)  |     |        +----Bridge------+        |
+----------------+     +----------------------------------+
```

## How It Works

1. **Call Initiation**: CLI or scheduler triggers an outbound call via Twilio REST API
2. **Media Stream**: Twilio connects a bidirectional WebSocket for real-time audio
3. **Audio Bridge**: Converts audio formats between Twilio (mulaw 8kHz) and Gemini (PCM 16/24kHz)
4. **AI Conversation**: Gemini Live API handles the voice conversation with barge-in support
5. **Gateway Integration**: Transcripts, structured data, and call results are sent to OpenClaw Gateway

## Prerequisites

- Python 3.11+
- Twilio account with a phone number
- Google AI API key (Gemini)
- OpenClaw Gateway running on `ws://127.0.0.1:18789`
- ngrok or similar for exposing webhooks to Twilio

## Installation

```bash
# Clone the repository
git clone https://github.com/IstiqlalBhat/aiagent.git
cd aiagent

# Create virtual environment
python -m venv venv
source venv/bin/activate  # Linux/Mac
# or: venv\Scripts\activate  # Windows

# Install the package
pip install -e .
```

## Configuration

### 1. Environment Variables

Copy `.env.example` to `.env` and fill in your credentials:

```bash
cp .env.example .env
```

```env
# Twilio Credentials
TWILIO_ACCOUNT_SID=ACxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx
TWILIO_AUTH_TOKEN=your_auth_token_here

# Gemini API Key
GEMINI_API_KEY=your_gemini_api_key_here
```

### 2. Configuration File

Edit `config.yaml` to set your Twilio phone number and customize settings:

```yaml
twilio:
  account_sid: ${TWILIO_ACCOUNT_SID}
  auth_token: ${TWILIO_AUTH_TOKEN}
  from_number: "+1XXXXXXXXXX"  # Your Twilio phone number

gemini:
  api_key: ${GEMINI_API_KEY}
  model: "models/gemini-2.5-flash-native-audio-preview-12-2025"
  voice: "Zephyr"  # Options: Zephyr, Puck, Charon, Kore, Fenrir, Aoede
  system_instruction: |
    You are a helpful AI assistant making phone calls on behalf of the user.
    Be concise, professional, and friendly.

gateway:
  url: "ws://127.0.0.1:18789"
  reconnect_max_attempts: 10
  reconnect_base_delay: 1.0
  reconnect_max_delay: 60.0

server:
  host: "0.0.0.0"
  port: 8080
  webhook_path: "/twilio/voice"
  websocket_path: "/twilio/media-stream"
```

## OpenClaw Gateway Integration

### Connection

Agentic AI connects to OpenClaw Gateway via WebSocket at `ws://127.0.0.1:18789`. The connection features:

- **Automatic reconnection** with exponential backoff
- **Message queuing** when disconnected (messages sent after reconnect)
- **Heartbeat** keepalive every 30 seconds

### Message Protocol

All messages are sent using **JSON-RPC 2.0** via the `sessions_send` method:

```json
{
  "jsonrpc": "2.0",
  "id": 1,
  "method": "sessions_send",
  "params": {
    "message": { ... }
  }
}
```

### Message Types

#### 1. `call_started`
Sent when a call is initiated.

```json
{
  "message_type": "call_started",
  "call_id": "uuid-string",
  "to_number": "+15551234567",
  "prompt": "Schedule an appointment for tomorrow",
  "metadata": {
    "purpose": "appointment",
    "priority": "high"
  },
  "timestamp": "2024-01-15T10:30:00.000000"
}
```

#### 2. `transcript`
Sent for each transcript segment during the call.

```json
{
  "message_type": "transcript",
  "call_id": "uuid-string",
  "speaker": "assistant",  // or "user"
  "text": "Hello, how can I help you today?",
  "timestamp": "2024-01-15T10:30:05.000000",
  "is_final": true
}
```

#### 3. `structured_data`
Sent when structured information is extracted from the conversation.

```json
{
  "message_type": "structured_data",
  "call_id": "uuid-string",
  "intent": "schedule_appointment",
  "entities": {
    "date": "2024-01-20",
    "time": "14:00",
    "service": "consultation"
  },
  "summary": "User wants to schedule a consultation appointment",
  "confidence": 0.95,
  "timestamp": "2024-01-15T10:32:00.000000"
}
```

#### 4. `action`
Sent when an action should be taken by the gateway.

```json
{
  "message_type": "action",
  "call_id": "uuid-string",
  "action_type": "send_confirmation_email",
  "parameters": {
    "to": "user@example.com",
    "appointment_date": "2024-01-20"
  },
  "timestamp": "2024-01-15T10:33:00.000000"
}
```

#### 5. `call_ended`
Sent when a call ends.

```json
{
  "message_type": "call_ended",
  "call_id": "uuid-string",
  "duration": 125.5,
  "outcome": "completed",  // or "failed", "no-answer", "busy", "canceled"
  "full_transcript": "Assistant: Hello...\nUser: Hi...",
  "summary": "Successfully scheduled appointment for Jan 20",
  "timestamp": "2024-01-15T10:35:00.000000"
}
```

### OpenClaw Handler Example

Here's how to handle these messages in OpenClaw:

```python
# In your OpenClaw Gateway handler
async def handle_sessions_send(params):
    message = params.get("message", {})
    msg_type = message.get("message_type")

    if msg_type == "call_started":
        # Log call initiation, prepare for transcripts
        call_id = message["call_id"]
        print(f"Call started: {call_id} to {message['to_number']}")

    elif msg_type == "transcript":
        # Process real-time transcripts
        speaker = message["speaker"]
        text = message["text"]
        print(f"{speaker}: {text}")

    elif msg_type == "structured_data":
        # Handle extracted data (appointments, orders, etc.)
        intent = message["intent"]
        entities = message["entities"]
        # Trigger workflows based on intent

    elif msg_type == "action":
        # Execute requested actions
        action_type = message["action_type"]
        params = message["parameters"]
        # e.g., send emails, update databases

    elif msg_type == "call_ended":
        # Store call record, trigger post-call workflows
        duration = message["duration"]
        outcome = message["outcome"]
        transcript = message["full_transcript"]
```

## Usage

### Start the Server

First, expose your local server to the internet using ngrok:

```bash
ngrok http 8080
```

Note the `https://xxxx.ngrok.io` URL.

Start the Agentic AI server:

```bash
agenticai server
```

### Make a Call

```bash
agenticai call \
  --to "+15551234567" \
  --prompt "Call to schedule an appointment for tomorrow afternoon" \
  --webhook-url https://xxxx.ngrok.io
```

### View Status

```bash
agenticai status
```

### Scheduling

List configured schedules:

```bash
agenticai schedule list
```

Run a schedule manually:

```bash
agenticai schedule run morning_check --webhook-url https://xxxx.ngrok.io
```

Run in daemon mode (server + scheduler):

```bash
agenticai daemon --webhook-url https://xxxx.ngrok.io
```

### Configure Schedules

Edit `schedules.yaml`:

```yaml
schedules:
  - name: "morning_check"
    cron: "0 9 * * 1-5"  # 9 AM, Monday-Friday
    enabled: true
    calls:
      - to_number: "+15551234567"
        prompt: "Good morning check-in call"
        metadata:
          purpose: "daily_checkin"
          priority: "normal"

  - name: "appointment_reminder"
    cron: "0 8 * * *"  # 8 AM daily
    enabled: true
    calls:
      - to_number: "+15559876543"
        prompt: "Reminder about today's appointment"
        metadata:
          purpose: "reminder"
```

## Audio Pipeline

The system handles real-time audio conversion between Twilio and Gemini:

| Direction | Source | Target | Conversion |
|-----------|--------|--------|------------|
| Inbound | Twilio (mulaw 8kHz) | Gemini (PCM 16kHz) | mulaw→PCM, resample 8→16kHz |
| Outbound | Gemini (PCM 24kHz) | Twilio (mulaw 8kHz) | resample 24→8kHz, PCM→mulaw |

**Barge-in Support**: When the user interrupts the AI, Twilio's audio buffer is cleared immediately for natural conversation flow.

## Testing

Run the test suite:

```bash
pip install -e ".[dev]"
pytest
```

## Project Structure

```
├── config.yaml              # Main configuration
├── schedules.yaml           # Cron schedules
├── src/agenticai/
│   ├── cli.py               # CLI interface
│   ├── core/
│   │   ├── config.py        # Configuration loading
│   │   ├── audio_bridge.py  # Twilio <-> Gemini bridge
│   │   └── call_manager.py  # Call lifecycle
│   ├── audio/
│   │   └── converter.py     # Audio format conversion
│   ├── twilio/
│   │   ├── client.py        # REST API client
│   │   └── websocket.py     # Media Streams handler
│   ├── gemini/
│   │   └── handler.py       # Gemini Live API
│   ├── gateway/
│   │   ├── client.py        # OpenClaw WebSocket client
│   │   └── messages.py      # Message types
│   ├── scheduler/
│   │   └── scheduler.py     # APScheduler cron
│   └── server/
│       └── app.py           # FastAPI webhooks
└── tests/
```

## Troubleshooting

### Call not connecting
- Verify Twilio credentials in `.env`
- Check that ngrok is running and URL is correct
- Ensure your Twilio number is configured for voice

### No audio
- Check Gemini API key is valid
- Verify the model name in `config.yaml`
- Check server logs for WebSocket errors

### Gateway not receiving messages
- Ensure OpenClaw Gateway is running on port 18789
- Check gateway logs for connection attempts
- Verify firewall allows local WebSocket connections

## License

MIT
