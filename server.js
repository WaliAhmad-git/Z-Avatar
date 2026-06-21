/**
 * Virtual Avatar — Node.js Control Server
 * ────────────────────────────────────────
 * Express REST API + WebSocket log streaming.
 * The Python scripts remain the actual workers;
 * this server launches them, streams their output,
 * and serves the UI.
 *
 * npm install express ws
 * node server.js
 */

const express  = require("express");
const http     = require("http");
const path     = require("path");
const fs       = require("fs");
const { execFile, spawn } = require("child_process");
const WebSocket = require("ws");

// ══════════════════════════════════════════════════════
//  CONFIG
// ══════════════════════════════════════════════════════
const PORT        = 3737;
const SCRIPT_DIR  = __dirname;
const CONFIG_FILE = path.join(SCRIPT_DIR, "config.json");
const PYTHON      = "python3";
const AI_STUDENT  = path.join(SCRIPT_DIR, "python", "ai_student.py");
const LAUNCHER    = path.join(SCRIPT_DIR, "python", "launch_core.py");
const YT_NOTES    = path.join(SCRIPT_DIR, "python", "youtube_notes.py");

// ══════════════════════════════════════════════════════
//  HELPERS
// ══════════════════════════════════════════════════════
function readConfig() {
  try {
    const cfg = JSON.parse(fs.readFileSync(CONFIG_FILE, "utf8"));
    return migrateGeminiKeys(cfg);
  } catch {
    return {};
  }
}

// Older configs stored a single "gemini_key" string. The app now supports
// multiple keys (for quota-exhaustion fallback) under "gemini_keys" (array).
// Migrate transparently so existing users don't lose their saved key.
function migrateGeminiKeys(cfg) {
  if (!Array.isArray(cfg.gemini_keys)) {
    cfg.gemini_keys = [];
    if (cfg.gemini_key && !String(cfg.gemini_key).startsWith("YOUR")) {
      cfg.gemini_keys.push(cfg.gemini_key);
    }
  }
  return cfg;
}

function writeConfig(data) {
  fs.writeFileSync(CONFIG_FILE, JSON.stringify(data, null, 2));
}

// ── Running processes ─────────────────────────────────
const activeSessions = {};  // key = session_id, value = { proc, logs: [] }
let sessionCounter = 0;

// ══════════════════════════════════════════════════════
//  EXPRESS APP
// ══════════════════════════════════════════════════════
const app    = express();
const server = http.createServer(app);

app.use(express.json());
app.use(express.static(path.join(SCRIPT_DIR, "public")));

// ── WebSocket server for live log streaming ───────────
const wss = new WebSocket.Server({ server });

function broadcast(sessionId, line, type = "log") {
  const msg = JSON.stringify({ sessionId, type, line, ts: Date.now() });
  wss.clients.forEach(client => {
    if (client.readyState === WebSocket.OPEN) {
      client.send(msg);
    }
  });
}

// ── API: GET /api/config ──────────────────────────────
app.get("/api/config", (req, res) => {
  const cfg = readConfig();
  // Never send secrets to frontend — mask them
  const safe = { ...cfg };
  if (safe.deepgram_key) safe.deepgram_key = "••••" + safe.deepgram_key.slice(-4);
  if (safe.elevenlabs_key && safe.elevenlabs_key.length > 4)
    safe.elevenlabs_key = "••••" + safe.elevenlabs_key.slice(-4);
  // gemini_keys: mask each key individually but keep them as separate array
  // entries so the frontend can render one input row per key.
  safe.gemini_keys = (cfg.gemini_keys || []).map(k =>
    k && k.length > 4 ? "••••" + k.slice(-4) : k
  );
  delete safe.gemini_key; // legacy field — fully replaced by gemini_keys now
  safe._has_deepgram    = !!(cfg.deepgram_key && !cfg.deepgram_key.startsWith("YOUR"));
  safe._has_gemini      = (cfg.gemini_keys || []).length > 0;
  safe._gemini_key_count = (cfg.gemini_keys || []).length;
  safe._has_elevenlabs  = !!(cfg.elevenlabs_key && cfg.elevenlabs_key.length > 8);
  res.json(safe);
});

