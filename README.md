# telegram-code-runner

Telegram-controlled GitHub Actions Windows VM — now with **full remote desktop** like TeamViewer, directly from Telegram + browser.

## Quick Start

1. Add secret `TELEGRAM_BOT_TOKEN` in repo Settings → Secrets
2. Run workflow **Telegram Runner** via Actions → Run workflow (optionally set Allowed Chat ID)
3. In Telegram, send `/start` to your bot → get full help
4. Send `livestream` → screen sharing **and PhoneFS** start automatically. Telegram receives both public links, the VM username/user ID, and a newly generated PhoneFS password. No ZIP download, BAT setup, or password copying on the VM is needed.

> Set an authorized chat ID when starting the workflow, or save it as the optional repository secret `ALLOWED_CHAT_ID`. The workflow input takes priority. If neither is set, the first chat to start remote access owns it for the rest of that run; other chats are ignored after that. Setting the ID in advance is safer, since shell/file commands are otherwise unrestricted before remote access is claimed.

## Automatic PhoneFS alongside screen sharing

Sending `livestream` (or `live`) starts **two separate listeners and two separate Cloudflare quick tunnels**:

| Service | Local port | Telegram reply |
|---|---|---|
| Interactive screen sharing | `5000` | Screen-sharing `https://…trycloudflare.com` link |
| PhoneFS file manager / command API | `8877`, or the next free port | Its own `https://…trycloudflare.com` link, VM user ID, generated password |

**Login note:** the linked PhoneFS build has **password-only authentication**. The user ID in the reply is the Windows VM account name (usually `runneradmin`), not a separate PhoneFS login account. Open the **PhoneFS URL** and enter the generated password; a username is not required.

- `livestream status` — show both service states, links, and the current PhoneFS credentials again, only to the owning chat.
- `livestream restart` — stop both tunnels and start them again with a fresh PhoneFS password. The screen web server is reused to avoid port conflicts.
- `stop stream` — close both public tunnels and stop PhoneFS and its child `cloudflared` process.
- Repeating `livestream` reuses running services. If PhoneFS failed or was stopped from its browser UI, it starts a fresh PhoneFS session without interrupting a working screen tunnel.
- PhoneFS setup runs in the background. A PhoneFS error is reported in Telegram and does not stop screen sharing. Startup failures are retried, including HTTP/2 fallback if QUIC is blocked.
- A quick tunnel can briefly return `502` while connecting; wait a few seconds and refresh. Both links are temporary and disappear when the workflow ends (maximum six hours).

