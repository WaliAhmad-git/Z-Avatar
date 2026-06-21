# Security Review — Virtual Avatar (local app)

Scope: `server.js`, `config.json` / `.runtime_config.json`, the CDP-driven
browser automation, and the Python workers it spawns. This is a single-user
local tool, so the bar is "safe on your own machine," not "safe on the
internet" — but a few things were worth tightening.

## Fixed in this pass

**1. Server was reachable from your whole network, not just your machine**
`server.listen(PORT, ...)` with no host binds to `0.0.0.0` by default —
anyone on the same Wi-Fi/LAN could open `http://<your-ip>:3737` and see your
masked config, change settings, or trigger `/api/launch` themselves.
→ Fixed: now binds explicitly to `127.0.0.1`.

**2. Real API keys typed into this chat**
Deepgram, Gemini, and the OBS password were pasted directly into our
conversation. Treat any secret that's been typed into a chat (even with an
AI assistant) as exposed — the safest move is to regenerate all three in
their respective dashboards once you're done testing, then drop the new
ones into the Settings tab in the UI. The values are in your `config.json`
now so things will work today, but rotate them when convenient.

**3. No `.gitignore` for secrets**
You push code to GitHub regularly (your portfolio repo, for example). Added
a `.gitignore` excluding `config.json` and `.runtime_config.json` so a
future `git add .` can't accidentally publish live keys. If this project
ever gets its own repo, double check `git status` before your first commit.

## Worth knowing, not changed (by design, for a personal tool)

**Chrome DevTools Protocol (port 9222) has no authentication.** Any process
running as your user on this machine can connect to the debug Brave
instance and fully control it — read page content, inject JS, see whatever
is on screen in that profile. This is inherent to how CDP automation works,
not a bug in this code. Mitigations if it ever bothers you: it's a
dedicated profile (`~/.virtual-avatar-brave-profile`) used only for joining
meetings, so the blast radius is limited to that session, not your main
browser/logins. Chrome's remote debugging only binds to `127.0.0.1` by
default (this code doesn't override that), so it's not exposed to your
network — only to other local processes/users on the same machine.

**`/api/launch` and `/api/launch-yt` spawn Python with array args, not a
shell** (`spawn(PYTHON, [...])`, no `shell: true`), so user-supplied values
like the Meet URL or notes path can't be used for shell injection. They
could still point `notes_path` at an arbitrary writable location on your
filesystem if something untrusted ever reached that endpoint — another
reason the loopback-only binding above matters.

**Passwordless `sudo modprobe`** in `launch_core.py` (`sudo -n modprobe
v4l2loopback ...`) requires you to have set up a sudoers rule. If you did,
make sure that rule is scoped to exactly that one `modprobe` command and
not a blanket `NOPASSWD: ALL` — the script doesn't need broader access and
neither should the sudoers entry.

**Secrets live in plaintext JSON on disk** (`config.json`,
`.runtime_config.json`). Normal for a local single-user tool; just be aware
backups/sync tools (Dropbox, Google Drive, Time Shift snapshots, etc.) will
pick these files up if pointed at this folder.

## Bottom line
Nothing here is a "stop using this" problem for a tool that only runs on
your own laptop for your own classes. The one real gap was the network
exposure, which is now fixed. The rest is "know what you're trusting and
why" rather than active vulnerabilities.