// ── API: POST /api/config — save keys & preferences ──
app.post("/api/config", (req, res) => {
  const cfg = readConfig();
  const body = req.body || {};

  // gemini_keys is handled separately: it's an array, and a masked entry
  // ("••••xxxx") means "keep the already-saved key that ends in xxxx" —
  // matched by suffix (not array position) so deleting/reordering rows in
  // the UI can't accidentally swap which real key gets kept.
  if (Array.isArray(body.gemini_keys)) {
    const existing = cfg.gemini_keys || [];
    cfg.gemini_keys = body.gemini_keys
      .map(k => {
        if (typeof k === "string" && k.startsWith("••••")) {
          const suffix = k.slice(4);
          return existing.find(realKey => realKey && realKey.endsWith(suffix)) || "";
        }
        return k;
      })
      .map(k => (k || "").trim())
      .filter(k => k.length > 0);
    delete cfg.gemini_key; // fully migrated — don't keep a stale duplicate
  }

  // Only overwrite if actually provided (not masked)
  const fields = [
    "deepgram_key", "elevenlabs_key", "elevenlabs_voice_id",
    "student_name", "trigger_words", "obs_password",
    "last_notes_dir", "last_subject"
  ];
  for (const f of fields) {
    if (body[f] !== undefined && !String(body[f]).startsWith("••••")) {
      cfg[f] = body[f];
    }
  }
  writeConfig(cfg);
  res.json({ ok: true });
});

// ── API: POST /api/validate-keys ─────────────────────
app.post("/api/validate-keys", async (req, res) => {
  // Lightweight ping — just check key format
  const cfg = readConfig();
  const result = {
    deepgram: !!(cfg.deepgram_key && cfg.deepgram_key.length > 10),
    gemini:   (cfg.gemini_keys || []).some(k => k && k.length > 10),
    elevenlabs: !!(cfg.elevenlabs_key && cfg.elevenlabs_key.length > 10),
  };
  res.json(result);
});

// ── API: GET /api/sessions ────────────────────────────
app.get("/api/sessions", (req, res) => {
  const list = Object.entries(activeSessions).map(([id, s]) => ({
    id,
    subject: s.subject,
    type: s.type,
    startTime: s.startTime,
    alive: s.proc && s.proc.exitCode === null,
    logCount: s.logs.length,
    status: s.status || null,
  }));
  res.json(list);
});

// ── API: GET /api/sessions/:id/logs ──────────────────
app.get("/api/sessions/:id/logs", (req, res) => {
  const s = activeSessions[req.params.id];
  if (!s) return res.status(404).json({ error: "Session not found" });
  res.json({ logs: s.logs, status: s.status || null });
});

// ── API: POST /api/launch — start a class session ────
app.post("/api/launch", (req, res) => {
  const cfg  = readConfig();
  const body = req.body || {};

  // Write runtime config that Python reads
  const runtime = {
    deepgram_key:      cfg.deepgram_key  || "",
    gemini_keys:       cfg.gemini_keys   || [],
    elevenlabs_key:    cfg.elevenlabs_key || "",
    elevenlabs_voice_id: cfg.elevenlabs_voice_id || "21m00Tcm4TlvDq8ikWAM",
    student_name:      cfg.student_name  || "Student",
    trigger_words:     cfg.trigger_words || [],
    obs_password:      cfg.obs_password  || "",
    notes_path:        body.notes_path   || "",
    start_time:        body.start_time   || "",
    end_time:          body.end_time     || "",
    subject:           body.subject      || "Class",
    meet_link:         body.meet_link    || "",
  };

  const runtimePath = path.join(SCRIPT_DIR, ".runtime_config.json");
  fs.writeFileSync(runtimePath, JSON.stringify(runtime, null, 2));

  const sessionId = `session_${++sessionCounter}`;
  const proc = spawn(PYTHON, [LAUNCHER,
    "--meet", runtime.meet_link,
    "--start", runtime.start_time,
    "--end",  runtime.end_time,
    "--notes", runtime.notes_path,
    "--subject", runtime.subject,
  ], { cwd: SCRIPT_DIR });

  activeSessions[sessionId] = {
    proc,
    subject: runtime.subject,
    type: "class",
    startTime: Date.now(),
    logs: [],
  };

  function onLine(data, isErr) {
    const lines = data.toString().split("\n");
    lines.forEach(line => {
      if (!line.trim()) return;
      activeSessions[sessionId]?.logs.push(line);

      // launch_core.py emits one machine-readable line summarizing whether
      // the virtual camera/mic/recording actually came up, e.g.:
      // "STATUS: vcam_active=false obs_recording=true audio_ok=true"
      // Parse it so the UI can show a clear badge instead of making the
      // user scroll the log to find out their mic/camera silently failed.
      const statusMatch = line.match(/^STATUS:\s*(.+)$/);
      if (statusMatch && activeSessions[sessionId]) {
        const status = {};
        for (const pair of statusMatch[1].trim().split(/\s+/)) {
          const [key, val] = pair.split("=");
          if (key) status[key] = val === "true";
        }
        activeSessions[sessionId].status = status;
        broadcast(sessionId, JSON.stringify(status), "status");
      }

      broadcast(sessionId, line, isErr ? "err" : "log");
    });
  }

  proc.stdout.on("data", d => onLine(d, false));
  proc.stderr.on("data", d => onLine(d, true));
  proc.on("exit", code => {
    broadcast(sessionId, `\n✅ Process exited (code ${code})`, "system");
    // Keep the dead session visible briefly (so the final log/status badges
    // are still readable) then remove it — otherwise finished sessions pile
    // up forever in the sidebar with no way to clear them.
    setTimeout(() => { delete activeSessions[sessionId]; }, 2 * 60 * 1000);
  });

  res.json({ sessionId });
});