The workflow pre-downloads the exact [fixed Windows ZIP](https://github.com/User662122/Reddit-user/blob/af3e6a305e6387fc869800709e9264bf2a949d0c/PhoneFS%20fixed%20Windows_Agent%20.zip). `.github/phonefs_service.py` verifies its SHA-256 and uses **`phonefs_win_FIXED.py`** directly, not the setup BAT, which expects a different filename and can fall back to an older download. Downloads and per-session config live under the runner's temporary directory, not in Git. PhoneFS needs no extra Python dependencies; it reuses the workflow's Python and `cloudflared`.

Passwords are generated with `secrets`, configured over stdin (not command-line arguments), and checked with an actual local login before Telegram receives the link. PhoneFS stores a password hash; the bot keeps the plaintext only in memory and does not log it. Neither public tunnel URL is printed in Actions logs. Each new session has its own config, so a saved password cannot silently replace the one sent to Telegram. The ZIP's hard-coded public Filebin URL announcement is disabled; Telegram is the announcement channel. The POSIX-only interactive PTY is disabled on this Windows build; its file manager and `/api/exec`, `/api/python`, and job APIs remain available.

**Security:** PhoneFS gives full file/command access to anyone with its URL and password. The screen-sharing link itself grants desktop control. Keep **both links and credentials private**, use a private authorized Telegram chat, and send `stop stream` when finished. `/stop` also cleans up both services when exiting the bot.

## Features

### 🖥️ Full VM Control (NEW — write anywhere, like local)
- **Type anywhere:** `type Hello World` — types into active window (Notepad, browser, etc).  
  Aliases: `paste <text>`, `typepaste <text>` for long/unicode via clipboard (reliable).
- **Keys & hotkeys:** `press enter`, `press ctrl+c`, `press alt+f4`, `press win+r`, `hotkey ctrl+shift+t`
- **Mouse:** `click 500 300` (coords), `click Save` (UI control name), `rclick 100 200`, `doubleclick 400 500`, `move 800 500`, `drag 100 100 500 500`, `scroll up 500`, `pos`
- **Live Remote Desktop:** `livestream` / `live` → get `https://*.trycloudflare.com` link.  
  Open on phone/PC: **tap to click, drag to select, scroll, type** in the text box, keyboard shortkeys (Ctrl+C/V, Alt+F4, Win+R…). Works like VNC with ~5-8 FPS, auto-reconnects.

### 📁 File Manager (Telegram)
- `pwd`, `ls [path]`, `cat <file>`, `get <file>` (sends file), `mkdir`, `rm`, `mv <src> <dst>`, `cp <src> <dst>`, `write <file> <text>`, `append <file> <text>`, `find <pattern>`, `tree`, `du`, `zip`, `unzip`, `wget <url>`, `cd <path>`
- **Upload:** just send any file/photo to Telegram → saved to VM (`/home/runner/...`)

### 🪟 Window & Apps
- `apps` (list start-menu & desktop), `windows` / `opened apps`, `open notepad` / `open chrome`, `browser https://...`, `focus <window>`, `close <window>` (Alt+F4), `minimize`/`maximize`, `buttons` (list controls in active window), `click <control name>`

### ⚙️ System
- `ps` (top processes), `kill <pid|name>`, `sysinfo` (CPU/RAM/Disk), `env [var]`, `uptime`, `ip`
- Clipboard: `clip get`, `clip set <text>`, `copyclip <text>`
- Interactive stdin: `input <text>` sends to running process (for prompts)
- `input`, `hold <key>` / `release <key>`

### 💻 Shell & Code
- `/<command>` → shell (e.g. `/dir`, `/pip list`), `cmd <command>` / `exec <command>` explicit shell
- Any other text → Python (runs as script)
- `terminate` kills running task

## Examples

```
screen
livestream
type Hello from my phone!
press win+r
type notepad
press enter
type This was typed remotely!
press ctrl+s
click 500 400
pos
ls
get myfile.zip
write notes.txt Hello\nWorld
browser https://google.com
sysinfo
```

## How it works

- GitHub Actions `windows-latest` runner checks out code, installs Python deps (`mss`, `opencv`, `flask`, `pyautogui`, `uiautomation`, `psutil`, `pyperclip`), installs `cloudflared`, then runs `.github/runner.py`.
- Runner polls Telegram `getUpdates`, executes commands, replies, serves Flask on `0.0.0.0:5000` for screen streaming, exposes via its own `cloudflared` tunnel. On `livestream`, it also launches the verified PhoneFS agent on a separate loopback port with an independent tunnel and sends the generated credentials to the owning chat.
- Mouse/keyboard injected via `pyautogui` + `uiautomation`; typing uses clipboard fallback for reliability on Unicode/long text.

## Workflow

`.github/workflows/telegram-runner.yml` installs dependencies, runs the offline regression tests, prepares the fixed PhoneFS agent, and starts `runner.py`. A failed pre-download is retried when you send `livestream`, rather than preventing the bot from starting. Timeout 360 min (6h) per run; re-run workflow to extend.

### Tests

```sh
python -m pip install requests
python -m unittest discover -s tests -v
```

The tests do not need a Telegram token or a public tunnel. They cover the pinned installer, checksum rejection, port fallback, private credential delivery, startup login verification, idempotent starts, fresh passwords on restart, stop/cancellation cleanup, and Telegram command routing. Run the updated workflow on Windows for the final live Telegram/Cloudflare check.

---

> Tip: Use `livestream` for the most **local-like** experience. Open the link, tap where you want to click, type in the toolbar box → instantly appears in VM. Combine with `type` / `press` via Telegram for quick actions without opening the browser.
