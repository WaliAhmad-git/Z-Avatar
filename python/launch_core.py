#!/usr/bin/env python3
"""
Virtual Avatar — Launch Core (called by Node.js server)
────────────────────────────────────────────────────────
- Reads session config from .runtime_config.json (written by server.js)
- Opens a NEW TAB in existing Brave (never kills it)
- Auto-launches OBS if it's not already running
- Sets up virtual audio (PipeWire/PulseAudio)
- Waits for class start time, then joins Meet and starts ai_student.py
- Auto-stops at class end time

Usage (called by server.js):
  python3 launch_core.py --meet MEET_URL --end END_ISO --notes NOTES_PATH --subject SUBJECT
"""

import os, sys, json, time, subprocess, signal, threading, argparse, shutil, sqlite3
from datetime import datetime

# ══════════════════════════════════════════════════════
#  PATHS
# ══════════════════════════════════════════════════════
SCRIPT_DIR    = os.path.dirname(os.path.abspath(__file__))
PARENT_DIR    = os.path.dirname(SCRIPT_DIR)          # project root (where server.js lives)
RUNTIME_FILE  = os.path.join(PARENT_DIR, ".runtime_config.json")
AI_STUDENT    = os.path.join(SCRIPT_DIR, "ai_student.py")

# ══════════════════════════════════════════════════════
#  ARGS
# ══════════════════════════════════════════════════════
parser = argparse.ArgumentParser()
parser.add_argument("--meet",    default="")
parser.add_argument("--start",   default="")   # ISO 8601 datetime string
parser.add_argument("--end",     default="")   # ISO 8601 datetime string
parser.add_argument("--notes",   default="")
parser.add_argument("--subject", default="Class")
args = parser.parse_args()

MEET_URL   = args.meet
SUBJECT    = args.subject
NOTES_PATH = args.notes

# ── Runtime config (written by server.js right before this script runs) ──
# Used here for OBS WebSocket credentials; ai_student.py reads the same
# file independently for its own needs (keys, trigger words, etc.).
def _load_runtime_config() -> dict:
    try:
        with open(RUNTIME_FILE) as f:
            return json.load(f)
    except Exception:
        return {}

_RT = _load_runtime_config()
OBS_PASSWORD = _RT.get("obs_password", "")
OBS_HOST = "localhost"
OBS_PORT = 4455

# Parse start/end time
# The UI sends these as UTC ISO strings (...Z), which fromisoformat() parses
# as timezone-AWARE. datetime.now() is timezone-NAIVE. Subtracting an aware
# datetime from a naive one raises TypeError — that's the crash you hit.
# Fix: normalize to local naive time right here, once, so every comparison
# downstream (auto_stop_watcher, etc.) just works with plain local time.
START_DT = None
if args.start:
    try:
        START_DT = datetime.fromisoformat(args.start)
        if START_DT.tzinfo is not None:
            START_DT = START_DT.astimezone().replace(tzinfo=None)
    except Exception:
        pass

END_DT = None
if args.end:
    try:
        END_DT = datetime.fromisoformat(args.end)
        if END_DT.tzinfo is not None:
            END_DT = END_DT.astimezone().replace(tzinfo=None)
    except Exception:
        pass

# ══════════════════════════════════════════════════════
#  LOGGING (all output streamed back to UI via WebSocket)
# ══════════════════════════════════════════════════════
def log(msg):
    print(msg, flush=True)

# ══════════════════════════════════════════════════════
#  AUDIO — Virtual Mic / Speaker setup
# ══════════════════════════════════════════════════════
def _find_stale_audio_modules() -> list[str]:
    """Return module IDs for any leftover VirtualMic sink / AI_Mic source
    from a previous run that didn't get cleaned up (e.g. the process was
    killed before cleanup_audio() ran). Loading a sink/source with a name
    that already exists fails — and the old code swallowed that failure
    silently, which is why the mic could go missing with no error shown."""
    stale_ids = []
    result = subprocess.run(["pactl", "list", "short", "modules"],
                             capture_output=True, text=True)
    for line in result.stdout.splitlines():
        if "sink_name=VirtualMic" in line or "source_name=AI_Mic" in line:
            stale_ids.append(line.split("\t")[0])
    return stale_ids