// ── API: POST /api/launch-yt — YouTube notes session ─
app.post("/api/launch-yt", (req, res) => {
  const cfg  = readConfig();
  const body = req.body || {};

  const sessionId = `yt_${++sessionCounter}`;
  const proc = spawn(PYTHON, [YT_NOTES,
    "--subject", body.subject || cfg.last_yt_subject || "Lecture",
    "--notes-dir", body.notes_dir || cfg.last_notes_dir || path.join(process.env.HOME, "Documents/Class_Notes"),
    "--chunk", String(body.chunk_minutes || 10),
  ], { cwd: SCRIPT_DIR,
    // Multiple keys joined by a separator youtube_notes.py won't see in a
    // real API key (commas never appear in Gemini keys), so a simple split
    // is safe and avoids needing JSON-escaping through an env var.
    env: { ...process.env, GEMINI_KEYS: (cfg.gemini_keys || []).join(",") } });

  activeSessions[sessionId] = {
    proc,
    subject: body.subject || "YouTube",
    type: "youtube",
    startTime: Date.now(),
    logs: [],
  };

  proc.stdout.on("data", d => {
    const lines = d.toString().split("\n");
    lines.forEach(line => {
      if (!line.trim()) return;
      activeSessions[sessionId]?.logs.push(line);

      // Capture the notes file path the moment youtube_notes.py announces it,
      // so the frontend can ask us to tail the actual generated note content
      // (the note text itself is written straight to the file, never printed
      // to stdout, so the WebSocket log stream alone can't show it).
      const pathMatch = line.match(/📁 Notes file:\s*(.+)/);
      if (pathMatch && activeSessions[sessionId]) {
        activeSessions[sessionId].notesPath = pathMatch[1].trim();
      }

      broadcast(sessionId, line, "log");
    });
  });
  proc.stderr.on("data", d => broadcast(sessionId, d.toString(), "err"));
  proc.on("exit", code => {
    broadcast(sessionId, `✅ Done (code ${code})`, "system");
    setTimeout(() => { delete activeSessions[sessionId]; }, 2 * 60 * 1000);
  });

  // Save last used settings
  cfg.last_yt_subject = body.subject || cfg.last_yt_subject;
  cfg.last_notes_dir  = body.notes_dir || cfg.last_notes_dir;
  writeConfig(cfg);

  res.json({ sessionId });
});

// ── API: GET /api/sessions/:id/notes-preview — latest chunk text ─
// Reads the session's notes .md file (if known) and returns the most
// recent "## 🕒 Part N" section, for a live preview in the UI.
app.get("/api/sessions/:id/notes-preview", (req, res) => {
  const s = activeSessions[req.params.id];
  if (!s) return res.status(404).json({ error: "Session not found" });
  if (!s.notesPath) return res.json({ path: null, latestChunkText: null });

  try {
    const content = fs.readFileSync(s.notesPath, "utf8");
    const parts = content.split(/\n---\n/).filter(p => p.trim());
    const last = parts.length ? parts[parts.length - 1].trim() : null;
    res.json({ path: s.notesPath, latestChunkText: last });
  } catch (e) {
    res.json({ path: s.notesPath, latestChunkText: null, error: String(e) });
  }
});

// ── API: DELETE /api/sessions/:id — stop a session ───
app.delete("/api/sessions/:id", (req, res) => {
  const s = activeSessions[req.params.id];
  if (!s || !s.proc) return res.status(404).json({ error: "Not found" });
  try {
    s.proc.kill("SIGINT");
    setTimeout(() => {
      if (s.proc.exitCode === null) s.proc.kill("SIGKILL");
    }, 5000);
    res.json({ ok: true });
  } catch (e) {
    res.status(500).json({ error: String(e) });
  }
});

// ── Catch-all → serve index.html ─────────────────────
app.get("/{*path}", (req, res) => {
  res.sendFile(path.join(SCRIPT_DIR, "public", "index.html"));
});

// ══════════════════════════════════════════════════════
//  START
// ══════════════════════════════════════════════════════
server.listen(PORT, "127.0.0.1", () => {
  console.log(`\n🎓 Virtual Avatar UI → http://localhost:${PORT}\n`);
  console.log(`   (bound to 127.0.0.1 only — not reachable from your network)\n`);
});

// Graceful shutdown
process.on("SIGINT", () => {
  console.log("\n🛑 Shutting down server...");
  Object.values(activeSessions).forEach(s => {
    try { s.proc?.kill("SIGINT"); } catch {}
  });
  server.close(() => process.exit(0));
});
