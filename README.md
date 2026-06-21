# 🎓 Virtual Avatar — AI Student System

An automated AI student bot for Google Meet that joins your online classes, listens to lectures, answers when your name is called, and generates detailed notes — all hands-free.

Built for **Fedora Linux** with PipeWire/PulseAudio, OBS Studio, and Brave Browser.

---

## ✨ Features

- **Auto-joins Google Meet** at a scheduled time via Chrome DevTools Protocol (CDP)
- **Listens to the lecture** using Deepgram Nova-2 speech-to-text (English + Hindi/Urdu)
- **Responds when called** — detects your trigger name and answers questions using Gemini AI
- **Speaks back** via ElevenLabs TTS (or free `edge-tts` fallback)
- **Takes exhaustive notes** — uploads audio chunks to Gemini and generates detailed Markdown notes per class
- **Records via OBS** — auto-starts OBS recording at class start, stops at end
- **Schedules multiple classes** — queues up to 4 classes; each session auto-closes its Meet tab and preserves the virtual mic for the next class
- **YouTube Lecture Mode** — screen-records any video lecture and generates notes from Gemini's video + audio analysis
- **Web UI** at `localhost:3737` — schedule classes, manage API keys, view live logs, read notes

---

## 🗂️ Project Structure

```
virtual-avatar/
├── server.js                  # Node.js control server + Web UI backend
├── config.json                # API keys + saved preferences (git-ignored)
├── package.json               # Node dependencies
├── public/                    # Web UI frontend (served by server.js)
│   └── index.html
└── python/
    ├── launch_core.py         # Orchestrator: audio, OBS, Brave, Meet joining
    ├── ai_student.py          # AI student: STT → Gemini Q&A → TTS → notes
    └── youtube_notes.py       # YouTube/video lecture note-taker
```

---

## 🛠️ Requirements

### System (Fedora Linux)
```bash
sudo dnf install obs-studio pipewire-utils pulseaudio-utils brave-browser ffmpeg wf-recorder
```

> **Virtual Camera (one-time setup):**
> ```bash
> sudo dnf install v4l2loopback-dkms -y
> sudo modprobe v4l2loopback exclusive_caps=1 card_label="OBS Virtual Camera"
> ```
> Add to `/etc/modules-load.d/v4l2loopback.conf` to persist across reboots.

### OBS WebSocket
In OBS: **Tools → WebSocket Server Settings** → Enable, set a password, port 4455. Save that password in the UI under Settings → API Keys.

### Node.js
```bash
node -v   # v18+ recommended
npm install
```

### Python (3.12+)
```bash
pip install deepgram-sdk google-genai sounddevice soundfile numpy \
            obsws-python websocket-client edge-tts --break-system-packages
```

---

## ⚙️ Configuration

Copy `config.json` and fill in your keys:

```json
{
  "deepgram_key": "YOUR_DEEPGRAM_API_KEY",
  "gemini_keys": ["YOUR_GEMINI_API_KEY"],
  "elevenlabs_key": "",
  "elevenlabs_voice_id": "21m00Tcm4TlvDq8ikWAM",
  "obs_password": "YOUR_OBS_WEBSOCKET_PASSWORD",
  "student_name": "Your Name",
  "trigger_words": ["yourname", "phonetic_alias"]
}
```

