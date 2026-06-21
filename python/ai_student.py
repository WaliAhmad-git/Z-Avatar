#!/usr/bin/env python3
"""
Virtual Avatar — AI Student  (v3 — UI Edition)
───────────────────────────────────────────────
OS: Fedora Linux (PipeWire/PulseAudio)
STT: Deepgram Nova-2  (dual English + Hindi/Urdu parallel)
AI:  Gemini cascade   (Q&A + exhaustive note-taking)
TTS: ElevenLabs if key provided, else edge-tts (free)
Trigger words: loaded from .runtime_config.json (saved once in UI)

Changes from v2:
  - ElevenLabs TTS integration (with edge-tts fallback)
  - Trigger words loaded from runtime config (not hardcoded)
  - Notes prompt expanded to request max-length output (~65k tokens)
  - Notes prompt now requires real-world examples relevant to lecture
  - OBS obs_scene calls ready to enable (one-line uncomment)
  - All config sourced from .runtime_config.json written by server.js
"""

import os, sys, time, threading, queue, subprocess, asyncio, uuid, json
from datetime import datetime

import numpy as np
import sounddevice as sd
import soundfile as sf

from deepgram import DeepgramClient
from google import genai

# ══════════════════════════════════════════════════════
#  RUNTIME CONFIG — written by server.js before launching
# ══════════════════════════════════════════════════════
_SCRIPT_DIR   = os.path.dirname(os.path.abspath(__file__))
_PROJECT_ROOT = os.path.dirname(_SCRIPT_DIR)

# Search for .runtime_config.json in multiple locations
_RUNTIME_CANDIDATES = [
    os.path.join(_PROJECT_ROOT, ".runtime_config.json"),   # normal: project root
    os.path.join(_SCRIPT_DIR,   ".runtime_config.json"),   # run directly from python/
    os.path.join(os.getcwd(),   ".runtime_config.json"),   # wherever shell is
]

def _load_runtime() -> dict:
    for path in _RUNTIME_CANDIDATES:
        if os.path.exists(path):
            try:
                with open(path) as f:
                    data = json.load(f)
                print(f"📋 Loaded runtime config: {path}", flush=True)
                return data
            except Exception as e:
                print(f"⚠️  Failed to parse {path}: {e}", flush=True)
    print("⚠️  No .runtime_config.json found — checking config.json for keys...", flush=True)

    # Last resort: try reading config.json directly
    for cfg_dir in [_PROJECT_ROOT, _SCRIPT_DIR, os.getcwd()]:
        cfg_path = os.path.join(cfg_dir, "config.json")
        if os.path.exists(cfg_path):
            try:
                with open(cfg_path) as f:
                    data = json.load(f)
                print(f"📋 Loaded from config.json: {cfg_path}", flush=True)
                return data
            except Exception:
                pass
    return {}

_RT = _load_runtime()

DEEPGRAM_API_KEY    = _RT.get("deepgram_key", "")
# Backward compatible: prefer the new "gemini_keys" array; fall back to the
# old single "gemini_key" string if that's all an older config has.
GEMINI_API_KEYS      = [k for k in _RT.get("gemini_keys", []) if k] or \
                        ([_RT["gemini_key"]] if _RT.get("gemini_key") else [])
ELEVENLABS_API_KEY  = _RT.get("elevenlabs_key", "")
ELEVENLABS_VOICE_ID = _RT.get("elevenlabs_voice_id", "21m00Tcm4TlvDq8ikWAM")  # Rachel
PRIMARY_NAME        = _RT.get("student_name", "Student")
OBS_PASSWORD        = _RT.get("obs_password", "")

print(
    f"🔑 Key status — deepgram: {'✅' if DEEPGRAM_API_KEY else '❌ MISSING'}, "
    f"gemini: {'✅ (' + str(len(GEMINI_API_KEYS)) + ' key' + ('s' if len(GEMINI_API_KEYS) != 1 else '') + ')' if GEMINI_API_KEYS else '❌ MISSING'}, "
    f"elevenlabs: {'✅ (will use)' if ELEVENLABS_API_KEY else 'none (falling back to edge-tts)'}",
    flush=True
)

# ── Trigger words ─────────────────────────────────────
# Loaded from config (set once in UI). Falls back to built-in aliases for Wali.
_CONFIG_TRIGGERS = _RT.get("trigger_words", [])