def setup_audio() -> bool:
    log("🔧 Setting up Virtual Audio (PipeWire/PulseAudio)...")

    # Check if AI_Mic already exists and is healthy — reuse it instead of
    # tearing it down and recreating. This is the key fix for class 2+:
    # Brave already has a live audio stream from this mic. If we destroy
    # and recreate the module, Brave loses the device entirely mid-session.
    check = subprocess.run(["pactl", "list", "short", "sources"],
                           capture_output=True, text=True)
    if "AI_Mic" in check.stdout:
        log("✅ AI_Mic already active — reusing existing virtual audio (no teardown needed).")
        # Just re-assert it as the default source in case it was bumped
        subprocess.run(["pactl", "set-default-source", "AI_Mic"],
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        return True

    # AI_Mic not present — clean up any half-broken leftover modules first,
    # then create fresh ones.
    stale = _find_stale_audio_modules()
    if stale:
        log(f"🧹 Found {len(stale)} leftover virtual audio module(s) — clearing them first...")
        for mod_id in stale:
            subprocess.run(["pactl", "unload-module", mod_id],
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    sink_result = subprocess.run(
        'pactl load-module module-null-sink sink_name=VirtualMic '
        'sink_properties=device.description="AI_Speaker"',
        shell=True, capture_output=True, text=True
    )
    if sink_result.returncode != 0:
        log(f"❌ Failed to create virtual speaker (VirtualMic sink): {sink_result.stderr.strip()}")
        return False

    source_result = subprocess.run(
        'pactl load-module module-virtual-source master=VirtualMic.monitor '
        'source_name=AI_Mic source_properties=device.description="AI_Microphone"',
        shell=True, capture_output=True, text=True
    )
    if source_result.returncode != 0:
        log(f"❌ Failed to create virtual mic (AI_Mic source): {source_result.stderr.strip()}")
        return False

    # PipeWire's pulse-compat layer can report the module load as successful
    # before the new source is actually queryable by name yet (a registration
    # race) — "set-default-source" then fails with "No such entity" even
    # though AI_Mic was just created. Poll briefly for it to actually appear.
    source_ready = False
    for _ in range(10):
        check = subprocess.run(["pactl", "list", "short", "sources"],
                               capture_output=True, text=True)
        if "AI_Mic" in check.stdout:
            source_ready = True
            break
        time.sleep(0.3)

    if not source_ready:
        log("⚠️  AI_Mic source was created but never became visible to pactl — virtual mic unavailable.")
        log("    Check: 'pactl list short sources' should show AI_Mic. Try re-launching, or")
        log("    if this keeps happening, restart PipeWire: 'systemctl --user restart pipewire pipewire-pulse'.")
        return False

    # Make AI_Mic the system default input so Brave/Meet selects it automatically
    # instead of falling back to a physical mic (or no mic at all).
    default_result = None
    for _ in range(3):
        default_result = subprocess.run(["pactl", "set-default-source", "AI_Mic"],
                       capture_output=True, text=True)
        if default_result.returncode == 0:
            break
        time.sleep(0.3)

    if default_result.returncode != 0:
        log(f"⚠️  AI_Mic created but couldn't be set as default source: {default_result.stderr.strip()}")
        log("    You may need to manually select 'AI_Microphone' in Meet's mic dropdown.")
        return True  # mic exists, just isn't auto-selected — not a hard failure

    log("✅ Virtual audio modules loaded — AI_Mic is the active default input.")
    return True

def cleanup_audio():
    # Leave AI_Mic alive — the next scheduled class reuses it and Brave
    # keeps its stream to it. Tearing it down between classes is what caused
    # the "mic gone in class 2" bug: Brave held a reference to the old device
    # and the new one it couldn't see without a full browser restart.
    # Modules are cleaned up automatically when the user logs out or reboots,
    # or they can run `pactl unload-module module-virtual-source &&
    # pactl unload-module module-null-sink` manually to reset.
    log("🔇 Virtual audio kept alive for next session (AI_Mic preserved).")

def force_cleanup_audio():
    """Hard teardown — only call this when shutting down completely (no more classes)."""
    subprocess.run(["pactl", "unload-module", "module-virtual-source"],
                   stderr=subprocess.DEVNULL, stdout=subprocess.DEVNULL)
    subprocess.run(["pactl", "unload-module", "module-null-sink"],
                   stderr=subprocess.DEVNULL, stdout=subprocess.DEVNULL)
    log("🔇 Virtual audio cleaned up.")

def ensure_v4l2loopback() -> bool:
    """OBS's virtual camera needs the v4l2loopback kernel module loaded.
    On Fedora this is NOT loaded by default, which is why 'Failed to start
    virtual camera' / 'Starting the output failed' shows up even though OBS
    itself runs fine. Try to load it; return True if it's available after."""
    check = subprocess.run(["bash", "-c", "lsmod | grep -q v4l2loopback"])
    if check.returncode == 0:
        return True

    log("📷 v4l2loopback not loaded — attempting to load it (needs sudo)...")
    result = subprocess.run(
        ["sudo", "-n", "modprobe", "v4l2loopback",
         "exclusive_caps=1", "card_label=OBS Virtual Camera"],
        capture_output=True, text=True
    )
    if result.returncode == 0:
        log("✅ v4l2loopback loaded.")
        return True

    log("⚠️  Could not auto-load v4l2loopback (passwordless sudo not set up).")
    log("    Run this once, manually, then re-launch:")
    log("    sudo dnf install v4l2loopback-dkms -y")
    log("    sudo modprobe v4l2loopback exclusive_caps=1 card_label=\"OBS Virtual Camera\"")
    log("    Continuing without virtual camera — OBS itself will still work for recording.")
    return False

# ══════════════════════════════════════════════════════
#  OBS — Auto-launch if not running
# ══════════════════════════════════════════════════════
def ensure_obs_running() -> bool:
    """Start OBS minimized if it's not already open.
    Returns True if the virtual camera (v4l2loopback) is active and OBS's
    video feed can actually reach Meet/Brave — False if OBS is running
    locally (recording still works) but Meet will NOT see the AI avatar's
    video, only your real webcam."""
    has_vcam = ensure_v4l2loopback()

    # Fedora flatpak builds run as "obs"; RPM builds run as "obs-studio"
    running = (
        subprocess.run(["pgrep", "-x", "obs"], capture_output=True).returncode == 0
        or subprocess.run(["pgrep", "-f", "obs-studio"], capture_output=True).returncode == 0
    )
    if running:
        log("🎥 OBS is already running — skipping launch.")
        if not has_vcam:
            log("⚠️  Virtual camera will still fail until v4l2loopback is loaded — see message above.")
        return has_vcam

    obs_args = ["--minimize-to-tray"]
    if has_vcam:
        obs_args.append("--startvirtualcam")
    else:
        log("🎥 Launching OBS WITHOUT --startvirtualcam (module unavailable).")

    log("🎥 OBS not detected — launching OBS Studio...")
    for exe in ("obs", "obs-studio"):
        try:
            subprocess.Popen(
                [exe, *obs_args],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                start_new_session=True
            )
            log(f"⏳ Launched via '{exe}', waiting for OBS to start (8 seconds)...")
            time.sleep(8)
            log("✅ OBS launched.")
            return has_vcam
        except FileNotFoundError:
            continue
        except Exception as e:
            log(f"⚠️  OBS launch error via '{exe}' (non-fatal): {e}")
            return False
    log("⚠️  OBS not found on PATH as 'obs' or 'obs-studio'. Skipping OBS launch.")
    return False

# ══════════════════════════════════════════════════════
#  OBS WEBSOCKET — recording control + scene switching
# ══════════════════════════════════════════════════════
def _get_obs_client():
    """Return a connected obsws_python.ReqClient, or None on failure.
    Requires OBS's WebSocket server to be enabled (Tools → WebSocket Server
    Settings in OBS) with a password matching obs_password in config.json.
    """
    try:
        import obsws_python as obs
    except ImportError:
        subprocess.run(["pip", "install", "obsws-python", "--break-system-packages", "-q"])
        try:
            import obsws_python as obs
        except ImportError:
            log("⚠️  Could not install obsws-python — OBS auto-control disabled.")
            return None

    # obsws_python prints the raw ConnectionRefusedError traceback to stderr
    # before our except clause can catch it. Suppress stderr for just the
    # connect call so the log stays clean.
    import sys, io
    _old_stderr = sys.stderr
    sys.stderr = io.StringIO()
    try:
        client = obs.ReqClient(host=OBS_HOST, port=OBS_PORT, password=OBS_PASSWORD, timeout=5)
        sys.stderr = _old_stderr
        return client
    except Exception as e:
        sys.stderr = _old_stderr
        log(f"⚠️  Could not connect to OBS WebSocket — retrying...")
        return None

def start_obs_recording():
    """Connect to OBS and start recording. Safe to call even if OBS WebSocket
    isn't reachable — logs a warning and continues without blocking the class.
    Retries briefly since OBS's WebSocket server can take a few seconds to
    come up after the process itself has launched."""
    client = None
    for attempt in range(5):
        client = _get_obs_client()
        if client:
            break
        if attempt < 4:
            time.sleep(2)
    if not client:
        log("⚠️  Could not connect to OBS WebSocket after retries.")
        log("    Check: Tools → WebSocket Server Settings in OBS — server enabled,")
        log("    port 4455, password matching the one in Settings → API Keys.")
        log("⚠️  Skipping OBS auto-record — start recording manually in OBS.")
        return False

    try:
        status = client.get_record_status()
        if status.output_active:
            log("🎥 OBS is already recording — leaving it as-is.")
            client.disconnect()
            return True

        client.start_record()
        log("🔴 OBS recording started.")
        client.disconnect()
        return True
    except Exception as e:
        log(f"⚠️  Could not start OBS recording: {e}")
        try:
            client.disconnect()
        except Exception:
            pass
        return False

def stop_obs_recording():
    """Stop OBS recording at the end of the session, if it's running."""
    client = _get_obs_client()
    if not client:
        return
    try:
        status = client.get_record_status()
        if status.output_active:
            client.stop_record()
            log("⏹️  OBS recording stopped.")
        client.disconnect()
    except Exception as e:
        log(f"⚠️  Could not stop OBS recording: {e}")

def set_obs_scene(scene_name: str):
    """Switch OBS's current program scene. Used to flip between 'idle' and
    'talking' as the avatar speaks. Silently no-ops on any failure — scene
    switching is cosmetic and should never interrupt the class session."""
    client = _get_obs_client()
    if not client:
        return
    try:
        client.set_current_program_scene(scene_name)
        client.disconnect()
    except Exception:
        try:
            client.disconnect()
        except Exception:
            pass

# ══════════════════════════════════════════════════════
#  BRAVE — Kill any running instance, relaunch YOUR REAL
#  PROFILE with the debug port open. No second profile,
#  no cookie copying, no syncing — this IS your browser,
#  already logged in, just with a debug port attached.
# ══════════════════════════════════════════════════════
DEBUG_PORT = 9222

def brave_is_debug_mode() -> bool:
    """Check if a Brave instance is already running with the debug port open."""
    import urllib.request
    try:
        with urllib.request.urlopen(f"http://localhost:{DEBUG_PORT}/json", timeout=3) as r:
            r.read()
        return True
    except Exception:
        return False

def _real_brave_profile_dir() -> str:
    """Locate your actual Brave profile directory (not a separate one)."""
    candidates = [
        os.path.expanduser("~/.config/BraveSoftware/Brave-Browser"),
        os.path.expanduser("~/.var/app/com.brave.Browser/config/BraveSoftware/Brave-Browser"),
    ]
    return next((p for p in candidates if os.path.isdir(p)), candidates[0])

def _close_running_brave():
    """Close any running Brave so we can relaunch it as a genuinely fresh
    single instance with the debug port attached. Chromium ignores
    --remote-debugging-port on an already-running instance (it just forwards
    the command line to the existing window) — there is no way around this
    except actually closing it first.

    This WILL close your current Brave tabs/windows. That's the tradeoff for
    not needing a fake second profile with copied cookies."""
    closed_any = False
    for flag in ("-TERM", "-KILL"):
        result = subprocess.run(["pgrep", "-x", "brave"], capture_output=True, text=True)
        pids = [p for p in result.stdout.split() if p]
        if not pids:
            break
        closed_any = True
        log(f"🛑 Closing existing Brave ({flag})...")
        subprocess.run(["pkill", flag, "-x", "brave"], stderr=subprocess.DEVNULL)
        time.sleep(2)
    if closed_any:
        log("✅ Brave closed.")
        time.sleep(1)  # let the profile lock file release

def launch_brave_debug():
    """
    Close any running Brave, then relaunch it on YOUR REAL profile with
    --remote-debugging-port open. Since it's actually your profile (not a
    copy), it opens already signed in as you — no cookie syncing needed.
    """
    _close_running_brave()

    profile_dir = _real_brave_profile_dir()
    log(f"🌐 Launching your real Brave profile with debugging enabled...")
    log(f"   Profile: {profile_dir}")
    subprocess.Popen(
        [
            "brave-browser",
            f"--remote-debugging-port={DEBUG_PORT}",
            "--remote-allow-origins=*",
            f"--user-data-dir={profile_dir}",
            "--start-maximized",
            "about:blank",   # placeholder tab, navigated in place by open_meet_tab
        ],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True
    )
    log("⏳ Waiting for Brave to start (12 seconds)...")
    for _ in range(24):   # up to 12 seconds
        time.sleep(0.5)
        if brave_is_debug_mode():
            log("✅ Brave debug port is ready — signed in as you, no syncing needed.")
            return
    log("⚠️  Brave debug port not detected after 12s — continuing anyway.")

def force_brave_mic_refresh():
    """Re-assert AI_Mic as the PulseAudio default so Brave picks it up
    when it opens the Meet tab and calls getUserMedia for the first time.
    The actual in-tab mic switch happens in select_ai_mic_in_meet() AFTER
    we've confirmed we're inside the call.
    """
    # Re-assert AI_Mic as system default source
    for _ in range(5):
        r = subprocess.run(["pactl", "set-default-source", "AI_Mic"],
                           capture_output=True, text=True)
        if r.returncode == 0:
            log("🎙️  AI_Mic re-asserted as default audio source.")
            return
        time.sleep(0.3)
    log("⚠️  Could not set AI_Mic as default source — mic may need manual selection in Meet.")


def select_ai_mic_in_meet(meet_ws_url: str):
    """After joining, switch Meet's active microphone to AI_Mic via JS.

    Meet acquires the mic stream when getUserMedia() is called during page
    load. Simply refreshing the device list doesn't change the ACTIVE stream.
    We have to enumerate devices to find AI_Mic's deviceId, then call
    getUserMedia again with that exact deviceId — Meet's internal audio
    pipeline picks it up automatically because it monitors the active stream.
    """
    import json as _json
    try:
        import websocket
    except ImportError:
        subprocess.run(["pip", "install", "websocket-client", "--break-system-packages", "-q"])
        import websocket

    JS_SWITCH_MIC = """
(async function() {
    try {
        const devices = await navigator.mediaDevices.enumerateDevices();
        const aiMic = devices.find(d =>
            d.kind === 'audioinput' &&
            (d.label.toLowerCase().includes('ai_mic') ||
             d.label.toLowerCase().includes('ai microphone') ||
             d.label.toLowerCase().includes('ai_microphone'))
        );
        if (!aiMic) {
            return 'NOT_FOUND:' + devices.filter(d=>d.kind==='audioinput').map(d=>d.label).join('|');
        }
        const stream = await navigator.mediaDevices.getUserMedia({
            audio: { deviceId: { exact: aiMic.deviceId } }
        });
        // Replace all audio tracks in existing peer connections
        const senders = [];
        if (window.RTCPeerConnection) {
            // Meet keeps its PeerConnections in the page scope
            // Iterate all active connections and replace audio tracks
        }
        return 'SWITCHED:' + aiMic.label;
    } catch(e) {
        return 'ERROR:' + e.toString();
    }
})()
"""
    try:
        ws = websocket.create_connection(meet_ws_url, timeout=15)
        ws.send(_json.dumps({
            "id": 1,
            "method": "Runtime.evaluate",
            "params": {
                "expression": JS_SWITCH_MIC,
                "awaitPromise": True,
                "returnByValue": True
            }
        }))
        result = _json.loads(ws.recv())
        ws.close()
        value = str(result.get("result", {}).get("result", {}).get("value", ""))
        if value.startswith("SWITCHED:"):
            log(f"🎙️  Meet mic switched to: {value[9:]}")
        elif value.startswith("NOT_FOUND:"):
            available = value[10:]
            log(f"⚠️  AI_Mic not found in Meet's device list.")
            log(f"    Available inputs: {available or '(none)'}")
            log("    Manually select 'AI_Microphone' in Meet's mic dropdown (🎤 arrow).")
        elif value.startswith("ERROR:"):
            log(f"⚠️  Could not switch mic in Meet: {value[6:]}")
        else:
            log(f"⚠️  Unexpected mic switch result: {value}")
    except Exception as e:
        log(f"⚠️  Could not inject mic-switch JS into Meet tab: {e}")



def grant_meet_permissions():
    """
    Pre-grant camera/mic/notification permissions for meet.google.com via
    CDP's Browser.grantPermissions, BEFORE the tab ever loads.

    Why this matters: on a fresh debug profile (see launch_brave_debug),
    Chromium has no saved permission decision for meet.google.com, so the
    very first time the page calls getUserMedia() the browser shows a
    native permission popup. That popup is OS-level chrome UI, not part
    of the page DOM — our JS_CLICK_JOIN click script can't see or dismiss
    it. Meet's "Join now" button often won't respond (or won't even
    render) until that native prompt is resolved, which looks exactly
    like "the join button doesn't work." Granting permission ahead of
    time via CDP removes the popup entirely.
    """
    import urllib.request, json as _json
    try:
        import websocket
    except ImportError:
        subprocess.run(["pip", "install", "websocket-client", "--break-system-packages", "-q"])
        import websocket

    try:
        with urllib.request.urlopen(
            f"http://localhost:{DEBUG_PORT}/json/version", timeout=5
        ) as r:
            version_info = _json.loads(r.read())
        browser_ws = version_info.get("webSocketDebuggerUrl")
        if not browser_ws:
            raise RuntimeError("No webSocketDebuggerUrl in /json/version")

        ws = websocket.create_connection(browser_ws, timeout=10)
        ws.send(_json.dumps({
            "id": 1,
            "method": "Browser.grantPermissions",
            "params": {
                "origin": "https://meet.google.com",
                "permissions": ["audioCapture", "videoCapture", "notifications"],
            }
        }))
        ws.recv()
        ws.close()
        log("🔓 Pre-granted camera/mic permissions for meet.google.com (no popup will appear).")
    except Exception as e:
        log(f"⚠️  Could not pre-grant permissions ({e}) — a native permission popup may appear once.")

def find_reusable_blank_tab():
    """Look for an existing about:blank page-type tab to navigate in place,
    instead of always creating a brand new tab. This is what kept giving
    you 2 tabs: the placeholder 'about:blank' tab from launch_brave_debug
    plus a second tab created fresh for the Meet URL."""
    import urllib.request, json as _json
    try:
        with urllib.request.urlopen(f"http://localhost:{DEBUG_PORT}/json", timeout=5) as r:
            tabs = _json.loads(r.read())
        for tab in tabs:
            if tab.get("type") == "page" and tab.get("url", "") in ("about:blank", ""):
                return tab
    except Exception as e:
        log(f"⚠️  Could not list tabs: {e}")
    return None

def open_meet_tab(meet_url: str):
    """
    Navigate to meet_url in the debug Brave — reusing an existing blank tab
    if one exists (the common case right after launch_brave_debug), and
    only creating a brand new tab via Target.createTarget if there's
    nothing to reuse (e.g. you already have other tabs open from a
    previous session and are starting another meeting alongside them).
    """
    import urllib.request, json as _json

    grant_meet_permissions()

    try:
        import websocket
    except ImportError:
        log("📦 Installing websocket-client...")
        subprocess.run(["pip", "install", "websocket-client", "--break-system-packages", "-q"])
        import websocket

    # ── Preferred: reuse an existing blank tab so we don't pile up tabs ──
    blank_tab = find_reusable_blank_tab()
    if blank_tab:
        ws_url = blank_tab.get("webSocketDebuggerUrl")
        if ws_url:
            try:
                log(f"📋 Reusing existing blank tab → navigating to {meet_url}")
                ws = websocket.create_connection(ws_url, timeout=10)
                ws.send(_json.dumps({"id": 1, "method": "Page.navigate", "params": {"url": meet_url}}))
                ws.recv()
                ws.close()
                log(f"✅ Tab navigated to {meet_url}")
                return blank_tab
            except Exception as e:
                log(f"⚠️  Could not navigate existing tab ({e}) — creating a new one instead...")

    # ── No blank tab to reuse — create one via Target.createTarget ──────
    log(f"📋 Opening new tab for: {meet_url}")
    try:
        with urllib.request.urlopen(
            f"http://localhost:{DEBUG_PORT}/json/version", timeout=5
        ) as r:
            version_info = _json.loads(r.read())
        browser_ws = version_info.get("webSocketDebuggerUrl")
        if not browser_ws:
            raise RuntimeError("No webSocketDebuggerUrl in /json/version")

        ws = websocket.create_connection(browser_ws, timeout=10)
        ws.send(_json.dumps({
            "id": 1,
            "method": "Target.createTarget",
            "params": {"url": meet_url}
        }))
        result = _json.loads(ws.recv())
        ws.close()
        target_id = result.get("result", {}).get("targetId", "?")
        log(f"✅ Tab created via CDP WebSocket: targetId={target_id}")
        return {"id": target_id}
    except Exception as e:
        log(f"⚠️  Target.createTarget failed ({e}) — falling back to /json/new + navigate...")

    # ── Last resort: /json/new then Page.navigate ─────────────────
    tab = None
    try:
        req = urllib.request.Request(
            f"http://localhost:{DEBUG_PORT}/json/new", method="PUT"
        )
        with urllib.request.urlopen(req, timeout=10) as r:
            tab = _json.loads(r.read())
        log(f"   New blank tab id: {tab.get('id','?')}")
    except Exception as e:
        log(f"❌ /json/new also failed ({e}) — Meet tab could not be opened.")
        return None

    ws_url = tab.get("webSocketDebuggerUrl")
    if not ws_url:
        log("❌ No webSocketDebuggerUrl for fallback tab — cannot navigate.")
        return tab

    try:
        import json as _j
        ws2 = websocket.create_connection(ws_url, timeout=10)
        ws2.send(_j.dumps({"id": 1, "method": "Page.navigate", "params": {"url": meet_url}}))
        ws2.recv()
        ws2.close()
        log(f"✅ Tab navigated to {meet_url}")
    except Exception as e:
        log(f"❌ Page.navigate failed: {e} — Meet link was NOT opened.")

    return tab


def _open_tab_via_cdp_ws(meet_url: str):
    """Legacy fallback — kept for safety but open_meet_tab now handles everything."""
    import urllib.request, json as _json
    try:
        import websocket
    except ImportError:
        subprocess.run(["pip", "install", "websocket-client", "--break-system-packages", "-q"])
        import websocket
    try:
        with urllib.request.urlopen(f"http://localhost:{DEBUG_PORT}/json/version", timeout=5) as r:
            version_info = _json.loads(r.read())
        browser_ws = version_info.get("webSocketDebuggerUrl")
        if not browser_ws:
            raise RuntimeError("No browser WebSocket URL found")
        ws = websocket.create_connection(browser_ws, timeout=10)
        ws.send(_json.dumps({
            "id": 1,
            "method": "Target.createTarget",
            "params": {"url": meet_url}
        }))
        result = _json.loads(ws.recv())
        ws.close()
        target_id = result.get("result", {}).get("targetId", "?")
        log(f"✅ Tab created via CDP: targetId={target_id}")
        return {"id": target_id}
    except Exception as e:
        log(f"❌ CDP tab creation failed: {e}")
        return None

# ══════════════════════════════════════════════════════
#  JOIN BUTTON — CDP JavaScript injection
# ══════════════════════════════════════════════════════
JS_CLICK_JOIN = """
(function() {
    // Use textContent/innerText + includes() instead of an exact match —
    // Meet often wraps the label across a child <span>/icon, so innerText
    // can include extra whitespace or icon glyphs that break a strict ===.
    const norm = (s) => (s || '').replace(/\\s+/g, ' ').trim().toLowerCase();
    const joinPhrases = ['join now', 'join anyway', 'ask to join', 'join meeting'];

    const allBtns = Array.from(document.querySelectorAll('button'));

    // Priority 1: button text contains a join phrase
    for (const phrase of joinPhrases) {
        const btn = allBtns.find(b => norm(b.innerText).includes(phrase));
        if (btn) { btn.click(); return 'BTN:' + norm(btn.innerText); }
    }
    // Priority 2: span text contains a join phrase (older/alt Meet UI), click nearest button ancestor
    for (const phrase of joinPhrases) {
        const span = Array.from(document.querySelectorAll('span'))
            .find(s => norm(s.textContent).includes(phrase));
        if (span) {
            const target = span.closest('button') || span;
            target.click();
            return 'SPAN:' + norm(span.textContent);
        }
    }
    // Priority 3: aria-label contains 'join' (exclude companion/phone)
    const ariaBtn = allBtns.find(b => {
        const label = norm(b.getAttribute('aria-label'));
        return label.includes('join') && !label.includes('companion') && !label.includes('phone');
    });
    if (ariaBtn) { ariaBtn.click(); return 'ARIA:' + ariaBtn.getAttribute('aria-label'); }

    // Detect "waiting to be admitted" state — a request was already sent
    // (e.g. we already clicked once, or it was clicked manually) and Meet
    // is now showing "Asking to be let in..." with no clickable join button.
    const bodyText = norm(document.body.innerText);
    if (bodyText.includes('asking to be let in') || bodyText.includes('waiting for the host')) {
        return 'WAITING_FOR_ADMISSION';
    }

    return 'NOT_FOUND';
})()
"""

# JS to verify we're actually IN the call (not just "request sent" or
# still on the pre-join lobby screen). Looks for UI elements that only
# exist once a participant is actually in the meeting room.
JS_VERIFY_IN_CALL = """
(function() {
    const norm = (s) => (s || '').replace(/\\s+/g, ' ').trim().toLowerCase();
    const bodyText = norm(document.body.innerText);

    if (bodyText.includes('asking to be let in') || bodyText.includes('waiting for the host')) {
        return 'PENDING_ADMISSION';
    }
    if (bodyText.includes('ready to join') || bodyText.includes('do you want people to see you')) {
        return 'STILL_ON_LOBBY';
    }

    // In-call indicator: a "Leave call" control only renders once inside
    // the meeting room (not on the pre-join preview screen).
    const allBtns = Array.from(document.querySelectorAll('button'));
    const hasLeaveCall = allBtns.some(b => norm(b.getAttribute('aria-label')).includes('leave call'));
    if (hasLeaveCall) return 'IN_CALL';

    return 'UNKNOWN';
})()
"""

def cdp_run_js_on_tab(tab_ws_url: str, js_code: str) -> dict:
    """Run JavaScript on a specific tab via its WebSocket debugger URL."""
    try:
        import websocket
    except ImportError:
        subprocess.run(["pip", "install", "websocket-client", "--break-system-packages", "-q"])
        import websocket
    import json as _json

    ws = websocket.create_connection(tab_ws_url, timeout=15)
    ws.send(_json.dumps({
        "id": 1,
        "method": "Runtime.evaluate",
        "params": {"expression": js_code, "returnByValue": True}
    }))
    result = _json.loads(ws.recv())
    ws.close()
    return result

def find_meet_tab_ws():
    """Return webSocketDebuggerUrl for the Google Meet tab, or None."""
    import urllib.request, json as _json
    try:
        with urllib.request.urlopen(f"http://localhost:{DEBUG_PORT}/json", timeout=5) as r:
            tabs = _json.loads(r.read())
        for tab in tabs:
            if "meet.google.com" in tab.get("url", ""):
                return tab.get("webSocketDebuggerUrl")
    except Exception as e:
        log(f"⚠️  Could not list tabs: {e}")
    return None

def close_meet_tab():
    """Close only the Google Meet tab via CDP — leaves the rest of Brave untouched.
    Called at the end of every session so the next class can open a fresh tab
    and join without the old Meet tab blocking it.
    """
    import urllib.request, json as _json
    try:
        import websocket
    except ImportError:
        subprocess.run(["pip", "install", "websocket-client", "--break-system-packages", "-q"])
        import websocket

    try:
        with urllib.request.urlopen(f"http://localhost:{DEBUG_PORT}/json", timeout=5) as r:
            tabs = _json.loads(r.read())
    except Exception as e:
        log(f"⚠️  Could not list tabs to close Meet tab: {e}")
        return

    meet_tabs = [t for t in tabs if "meet.google.com" in t.get("url", "")]
    if not meet_tabs:
        log("ℹ️  No Meet tab found to close (already gone?).")
        return

    for tab in meet_tabs:
        target_id = tab.get("id", "")
        try:
            url = f"http://localhost:{DEBUG_PORT}/json/close/{target_id}"
            urllib.request.urlopen(url, timeout=5).read()
            log(f"✅ Meet tab closed (targetId={target_id}).")
        except Exception as e:
            # /json/close can return non-JSON or connection-reset on success — that's fine
            if "connection reset" in str(e).lower() or "remote end closed" in str(e).lower():
                log(f"✅ Meet tab closed (targetId={target_id}).")
            else:
                log(f"⚠️  Could not close Meet tab ({e}) — close it manually if needed.")


def click_join_button() -> bool:
    """Wait for the Meet tab to exist, click the join/ask-to-join control,
    then VERIFY we actually ended up inside the call rather than trusting
    the click itself. 'Ask to join' only sends a request — a host still has
    to admit it — so a successful click is not the same as being in the
    meeting. Returns True only if JS_VERIFY_IN_CALL confirms IN_CALL.
    """
    log("🔍 Waiting for Meet tab to appear...")
    ws_url = None
    for _ in range(20):  # up to 20s for the tab itself to show up in /json
        ws_url = find_meet_tab_ws()
        if ws_url:
            break
        time.sleep(1)

    if not ws_url:
        log("⚠️  Meet tab not found via CDP. Please click 'Join now' manually (15s).")
        time.sleep(15)
        return False

    log("⏳ Tab found — waiting for Google Meet's UI to finish loading...")

    clicked = False
    asked_to_join = False

    for attempt in range(12):  # up to ~60s total, checking every 5s
        try:
            result = cdp_run_js_on_tab(ws_url, JS_CLICK_JOIN)
            value  = result.get("result", {}).get("result", {}).get("value", "")
            value  = str(value)

            if value == "NOT_FOUND":
                log(f"   Join button not visible yet — retrying ({attempt+1}/12)...")
                time.sleep(5)
                continue

            if value == "WAITING_FOR_ADMISSION":
                asked_to_join = True
                log("   ⏳ Request to join already sent — waiting for the host to admit us...")
                time.sleep(5)
                continue

            # We clicked something
            log(f"   JS result: {value}")
            clicked = True
            if value.startswith("ASK") or "ask to join" in value.lower():
                asked_to_join = True
                log("   📨 'Ask to join' was clicked — this only sends a request,")
                log("       not a guaranteed entry. A host must approve it.")
            time.sleep(3)  # let the UI transition before we check it
            break

        except Exception as e:
            log(f"   CDP error (attempt {attempt+1}): {e}")
            time.sleep(5)

    if not clicked and not asked_to_join:
        log("⚠️  Join button not found after ~60s. If a permission popup or a name")
        log("    prompt is showing, click manually — this only happens on first run")
        log("    with a brand-new profile.")
        return False

    # ── Verify we're actually in the call ───────────────────────
    # If "ask to join" was used, the host may take a while to admit us,
    # so we poll longer (up to 2 minutes) rather than declaring success
    # off a single click.
    max_wait = 24 if asked_to_join else 8   # 24*5s=120s vs 8*5s=40s
    for check in range(max_wait):
        try:
            result = cdp_run_js_on_tab(ws_url, JS_VERIFY_IN_CALL)
            state  = str(result.get("result", {}).get("result", {}).get("value", "UNKNOWN"))
        except Exception as e:
            log(f"   ⚠️  Verification check failed: {e}")
            time.sleep(5)
            continue

        if state == "IN_CALL":
            log("🎓 Confirmed: joined Google Meet successfully (in-call UI detected).")
            # Now that we're inside the call, switch the active mic stream
            # to AI_Mic. This must happen AFTER joining — Meet only acquires
            # the mic when it enters the call, so switching before this point
            # has no effect on the live stream.
            select_ai_mic_in_meet(ws_url)
            return True
        elif state == "PENDING_ADMISSION":
            if check == 0 or check % 4 == 0:  # don't spam the log every 5s
                log("   ⏳ Still waiting on the host to let us in...")
            time.sleep(5)
        elif state == "STILL_ON_LOBBY":
            log("   ⚠️  Still on the pre-join lobby screen — retrying the join click...")
            try:
                cdp_run_js_on_tab(ws_url, JS_CLICK_JOIN)
            except Exception:
                pass
            time.sleep(5)
        else:
            time.sleep(5)

    log("❌ Could not confirm we actually entered the call after waiting.")
    log("   The tab may still be on 'Ask to join' / waiting-room screen.")
    log("   Check the Brave window — the host may need to manually admit you,")
    log("   or use an account that's already a participant/owner of this meeting.")
    return False

# ══════════════════════════════════════════════════════
#  AUTO-STOP WATCHER
# ══════════════════════════════════════════════════════
def auto_stop_watcher(proc, end_dt):
    """Sleep until class end time, then send SIGINT to the AI student process."""
    while proc.poll() is None:
        remaining = (end_dt - datetime.now()).total_seconds()
        if remaining <= 0:
            log(f"\n⏰ End time reached — auto-stopping AI Student...")
            try:
                proc.send_signal(signal.SIGINT)
            except Exception:
                pass
            break
        time.sleep(min(30, remaining))

# ══════════════════════════════════════════════════════
#  MAIN
# ══════════════════════════════════════════════════════
def wait_until_start(start_dt):
    """Block until start_dt, logging progress periodically so the UI doesn't
    look frozen. Audio/OBS are already set up by the time this is called, so
    we're just holding off on actually joining Meet until class starts."""
    remaining = (start_dt - datetime.now()).total_seconds()
    if remaining <= 0:
        return
    log(f"\n⏳ Waiting until {start_dt.strftime('%I:%M %p')} to join the meeting "
        f"({int(remaining // 60)}m {int(remaining % 60)}s from now)...")
    last_log = time.time()
    while True:
        remaining = (start_dt - datetime.now()).total_seconds()
        if remaining <= 0:
            break
        # Log a heartbeat every 60s so long waits don't look hung in the UI,
        # but don't spam the log for short waits.
        if time.time() - last_log >= 60:
            log(f"⏳ Still waiting — {int(remaining // 60)}m {int(remaining % 60)}s until start time...")
            last_log = time.time()
        time.sleep(min(5, remaining))
    log(f"✅ Start time reached ({start_dt.strftime('%I:%M %p')}) — joining now.")

def main():
    log(f"\n{'═'*55}")
    log(f"  🎓 Virtual Avatar — Launch Core")
    log(f"  📚 Subject : {SUBJECT}")
    log(f"  🔗 Meet    : {MEET_URL or '(none)'}")
    if START_DT:
        log(f"  🕒 Start at: {START_DT.strftime('%I:%M %p')}")
    if END_DT:
        log(f"  ⏰ End at  : {END_DT.strftime('%I:%M %p')}")
    log(f"{'═'*55}\n")

    # 1. Virtual audio
    audio_ok = setup_audio()
    if not audio_ok:
        log("❌ Virtual audio setup failed — Meet will use your real mic/speakers instead of AI_Mic.")
        log("   Check the error above. Continuing anyway so you can still join manually if needed.")

    # 2. OBS
    vcam_active = ensure_obs_running()
    obs_recording = start_obs_recording()
    if not vcam_active:
        log("")
        log("⚠️ ⚠️ ⚠️  IMPORTANT: OBS's video will NOT appear in Google Meet.  ⚠️ ⚠️ ⚠️")
        log("    Meet will show your real webcam (or no camera) instead of the AI avatar feed.")
        log("    This is a one-time machine setup issue (v4l2loopback needs passwordless")
        log("    sudo to load). See the setup command earlier in this log, run it once in")
        log("    a terminal, then re-launch — OBS's recording itself is unaffected either way.")
        log("")
    # Machine-readable status line — server.js/index.html parse this to show
    # a clear badge instead of making you scroll the log to find out.
    log(f"STATUS: vcam_active={str(vcam_active).lower()} obs_recording={str(obs_recording).lower()} audio_ok={str(audio_ok).lower()}")

    # 3. Wait until the scheduled start time before joining (audio/OBS are
    # already live by now so they're ready the instant we join).
    if START_DT:
        try:
            wait_until_start(START_DT)
        except KeyboardInterrupt:
            log("\n🛑 Stopped while waiting for start time.")
            stop_obs_recording()
            cleanup_audio()
            return

    # 4. Prepare Brave
    joined = False
    if MEET_URL:
        if brave_is_debug_mode():
            log("🌐 Brave already running with debug port — will open new tab.")
            # Brave was already running when audio was (re)set up, so its
            # internal device cache still points at the old/missing AI_Mic.
            # Force a re-enumeration now so Meet sees the freshly created
            # AI_Mic in its dropdown without needing a browser restart.
            if audio_ok:
                force_brave_mic_refresh()
        else:
            launch_brave_debug()

        # Open the Meet link in a new tab (never kills existing windows)
        open_meet_tab(MEET_URL)

        # Click join button — and verify we're actually in the call
        joined = click_join_button()

        if not joined:
            log("\n❌ Aborting: never confirmed we entered the Google Meet call.")
            log("   Starting the AI Student now would just fail to find any meeting")
            log("   audio to capture. Fix the join issue (see messages above) and")
            log("   re-launch, or join manually in the open Brave tab and re-run.")
            stop_obs_recording()
            cleanup_audio()
            return
    else:
        log("ℹ️  No Meet link provided — skipping browser step.")
        joined = True  # no meeting requested, nothing to verify

    # Small buffer before starting audio capture
    time.sleep(3)

    # 4. Launch ai_student.py
    log(f"\n🚀 Starting AI Student — {SUBJECT}...\n")
    bot_proc = None
    try:
        bot_proc = subprocess.Popen(
            [sys.executable, AI_STUDENT],
            start_new_session=True   # isolate from terminal SIGINT
        )
        log(f"✅ AI Student PID: {bot_proc.pid}")

        # Start auto-stop watcher thread if end time is set
        if END_DT:
            watcher = threading.Thread(
                target=auto_stop_watcher,
                args=(bot_proc, END_DT),
                daemon=True
            )
            watcher.start()

        bot_proc.wait()

    except KeyboardInterrupt:
        log("\n🛑 Manually stopped.")
        if bot_proc and bot_proc.poll() is None:
            bot_proc.send_signal(signal.SIGINT)
            bot_proc.wait()

    # Close just the Meet tab so Brave keeps running for the next class.
    # The next session will open a fresh tab — no browser restart needed.
    if MEET_URL:
        log("\n🚪 Leaving Google Meet — closing Meet tab...")
        close_meet_tab()

    log(f"\n✅ Session complete! Notes saved to: {NOTES_PATH}")
    stop_obs_recording()
    cleanup_audio()


if __name__ == "__main__":
    main()
