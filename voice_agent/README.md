# Voice Agent (Realtime)

`voice_agent/` provides the realtime voice subsystem for X-Plane Co-Pilot.

## Current Status
- Voice orchestration and turn-state machine are implemented.
- Provider fallback is implemented with ordered priority:
  - `openai`
  - `volcengine`
  - `mock`
- If voice startup fails and `VOICE_FALLBACK_TEXT=true`, app continues in text mode.

## Directory

```text
voice_agent/
  __init__.py
  config.py
  events.py
  orchestrator.py
  session.py
  PLAN.md
  ARCHITECTURE.md
  RUNBOOK.md
  metrics.md
  config.example.env
  tests/
```

## Quick Start

Text-only mode (default):

```powershell
python external_agent_chat_ui.py
```

Enable voice with provider fallback:

```powershell
$env:VOICE_ENABLED="true"
$env:VOICE_MODE="hybrid"
$env:VOICE_PROVIDERS="openai,volcengine,mock"
python external_agent_chat_ui.py
```

## Provider Config

OpenAI:
- `OPENAI_API_KEY`
- `OPENAI_REALTIME_MODEL` (default `gpt-realtime`)
- `OPENAI_REALTIME_URL` (default `wss://api.openai.com/v1/realtime`)

Volcengine:
- `VOLCENGINE_APP_ID`
- `VOLCENGINE_ACCESS_TOKEN`
- `VOLCENGINE_REALTIME_URL` (default `wss://openspeech.bytedance.com/api/v3/realtime/dialogue`)