NAME_ALIASES = list({w.lower().strip() for w in _CONFIG_TRIGGERS if w.strip()}) or [
    # Default phonetic aliases (Latin)
    "wali", "willie", "wally", "valey", "wily", "vali", "walee",
    "volley", "walid", "wali ahmad", "wali ahmad hotak", "hotak", "ahmad",
    # Devanagari
    "\u0935\u0932\u0940", "\u0935\u0939\u0940", "\u0935\u0932\u093f", "\u0935\u093e\u0932\u0940",
    "\u0935\u0932\u0940 \u0905\u0939\u092e\u0926", "\u0935\u0932\u0940 \u0905\u0939\u092e\u0926 \u0939\u094b\u0924\u0915",
    "\u0939\u094b\u0924\u0915", "\u0935\u0949\u0932\u0940", "\u0935\u0947\u0932\u0940",
    # Urdu script
    "\u0648\u0644\u06cc", "\u0648\u0644\u06cc \u0627\u062d\u0645\u062f", "\u06c1\u0648\u062a\u06a9",
]

# ══════════════════════════════════════════════════════
#  CONSTANTS
# ══════════════════════════════════════════════════════
OBS_HOST      = "localhost"
OBS_PORT      = 4455
SAMPLE_RATE   = 48000
BLOCKSIZE     = 2048
CHUNK_MINUTES = 15          # how often to generate notes
MAX_NOTES_TOKENS = 65000    # passed in prompt so Gemini knows target length

# ══════════════════════════════════════════════════════
#  NOTES FILE
# ══════════════════════════════════════════════════════
if _RT.get("notes_path"):
    NOTES_FILE = _RT["notes_path"]
    _dir = os.path.dirname(NOTES_FILE)
    try:
        if _dir:
            os.makedirs(_dir, exist_ok=True)
    except (PermissionError, OSError) as e:
        print(f"⚠️  Notes path '{_dir}' is unwritable ({e}) — falling back to ~/Documents/Class_Notes", flush=True)
        _save_dir = os.path.expanduser("~/Documents/Class_Notes")
        os.makedirs(_save_dir, exist_ok=True)
        NOTES_FILE = os.path.join(_save_dir, os.path.basename(NOTES_FILE))
else:
    _save_dir  = os.path.expanduser("~/Documents/Class_Notes")
    os.makedirs(_save_dir, exist_ok=True)
    _date_str  = datetime.now().strftime("%Y-%m-%d")
    NOTES_FILE = os.path.join(_save_dir, f"Master_Notes_{_date_str}.md")

FILE_LOCK = threading.Lock()

with open(NOTES_FILE, "w", encoding="utf-8") as _f:
    _f.write(f"# 📚 Class Notes — {_RT.get('subject','Class')}\n"
             f"**Date:** {datetime.now().strftime('%B %d, %Y')}\n"
             f"**Student:** {PRIMARY_NAME}\n\n")
print(f"📁 Notes file: {NOTES_FILE}", flush=True)

# ══════════════════════════════════════════════════════
#  API CLIENTS
# ══════════════════════════════════════════════════════
if not DEEPGRAM_API_KEY:
    print("❌ DEEPGRAM_API_KEY is missing. Set it in the UI (Settings → API Keys) or in config.json.", flush=True)
    sys.exit(1)
if not GEMINI_API_KEYS:
    print("❌ No Gemini API key found. Set at least one in the UI (Settings → API Keys) or in config.json.", flush=True)
    sys.exit(1)
dg_client = DeepgramClient(api_key=DEEPGRAM_API_KEY)

# One genai.Client per Gemini key, so a quota-exhausted key can be skipped
# entirely (not just a quota-exhausted model) by moving to the next client.
ai_clients = [genai.Client(api_key=k) for k in GEMINI_API_KEYS]
ai_client = ai_clients[0]  # kept for any code path that still wants "the" client

# ══════════════════════════════════════════════════════
#  GLOBAL STATE
# ══════════════════════════════════════════════════════
is_responding  = False
recording      = True
last_qa_time   = 0.0

QA_MODELS = [
    "gemini-2.5-flash",
    "gemini-2.0-flash",
    "gemini-1.5-flash",
]
qa_key_index   = 0
qa_model_index = 0

NOTES_MODELS = [
    "gemini-2.5-flash",
    "gemini-2.0-flash",
    "gemini-1.5-flash",
]
notes_key_index   = 0
notes_model_index = 0