| Key | Required | Where to get it |
|-----|----------|-----------------|
| `deepgram_key` | ✅ Yes | [console.deepgram.com](https://console.deepgram.com) |
| `gemini_keys` | ✅ Yes | [aistudio.google.com](https://aistudio.google.com) — add multiple for quota fallback |
| `elevenlabs_key` | ❌ Optional | [elevenlabs.io](https://elevenlabs.io) — leave blank to use free `edge-tts` |
| `obs_password` | ❌ Optional | Your OBS WebSocket server password |

> **Security:** `config.json` contains your API keys. It is listed in `.gitignore` — never commit it.

---

## 🚀 Getting Started

### 1. Start the server
```bash
node server.js
```
Open **http://localhost:3737** in your browser.

### 2. Add your API keys
Go to **Settings → API Keys**, paste your Deepgram and Gemini keys, click **Save Keys**.

### 3. Set your name and trigger words
Go to **Settings → Profile** — set your student name.  
Go to **Settings → Trigger Words** — add phonetic spellings of your name (e.g. `wali`, `waleed`, `vali`) so the AI knows when the teacher is calling on you.

### 4. Schedule a class
Go to **Schedule Class**, fill in:
- Subject name
- Google Meet link
- Start time / End time
- Notes save folder

Click **Launch**. The system will wait until start time, join Meet automatically, and begin listening.

### 5. YouTube notes (optional)
Go to **Dashboard → YouTube Notes**, enter subject and chunk interval, then play any lecture video — the bot records your screen in chunks and generates notes via Gemini.

---

## 🔄 How It Works

```
server.js (Node)
    │
    ├── Writes .runtime_config.json  (keys + session config)
    │
    └── Spawns: python/launch_core.py
                    │
                    ├── setup_audio()        → creates AI_Mic virtual source (PipeWire)
                    ├── ensure_obs_running() → launches OBS, starts recording
                    ├── wait_until_start()   → holds until scheduled time
                    ├── open_meet_tab()      → opens Meet in Brave via CDP
                    ├── click_join_button()  → injects JS to click "Join now"
                    │       └── select_ai_mic_in_meet() → switches Meet's mic to AI_Mic
                    │
                    └── Spawns: python/ai_student.py
                                    │
                                    ├── Deepgram STT   → transcribes Brave audio stream
                                    ├── Trigger detect → "Wali?" → ask Gemini
                                    ├── Gemini Q&A     → generate answer
                                    ├── edge-tts / ElevenLabs → speak answer via AI_Mic
                                    └── NoteTaker      → chunk audio → Gemini → Markdown notes
```

At end time:
1. AI Student finishes, finalizes notes
2. Meet tab is closed (Brave stays open for next class)
3. OBS recording stopped
4. Virtual mic **kept alive** so the next class reuses it without Brave losing the device

---

## 🎙️ Virtual Mic — How It Works

The system creates two PulseAudio modules:
- `VirtualMic` (null sink) — the AI speaks into this
- `AI_Mic` (virtual source, monitors VirtualMic) — Meet hears this as a microphone

This means the AI's voice goes into Meet, while your physical mic stays private.

> If AI_Mic doesn't appear in Meet's mic dropdown, click the arrow next to the mic icon in Meet and select **AI_Microphone** manually. This only happens on first launch.

---

## 📝 Notes Output

Notes are saved as Markdown files:
```
~/Documents/Class_Notes/
└── SubjectName_DD-MM-YYYY_HHMMam.md
```

Each file contains:
- Timestamped chunks per recording interval
- Full concept explanations with examples
- Q&A pairs when the AI was called on
- Automatically uploaded and analyzed by Gemini 2.5 Flash

---

## 🐛 Troubleshooting

| Problem | Fix |
|---------|-----|
| OBS WebSocket errors in log | Enable WebSocket in OBS → Tools → WebSocket Server Settings |
| AI_Mic not in Meet dropdown | Select it manually once; or run `pactl list short sources` to verify it exists |
| Join button not found | Meet UI is slow to load — the script retries for 60s automatically |
| Gemini 503 error | Temporary quota/load issue — add a second Gemini key in Settings for fallback |
| `v4l2loopback` warning | Run the one-time `modprobe` command shown in Requirements; OBS recording still works without it |
| Class 2 mic missing | Update to latest `launch_core.py` — fixed by preserving AI_Mic between sessions |

---

## 🔒 Security Notes

- `config.json` is **git-ignored** — never commit your API keys
- The Node server binds to `127.0.0.1` only — not accessible from your network
- Brave is launched with `--remote-debugging-port` on localhost only
- The `.runtime_config.json` written before each session is also git-ignored

---

## 📦 Tech Stack

| Component | Technology |
|-----------|-----------|
| Web UI + API | Node.js + Express + WebSocket |
| Meet automation | Chrome DevTools Protocol (CDP) |
| Speech-to-text | Deepgram Nova-2 |
| AI Q&A + Notes | Google Gemini 2.5 Flash |
| Text-to-speech | ElevenLabs / edge-tts |
| Audio routing | PipeWire / PulseAudio virtual sink |
| Screen recording | OBS Studio + wf-recorder |
| Browser | Brave (Chromium-based) |
| OS | Fedora Linux |

---

## 📄 License

MIT License — free to use, modify, and distribute.

---

> Built by [Wali Ahmad Hotak](https://linkedin.com/in/wali-ahmad-hotak-452663297/) — CS student at IMSciences Peshawar