audio_queue   = queue.Queue(maxsize=1000)
stt_buffer    = []
notes_buffer  = []
silence_count = 0
stt_lock      = threading.Lock()

# Rolling text buffer of recently spoken lecture content (not just the
# trigger-word chunk). This is what gives ask_gemini_question() real
# context to answer from — without it, the only "transcript" it sees is
# the few seconds of audio that contained the trigger word itself, which
# is usually just the name being called, not the actual question.
RECENT_TRANSCRIPT_CHARS = 3000
recent_transcript = ""
recent_transcript_lock = threading.Lock()

def _append_recent_transcript(text: str):
    global recent_transcript
    if not text:
        return
    with recent_transcript_lock:
        recent_transcript = (recent_transcript + " " + text).strip()
        if len(recent_transcript) > RECENT_TRANSCRIPT_CHARS:
            recent_transcript = recent_transcript[-RECENT_TRANSCRIPT_CHARS:]

def _get_recent_transcript() -> str:
    with recent_transcript_lock:
        return recent_transcript

# ══════════════════════════════════════════════════════
#  TTS — ElevenLabs if key provided, else edge-tts
# ══════════════════════════════════════════════════════
_tts_loop = asyncio.new_event_loop()

def _run_tts_loop():
    asyncio.set_event_loop(_tts_loop)
    _tts_loop.run_forever()

threading.Thread(target=_run_tts_loop, daemon=True, name="TTS-Loop").start()


def run_tts_sync(text: str):
    """Submit TTS to dedicated async loop and block until audio plays."""
    future = asyncio.run_coroutine_threadsafe(_tts_coro(text), _tts_loop)
    future.result()


async def _tts_coro(text: str):
    temp_file = f"/tmp/tts_{uuid.uuid4().hex}"

    if ELEVENLABS_API_KEY:
        # ── ElevenLabs ───────────────────────────────
        await _elevenlabs_tts(text, temp_file + ".mp3")
        audio_path = temp_file + ".mp3"
    else:
        # ── edge-tts fallback ─────────────────────────
        import edge_tts
        audio_path = temp_file + ".mp3"
        communicate = edge_tts.Communicate(text, "en-US-ChristopherNeural")
        await communicate.save(audio_path)

    try:
        # Play through virtual mic (PipeWire)
        subprocess.run(["pw-play", "--target=VirtualMic", audio_path], check=True,
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except Exception:
        try:
            subprocess.run(["pw-play", audio_path], check=True,
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        except Exception as e:
            print(f"❌ Audio playback error: {e}", flush=True)
    finally:
        if os.path.exists(audio_path):
            os.remove(audio_path)


async def _elevenlabs_tts(text: str, out_path: str):
    """
    Call ElevenLabs text-to-speech API and save MP3 to out_path.
    Uses the streaming endpoint for low latency.
    """
    import urllib.request, urllib.error

    url = f"https://api.elevenlabs.io/v1/text-to-speech/{ELEVENLABS_VOICE_ID}/stream"
    payload = json.dumps({
        "text": text,
        "model_id": "eleven_turbo_v2",
        "voice_settings": {"stability": 0.5, "similarity_boost": 0.75}
    }).encode()

    req = urllib.request.Request(
        url,
        data=payload,
        headers={
            "xi-api-key":   ELEVENLABS_API_KEY,
            "Content-Type": "application/json",
            "Accept":       "audio/mpeg",
        },
        method="POST"
    )

    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            with open(out_path, "wb") as f:
                f.write(resp.read())
        print(f"🔊 ElevenLabs TTS: {len(text)} chars → {out_path}", flush=True)
    except urllib.error.HTTPError as e:
        print(f"⚠️  ElevenLabs error {e.code} — falling back to edge-tts", flush=True)
        import edge_tts
        communicate = edge_tts.Communicate(text, "en-US-ChristopherNeural")
        await communicate.save(out_path)

# ══════════════════════════════════════════════════════
#  OBS — scene switching (idle ↔ talking)
# ══════════════════════════════════════════════════════
# Reuses one connection across the whole session rather than reconnecting
# per call — scene switches happen every time the avatar starts/stops
# talking, which would otherwise mean a new WebSocket handshake each time.
_obs_client = None
_obs_lock = threading.Lock()

def _get_obs_scene_client():
    global _obs_client
    with _obs_lock:
        if _obs_client is not None:
            return _obs_client
        try:
            import obsws_python as obs
        except ImportError:
            try:
                subprocess.run(["pip", "install", "obsws-python", "--break-system-packages", "-q"])
                import obsws_python as obs
            except Exception:
                print("⚠️  obsws-python unavailable — OBS scene switching disabled.", flush=True)
                return None
        try:
            _obs_client = obs.ReqClient(host=OBS_HOST, port=OBS_PORT, password=OBS_PASSWORD, timeout=5)
            print("🎬 Connected to OBS for scene switching.", flush=True)
            return _obs_client
        except Exception as e:
            print(f"⚠️  OBS scene switching disabled — couldn't connect ({e}).", flush=True)
            return None

def obs_scene(scene_name: str):
    """Switch OBS's current program scene. Silently no-ops on failure —
    a missed scene switch should never interrupt the class session."""
    client = _get_obs_scene_client()
    if not client:
        return
    try:
        client.set_current_program_scene(scene_name)
    except Exception as e:
        # Connection may have dropped (e.g. OBS restarted) — drop the cached
        # client so the next call retries a fresh connection.
        global _obs_client
        print(f"⚠️  OBS scene switch to '{scene_name}' failed: {e}", flush=True)
        with _obs_lock:
            _obs_client = None

# ══════════════════════════════════════════════════════
#  GEMINI — Q&A (short, conversational)
# ══════════════════════════════════════════════════════
def ask_gemini_question(recent_speech: str) -> str:
    global qa_key_index, qa_model_index

    notes_context = ""
    try:
        with open(NOTES_FILE, "r", encoding="utf-8") as f:
            notes_context = f.read()[-4000:]
    except Exception:
        pass

    prompt = f"""You are {PRIMARY_NAME}, a university student in a live online lecture.

CONTEXT from today's class notes:
{notes_context or "No notes yet — class just started."}

RECENT SPOKEN AUDIO (last few minutes, transcribed — may include the teacher
calling your name partway through; treat whatever was said just before your
name as the actual question/prompt you're being asked to respond to):
"{recent_speech}"

RESPONSE RULES:
- Always respond in English only. Never use Urdu, Hindi, or any other language.
- Roll call / attendance → say exactly "Present sir." or "Present ma'am." (match the teacher's gender from context).
- Question about class content → answer ONLY from the notes above. Never fabricate topics.
- General question → answer naturally, briefly, max 2-3 sentences.
- If the recent audio doesn't contain a clear question (e.g. it's just chatter
  or your name with no follow-up), default to "Present sir."
- Do NOT use bullet points, markdown, numbered lists, or AI-sounding openers.
- Sound like a real human student speaking out loud. Be confident and natural.
"""
    # Try every (key, model) combination, starting from wherever we last
    # succeeded. A 429 steps to the next model on the same key; once a key's
    # models are all exhausted, move to the next key and start back at its
    # first (strongest) model. After cycling through everything once, give
    # up for this call rather than spinning forever.
    attempts = len(GEMINI_API_KEYS) * len(QA_MODELS)
    for _ in range(attempts):
        client = ai_clients[qa_key_index]
        model  = QA_MODELS[qa_model_index]
        try:
            response = client.models.generate_content(model=model, contents=prompt)
            return response.text.strip()
        except Exception as e:
            if "429" in str(e):
                qa_model_index += 1
                if qa_model_index >= len(QA_MODELS):
                    qa_model_index = 0
                    qa_key_index = (qa_key_index + 1) % len(GEMINI_API_KEYS)
                    print(f"⚠️  [key #{qa_key_index} -+1] quota hit on all models for previous key — "
                          f"switching to Gemini key #{qa_key_index + 1}/{len(GEMINI_API_KEYS)}...", flush=True)
                else:
                    print(f"⚠️  [{model}] quota hit — stepping down...", flush=True)
            else:
                print(f"❌ Gemini Q&A error ({model}): {e}", flush=True)
                return "Present sir."
    print("❌ All Gemini keys/models exhausted for this request.", flush=True)
    return "Present sir."

# ══════════════════════════════════════════════════════
#  GEMINI — NOTE TAKING (maximum length, rich examples)
# ══════════════════════════════════════════════════════
NOTES_PROMPT = """You are an expert university teaching assistant and note-taker with encyclopedic knowledge.
Listen to this lecture audio chunk and produce the most exhaustive, comprehensive markdown notes possible.

TARGET: Fill as much of a {max_tokens}-token response as possible. Do NOT stop early.
If you run out of things from the audio, add deeply relevant supplementary explanation.

MANDATORY SECTIONS — include ALL that apply:
## 📌 Topic Overview
## 🔑 Key Concepts & Definitions
## 📐 Formulas, Algorithms, Pseudocode (with full derivation steps)
## 🧠 Detailed Explanations (explain *why*, not just *what*)
## 🌍 Real-World Examples
  - Minimum 5 concrete, realistic examples per major concept
  - Examples must be DIRECTLY relevant to the lecture topic
  - Each example: explain the scenario, show how the concept applies, give expected outcome
## 🔗 Connections to Other Topics
## ⚠️ Common Mistakes & Misconceptions
## 📝 Points the Professor Emphasized (quote or paraphrase exactly)
## 🧪 Practice Problems with Full Solutions
## 💡 Exam Tips

STYLE RULES:
- Write in rich markdown: use headers, subheaders, tables, code blocks, bullet points
- Explain every step of every formula and algorithm
- Use analogies to make abstract concepts concrete
- For programming/CS topics: include annotated code examples
- For math topics: show full working for every example
- NEVER be brief. Verbose is correct. Superficial is wrong.
- If the audio has gaps or is unclear, fill with relevant academic content on the same topic
""".format(max_tokens=MAX_NOTES_TOKENS)


def generate_chunk_notes(audio_file_path: str, chunk_number: int):
    global notes_key_index, notes_model_index
    timestamp = datetime.now().strftime("%I:%M %p")
    print(f"\n[AI Scribe] ☁️  Uploading chunk {chunk_number} ({timestamp})...", flush=True)

    # Uploaded files are scoped to the API key/client that uploaded them, so
    # if we rotate to a different key mid-loop we must re-upload under that
    # key's client — keep track of which client currently owns audio_file
    # so cleanup deletes it from the right place.
    audio_file = None
    audio_file_client = None
    response = None

    attempts = len(GEMINI_API_KEYS) * len(NOTES_MODELS)
    try:
        for _ in range(attempts):
            client = ai_clients[notes_key_index]
            model  = NOTES_MODELS[notes_model_index]

            if audio_file_client is not client:
                # Either the first attempt, or we just rotated keys — this
                # client has never seen this file, so upload it fresh.
                if audio_file is not None:
                    try:
                        audio_file_client.files.delete(name=audio_file.name)
                    except Exception:
                        pass
                audio_file = client.files.upload(file=audio_file_path)
                audio_file_client = client

            print(f"[AI Scribe] 🧠 Analyzing with {model} (key #{notes_key_index + 1}/{len(GEMINI_API_KEYS)})...", flush=True)
            try:
                response = client.models.generate_content(
                    model=model,
                    contents=[audio_file, NOTES_PROMPT],
                    config={"max_output_tokens": MAX_NOTES_TOKENS}
                )
                break
            except Exception as e:
                err = str(e)
                if "429" in err:
                    print(f"⚠️  [{model}] notes quota hit — stepping down...", flush=True)
                    notes_model_index += 1
                elif "not supported" in err.lower() or "multimodal" in err.lower():
                    print(f"⚠️  [{model}] no audio support — stepping down...", flush=True)
                    notes_model_index += 1
                else:
                    print(f"❌ [AI Scribe] Error ({model}) chunk {chunk_number}: {e}", flush=True)
                    response = None
                    break

                if notes_model_index >= len(NOTES_MODELS):
                    notes_model_index = 0
                    notes_key_index = (notes_key_index + 1) % len(GEMINI_API_KEYS)
                    print(f"⚠️  All models exhausted on previous key — switching to Gemini key "
                          f"#{notes_key_index + 1}/{len(GEMINI_API_KEYS)}...", flush=True)

        if response is None:
            print(f"❌ [AI Scribe] All keys/models failed for chunk {chunk_number}.", flush=True)
            return

        with FILE_LOCK:
            with open(NOTES_FILE, "a", encoding="utf-8") as f:
                f.write(f"\n---\n\n# 🕒 Part {chunk_number} — {timestamp}\n\n")
                f.write(response.text.strip() + "\n\n")
        print(f"✅ [AI Scribe] Chunk {chunk_number} saved ({len(response.text)} chars).", flush=True)

    except Exception as e:
        print(f"❌ [AI Scribe] Unexpected error chunk {chunk_number}: {e}", flush=True)
    finally:
        try:
            if audio_file and audio_file_client:
                audio_file_client.files.delete(name=audio_file.name)
        except Exception:
            pass
        if os.path.exists(audio_file_path):
            os.remove(audio_file_path)

# ══════════════════════════════════════════════════════
#  DEEPGRAM STT — dual-language parallel
# ══════════════════════════════════════════════════════
def _stt_call(audio_bytes: bytes, language: str) -> tuple:
    """Single Deepgram call for one language. Returns (transcript, confidence)."""
    result = dg_client.listen.v1.media.transcribe_file(
        request=audio_bytes,
        model="nova-2",
        smart_format=True,
        language=language,
        keywords=[f"{alias}:10" for alias in NAME_ALIASES] + ["present:5", "hazri:5"]
    )
    alt        = result.results.channels[0].alternatives[0]
    transcript = (alt.transcript or "").strip()
    confidence = alt.confidence or 0.0
    return transcript, confidence


def check_avatar_stt(audio_data: np.ndarray):
    global is_responding, last_qa_time

    if not stt_lock.acquire(blocking=False):
        return

    temp_wav = f"/tmp/stt_{uuid.uuid4().hex}.wav"
    try:
        sf.write(temp_wav, audio_data, SAMPLE_RATE)
        with open(temp_wav, "rb") as f:
            audio_bytes = f.read()

        # Run English + Hindi/Urdu in parallel
        results = {}
        def run_lang(lang):
            try:
                results[lang] = _stt_call(audio_bytes, lang)
            except Exception:
                results[lang] = ("", 0.0)

        t_en = threading.Thread(target=run_lang, args=("en",))
        t_hi = threading.Thread(target=run_lang, args=("hi",))
        t_en.start(); t_hi.start()
        t_en.join();  t_hi.join()

        trans_en, conf_en = results.get("en", ("", 0.0))
        trans_hi, conf_hi = results.get("hi", ("", 0.0))

        if conf_hi >= conf_en and trans_hi:
            transcript, detected = trans_hi, "hi/ur"
        elif trans_en:
            transcript, detected = trans_en, "en"
        else:
            transcript, detected = trans_hi or trans_en, "?"

        if transcript:
            print(f"🗣️  [{detected} en={conf_en:.2f} hi={conf_hi:.2f}]: {transcript}", flush=True)
            _append_recent_transcript(transcript)
            lower = transcript.lower()

            if any(alias in lower for alias in NAME_ALIASES):
                cooldown = time.time() - last_qa_time
                if cooldown < 20:
                    print(f"⏳ Name detected — cooling down ({20-cooldown:.0f}s left).", flush=True)
                else:
                    print(f"🚨 NAME DETECTED → responding as {PRIMARY_NAME}...", flush=True)
                    is_responding = True
                    obs_scene("talking")
                    answer = ask_gemini_question(_get_recent_transcript())
                    print(f"🤖 Reply: {answer}", flush=True)
                    last_qa_time = time.time()
                    run_tts_sync(answer)
                    is_responding = False
                    obs_scene("idle")

    except Exception as e:
        print(f"❌ STT Error: {e}", flush=True)
    finally:
        stt_lock.release()
        if os.path.exists(temp_wav):
            os.remove(temp_wav)

# ══════════════════════════════════════════════════════
#  AUDIO CALLBACK
# ══════════════════════════════════════════════════════
def audio_callback(indata, frames, time_info, status):
    if status and "overflow" not in str(status).lower():
        print(f"⚠️  Audio status: {status}", file=sys.stderr, flush=True)
    try:
        audio_queue.put_nowait(indata.copy())
    except queue.Full:
        pass  # Drop frame — prevents memory explosion

# ══════════════════════════════════════════════════════
#  AUDIO DEVICE DETECTION
# ══════════════════════════════════════════════════════
def get_capture_device():
    print("🔍 Searching for Brave browser audio monitor...", flush=True)
    devices = sd.query_devices()

    for i, d in enumerate(devices):
        name = d['name'].lower()
        if d['max_input_channels'] > 0 and 'brave' in name:
            print(f"🎯 Locked on Brave at [{i}] → {d['name']}", flush=True)
            return i

    for i, d in enumerate(devices):
        name = d['name'].lower()
        if d['max_input_channels'] > 0 and 'monitor' in name and 'virtual' not in name:
            print(f"⚠️  Brave not found. Using monitor at [{i}] → {d['name']}", flush=True)
            return i

    print("❌ CRITICAL: No suitable audio capture device found!", flush=True)
    print("   Run: pactl list sources | grep -A3 brave", flush=True)
    return None

# ══════════════════════════════════════════════════════
#  MAIN LOOP
# ══════════════════════════════════════════════════════
def run():
    global silence_count, stt_buffer, notes_buffer, recording

    tts_engine = "ElevenLabs" if ELEVENLABS_API_KEY else "edge-tts (free)"
    print(f"\n🎓 AI Student v3 starting...", flush=True)
    print(f"   TTS      : {tts_engine}", flush=True)
    print(f"   Triggers : {NAME_ALIASES[:5]}{'...' if len(NAME_ALIASES)>5 else ''}", flush=True)
    print(f"   Notes    : {NOTES_FILE}", flush=True)
    obs_scene("idle")

    chunk_counter    = 1
    frames_per_chunk = SAMPLE_RATE * 60 * CHUNK_MINUTES
    SILENCE_BLOCKS   = int((SAMPLE_RATE / BLOCKSIZE) * 1.5)

    device_idx = get_capture_device()
    if device_idx is None:
        sys.exit(1)

    print(f"🎧 Capturing from device [{device_idx}]", flush=True)
    print(f"🛑 Press Ctrl+C to end class and finalize notes.\n", flush=True)

    try:
        with sd.InputStream(
            device=device_idx,
            samplerate=SAMPLE_RATE,
            channels=1,
            blocksize=BLOCKSIZE,
            dtype='float32',
            callback=audio_callback
        ):
            while recording:
                try:
                    data = audio_queue.get(timeout=0.5)
                except queue.Empty:
                    continue

                notes_buffer.append(data)

                # ── STT / Name Detection ─────────────────
                if not is_responding:
                    stt_buffer.append(data)
                    volume = np.linalg.norm(data) * 10

                    if volume < 0.5:
                        silence_count += 1
                    else:
                        silence_count = 0

                    if silence_count > SILENCE_BLOCKS and len(stt_buffer) > 10:
                        chunk = np.concatenate(stt_buffer)
                        if len(chunk) >= SAMPLE_RATE * 0.5:
                            threading.Thread(
                                target=check_avatar_stt,
                                args=(chunk,),
                                daemon=True,
                                name="STT-Worker"
                            ).start()
                        stt_buffer    = []
                        silence_count = 0

                # ── Notes Chunking ────────────────────────
                total_frames = sum(len(c) for c in notes_buffer)
                if total_frames >= frames_per_chunk:
                    print(f"\n[NoteTaker] ⏱️  {CHUNK_MINUTES}min → chunk {chunk_counter}...", flush=True)
                    chunk_audio = np.concatenate(notes_buffer)
                    chunk_file  = f"/tmp/chunk_{chunk_counter}_{uuid.uuid4().hex}.wav"
                    sf.write(chunk_file, chunk_audio, SAMPLE_RATE)
                    threading.Thread(
                        target=generate_chunk_notes,
                        args=(chunk_file, chunk_counter),
                        daemon=True
                    ).start()
                    notes_buffer  = []
                    chunk_counter += 1

    except KeyboardInterrupt:
        recording = False
        print("\n\n🛑 Class ended — finalizing notes...", flush=True)

        if notes_buffer:
            remaining_frames = sum(len(c) for c in notes_buffer)
            if remaining_frames >= SAMPLE_RATE * 30:
                print("[NoteTaker] Processing final audio chunk...", flush=True)
                chunk_audio = np.concatenate(notes_buffer)
                chunk_file  = f"/tmp/chunk_{chunk_counter}_final.wav"
                sf.write(chunk_file, chunk_audio, SAMPLE_RATE)
                t = threading.Thread(
                    target=generate_chunk_notes,
                    args=(chunk_file, chunk_counter),
                    daemon=True
                )
                t.start()
                print("[NoteTaker] Uploading final chunk (max 90s wait)...", flush=True)
                deadline = time.time() + 90
                while t.is_alive() and time.time() < deadline:
                    try:
                        t.join(timeout=2)
                    except KeyboardInterrupt:
                        pass

        print(f"\n✅ Done! Notes saved at:\n   {NOTES_FILE}", flush=True)
        # obs_scene("idle")
        sys.exit(0)


if __name__ == "__main__":
    run()
