"""
local_livestream.py — the "livestream" experience from the telegram-code-runner
GitHub Actions workflow, but running on YOUR PC.

Exactly like on the GitHub Windows runner it opens TWO independent Cloudflare
quick tunnels and sends everything to your Telegram chat:

  1. Screen sharing (Flask + mss remote desktop on local port 5000)
     -> https://....trycloudflare.com   (tap to click, type, drag, scroll)
  2. PhoneFS file manager / command API on local port 8877 (or next free)
     -> its own https://....trycloudflare.com + generated password

Usage (Windows recommended, PhoneFS agent is a Windows build):

  set TELEGRAM_BOT_TOKEN=***            (cmd)
  set TELEGRAM_CHAT_ID=123456789        (optional; if missing, send any
                                         message to your bot within 3 min
                                         after start - that chat owns it)
  python local_livestream.py

Dependencies:
  pip install flask mss numpy opencv-python pyautogui pyperclip

cloudflared is downloaded automatically if not found (same URL as the
workflow). The pinned PhoneFS ZIP is downloaded, SHA-256 verified and cached
under %TEMP%\\local-livestream\\phonefs (never in your project folder).

SECURITY: both public links grant full control of this PC to anyone who has
them (desktop control + files/commands via PhoneFS). Keep the Telegram chat
private and stop the script when done.
"""

import getpass
import hashlib
import io
import json
import os
import platform
import queue
import re
import secrets
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import zipfile
from collections import deque
from pathlib import Path, PurePosixPath

# ----------------------------------------------------------------------------
# Configuration (same values the GitHub workflow uses)
# ----------------------------------------------------------------------------

TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
BASE = f"https://api.telegram.org/bot{TOKEN}" if TOKEN else ""
ALLOWED_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID") or os.environ.get("ALLOWED_CHAT_ID")
if ALLOWED_CHAT_ID:
    try:
        ALLOWED_CHAT_ID = int(ALLOWED_CHAT_ID)
    except ValueError:
        ALLOWED_CHAT_ID = None

SCREEN_PORT = 5000                       # same as the runner

PACKAGE_REF = "af3e6a305e6387fc869800709e9264bf2a949d0c"
PACKAGE_PATH = "PhoneFS%20fixed%20Windows_Agent%20.zip"
PACKAGE_URL = (
    f"https://raw.githubusercontent.com/User662122/Reddit-user/{PACKAGE_REF}/{PACKAGE_PATH}"
)
PACKAGE_API_URL = (
    "https://api.github.com/repos/User662122/Reddit-user/contents/"
    f"{PACKAGE_PATH}?ref={PACKAGE_REF}"
)
PACKAGE_SHA256 = "3de33e5ce3ab75b40068e367d816a08d9e4fa5c81bc67ee003c4e684174c40fd"
AGENT_NAME = "phonefs_win_FIXED.py"
PREFERRED_PORTS = (8877, 8878, 8879, 8890, 8891, 8892)
URL_RE = re.compile(r"https://[a-z0-9][a-z0-9-]{0,62}\.trycloudflare\.com", re.I)
ANSI_RE = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]")
# Disable the ZIP's hard-coded public Filebin announcement (same as the repo).
MAILBOX_START = "    threading.Thread(target=_announce, daemon=True).start()"
MAILBOX_DISABLED = "    # URL announcements are handled privately by Telegram."

# ----------------------------------------------------------------------------
# Small helpers (ported from .github/phonefs_service.py and runner.py)
# ----------------------------------------------------------------------------

def http_get(url, timeout=60, headers=None):
    request = urllib.request.Request(url, headers=headers or {"User-Agent": "local-livestream"})
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return response.read()


def api_get(method, params=None, timeout=40):
    url = BASE + "/" + method
    if params:
        url += "?" + urllib.parse.urlencode(params)
    return json.loads(http_get(url, timeout=timeout).decode("utf-8"))


def send_message(chat_id, text):
    try:
        payload = json.dumps({"chat_id": chat_id, "text": str(text)[:4000]}).encode()
        req = urllib.request.Request(
            BASE + "/sendMessage", data=payload,
            headers={"Content-Type": "application/json", "User-Agent": "local-livestream"},
        )
        urllib.request.urlopen(req, timeout=15).read()
    except Exception as exc:
        print(f"[telegram] send failed: {exc}", flush=True)


def child_environment():
    env = os.environ.copy()
    for name in ("TELEGRAM_BOT_TOKEN", "ALLOWED_CHAT_ID", "TELEGRAM_CHAT_ID",
                 "GITHUB_TOKEN", "GH_TOKEN"):
        env.pop(name, None)
    env.update(PYTHONUNBUFFERED="1", PYTHONIOENCODING="utf-8", NO_COLOR="1")
    return env


def process_options():
    if os.name == "nt":
        return {"creationflags": subprocess.CREATE_NO_WINDOW}
    return {"start_new_session": True}


def terminate_process_tree(process):
    """Stop only this service's process tree, never unrelated cloudflared."""
    if process is None:
        return
    try:
        if os.name == "nt":
            if process.poll() is None:
                subprocess.run(["taskkill", "/PID", str(process.pid), "/T", "/F"],
                               capture_output=True, timeout=8,
                               creationflags=subprocess.CREATE_NO_WINDOW)
        else:
            try:
                os.killpg(process.pid, signal.SIGTERM)
            except ProcessLookupError:
                pass
        if process.poll() is None:
            process.terminate()
        process.wait(timeout=5)
    except (OSError, subprocess.SubprocessError):
        try:
            process.kill()
            process.wait(timeout=3)
        except (OSError, subprocess.SubprocessError):
            pass
    finally:
        if os.name != "nt":
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass


def is_port_open(port, host="127.0.0.1", timeout=1):
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def choose_free_port(preferred=()):
    for port in (*preferred, 0):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
            try:
                listener.bind(("127.0.0.1", port))
                return listener.getsockname()[1]
            except OSError:
                continue
    raise RuntimeError("No free local port")


def generate_password():
    # 144 random bits, with all four required character classes guaranteed.
    return "Aa1-" + secrets.token_urlsafe(18)


# ----------------------------------------------------------------------------
# cloudflared (same download URL as the workflow's "Install Cloudflared" step)
# ----------------------------------------------------------------------------

CLOUDFLARED_URLS = {
    ("Windows", "AMD64"): "https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-windows-amd64.exe",
    ("Linux", "x86_64"): "https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-linux-amd64",
    ("Linux", "aarch64"): "https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-linux-arm64",
    ("Darwin", "arm64"): "https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-darwin-amd64.tgz",
}


def find_cloudflared():
    for candidate in ("cloudflared", "cloudflared.exe", r"C:\Windows\System32\cloudflared.exe",
                      "/usr/local/bin/cloudflared", "/usr/bin/cloudflared"):
        found = shutil.which(candidate)
        if found:
            return found
        if os.path.isfile(candidate):
            return candidate
    cached = Path(tempfile.gettempdir()) / "local-livestream" / (
        "cloudflared.exe" if os.name == "nt" else "cloudflared")
    if cached.is_file():
        return str(cached)
    print("[cloudflared] not found; downloading (same URL as the GitHub workflow)...", flush=True)
    url = CLOUDFLARED_URLS.get((platform.system(), platform.machine()))
    if not url:
        raise RuntimeError("No cloudflared download URL for this platform; install cloudflared manually")
    data = http_get(url, timeout=120)
    if url.endswith(".tgz"):  # macOS tarball
        import tarfile
        with tarfile.open(fileobj=io.BytesIO(data), mode="r:gz") as tar:
            member = next(m for m in tar.getmembers() if m.name.endswith("cloudflared"))
            data = tar.extractfile(member).read()
    cached.parent.mkdir(parents=True, exist_ok=True)
    tmp = cached.with_suffix(cached.suffix + ".tmp")
    tmp.write_bytes(data)
    os.replace(tmp, cached)
    if os.name != "nt":
        os.chmod(cached, 0o755)
    print(f"[cloudflared] installed: {cached}", flush=True)
    return str(cached)


# ----------------------------------------------------------------------------
# PhoneFS install (identical safety model as .github/phonefs_service.py)
# ----------------------------------------------------------------------------

def phonefs_install_dir():
    return Path(tempfile.gettempdir()) / "local-livestream" / "phonefs"


def _agent_source(archive):
    if hashlib.sha256(archive).hexdigest() != PACKAGE_SHA256:
        raise RuntimeError("PhoneFS ZIP checksum mismatch; refusing to execute it")
    with zipfile.ZipFile(io.BytesIO(archive)) as bundle:
        members = [info for info in bundle.infolist()
                   if PurePosixPath(info.filename).name == AGENT_NAME and not info.is_dir()]
        if len(members) != 1:
            raise RuntimeError(f"PhoneFS ZIP must contain exactly one {AGENT_NAME}")
        source = bundle.read(members[0]).decode("utf-8")
    if source.count(MAILBOX_START) != 1:
        raise RuntimeError("Unexpected PhoneFS build: cannot disable the public URL mailbox safely")
    source = source.replace(MAILBOX_START, MAILBOX_DISABLED)
    compile(source, AGENT_NAME, "exec")
    return source.encode("utf-8")


INSTALL_LOCK = threading.Lock()


def install_phonefs():
    directory = phonefs_install_dir()
    archive_path = directory / "package.zip"
    agent_path = directory / AGENT_NAME
    with INSTALL_LOCK:
        archive = None
        if archive_path.is_file():
            cached = archive_path.read_bytes()
            if hashlib.sha256(cached).hexdigest() == PACKAGE_SHA256:
                archive = cached
        if archive is None:
            failures = []
            for url in (PACKAGE_URL, PACKAGE_API_URL):
                try:
                    request = urllib.request.Request(url, headers={
                        "User-Agent": "local-livestream/phonefs",
                        "Accept": "application/vnd.github.raw+json",
                    })
                    with urllib.request.urlopen(request, timeout=30) as response:
                        data = response.read(5 * 1024 * 1024 + 1)
                    if hashlib.sha256(data).hexdigest() != PACKAGE_SHA256:
                        raise RuntimeError("PhoneFS ZIP checksum mismatch; refusing to execute it")
                    archive = data
                    break
                except (OSError, urllib.error.URLError) as exc:
                    failures.append(str(exc))
            if archive is None:
                raise RuntimeError("Could not download the pinned PhoneFS ZIP: " + "; ".join(failures))
        source = _agent_source(archive)
        directory.mkdir(parents=True, exist_ok=True)
        for destination, content in ((archive_path, archive), (agent_path, source)):
            if not destination.is_file() or destination.read_bytes() != content:
                temporary = destination.with_suffix(destination.suffix + ".tmp")
                temporary.write_bytes(content)
                os.replace(temporary, destination)
        return agent_path.resolve()


def _phonefs_login_ready(port, password):
    """Verify the exact password being sent to Telegram, not just an open socket."""
    request = urllib.request.Request(
        f"http://127.0.0.1:{port}/api/login",
        data=json.dumps({"password": password, "device": "local-startup-check"}).encode(),
        headers={"Content-Type": "application/json"}, method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=2) as response:
            result = json.loads(response.read())
        return result.get("ok") is True and bool(result.get("csrf"))
    except (OSError, ValueError, urllib.error.URLError):
        return False


# ----------------------------------------------------------------------------
# Global session state
# ----------------------------------------------------------------------------

class State:
    chat_id = None
    stop_all = threading.Event()

    # screen sharing
    screen_port = SCREEN_PORT
    screen_url = None
    screen_proc = None
    screen_tunnel_thread = None
    screen_active = False
    flask_server = None
    flask_started = False

    # phonefs
    pfs_password = None
    pfs_user = getpass.getuser()
    pfs_port = None
    pfs_url = None
    pfs_state = "stopped"     # starting | installing | connecting | online | failed | stopped
    pfs_error = None
    pfs_proc = None
    pfs_thread = None
    pfs_stop = threading.Event()
    pfs_config_dir = None
    pfs_logs = deque(maxlen=20)

    lock = threading.RLock()


def _redact(text):
    text = ANSI_RE.sub("", str(text))
    return text.replace(State.pfs_password, "[redacted]") if State.pfs_password else text


def connection_text():
    lines = ["PhoneFS online",
             f"PhoneFS URL: {State.pfs_url}",
             f"PhoneFS local port: {State.pfs_port}",
             f"User ID (PC username): {State.pfs_user}",
             f"Generated password: {State.pfs_password}",
             "PhoneFS login is password-only; no username is required."]
    if State.screen_url:
        lines += [f"Screen sharing URL: {State.screen_url}/",
                  f"Screen sharing local port: {State.screen_port}"]
    lines += ["Keep these links and credentials private.",
              "Send 'livestream status' to see them again, 'livestream restart' for",
              "new links/password, or 'stop stream' to close both tunnels."]
    return "\n".join(lines)


def status_text():
    with State.lock:
        port_ok = is_port_open(State.screen_port)
        proc_ok = State.screen_proc is not None and State.screen_proc.poll() is None
        lines = ["Screen sharing status:",
                 f"- Active: {State.screen_active}",
                 f"- Web server: {State.flask_started} (port {State.screen_port} open={port_ok})",
                 f"- Tunnel alive: {proc_ok}",
                 f"- Screen URL: {State.screen_url or ('connecting' if State.screen_active else 'stopped')}",
                 "", "PhoneFS status:"]
        if State.pfs_state == "online":
            lines.append(connection_text())
        else:
            lines.append(f"PhoneFS: {State.pfs_state}")
            if State.pfs_port:
                lines.append(f"Local port: {State.pfs_port}")
            if State.pfs_error:
                lines.append(f"Error: {_redact(State.pfs_error)}")
            if State.pfs_state in ("failed", "stopped"):
                lines.append("Send 'livestream restart' to retry.")
        return "\n".join(lines)


# ----------------------------------------------------------------------------
# Screen sharing web server (Flask + mss, same routes as the runner)
# ----------------------------------------------------------------------------

def ensure_pyautogui():
    try:
        import pyautogui
        pyautogui.FAILSAFE = False
        pyautogui.PAUSE = 0.01
        return pyautogui
    except Exception as exc:
        print(f"pyautogui import error: {exc}", flush=True)
        return None


INDEX_HTML = r"""<!DOCTYPE html>
<html>
<head>
  <title>PC Remote Desktop</title>
  <meta name="viewport" content="width=device-width, initial-scale=1, maximum-scale=1, user-scalable=no">
  <style>
    *{box-sizing:border-box;margin:0;padding:0}
    body{background:#0a0a0a;color:#eee;font-family:-apple-system,BlinkMacSystemFont,Segoe UI,Roboto,Helvetica,Arial,sans-serif;min-height:100vh;display:flex;flex-direction:column}
    header{background:#1a1a1a;padding:8px 12px;display:flex;align-items:center;justify-content:space-between;position:sticky;top:0;z-index:10;border-bottom:1px solid #333}
    header h1{font-size:14px;font-weight:600}
    header .status{font-size:11px;color:#0f0}
    #toolbar{background:#222;padding:6px;display:flex;gap:6px;flex-wrap:wrap;align-items:center;border-bottom:1px solid #333}
    #toolbar button,#toolbar input{font-size:12px;padding:6px 10px;border-radius:6px;border:1px solid #444;background:#333;color:#fff}
    #toolbar button:active{background:#555}
    #toolbar input{background:#111;min-width:140px;flex:1}
    #viewport{flex:1;display:flex;align-items:center;justify-content:center;background:#000;position:relative;overflow:hidden;touch-action:none}
    #screen{max-width:100%;max-height:calc(100vh - 140px);display:block;cursor:crosshair;user-select:none;-webkit-user-drag:none}
    #overlay{position:absolute;inset:0;pointer-events:none;display:flex;align-items:flex-start;justify-content:center;padding-top:6px}
    #fps{font:11px monospace;background:rgba(0,0,0,0.7);color:#0f0;padding:3px 6px;border-radius:4px}
    .keys{display:flex;gap:4px;flex-wrap:wrap}
    .keys button{padding:5px 8px;font-size:11px}
    .hint{font-size:10px;color:#888;padding:6px;text-align:center;background:#111}
    .sep{width:1px;height:22px;background:#444;margin:0 2px}
  </style>
</head>
<body>
  <header>
    <h1>&#128421; PC Remote Desktop</h1>
    <div class="status" id="st">Connecting...</div>
  </header>
  <div id="toolbar">
    <input id="typeText" placeholder="Type text here & Send" />
    <button onclick="sendType()">Send (Paste)</button>
    <button onclick="sendKey('enter')">Enter</button>
    <button onclick="sendKey('backspace')">&#9003;</button>
    <button onclick="sendKey('tab')">Tab</button>
    <button onclick="sendKey('escape')">Esc</button>
    <div class="sep"></div>
    <div class="keys">
      <button onclick="sendHotkey('ctrl+c')">Ctrl+C</button>
      <button onclick="sendHotkey('ctrl+v')">Ctrl+V</button>
      <button onclick="sendHotkey('ctrl+a')">Ctrl+A</button>
      <button onclick="sendHotkey('ctrl+z')">Ctrl+Z</button>
      <button onclick="sendHotkey('alt+f4')">Alt+F4</button>
      <button onclick="sendHotkey('win+r')">Win+R</button>
      <button onclick="sendHotkey('win+d')">Win+D</button>
    </div>
    <div class="sep"></div>
    <button onclick="sendKey('up')">&#8593;</button>
    <button onclick="sendKey('down')">&#8595;</button>
    <button onclick="sendKey('left')">&#8592;</button>
    <button onclick="sendKey('right')">&#8594;</button>
    <button onclick="location.reload()">&#8635;</button>
  </div>
  <div id="viewport">
    <img id="screen" src="/snapshot" alt="desktop" draggable="false">
    <div id="overlay"><div id="fps">Live</div></div>
  </div>
  <div class="hint">Tap / Click image to click &#8226; Right-click for right-click &#8226; Drag to select/drag &#8226; Scroll with wheel / two fingers &#8226; Use text box to type &#8226; Keyboard works when focused</div>
<script>
let img=document.getElementById('screen');
let st=document.getElementById('st');
let fpsEl=document.getElementById('fps');
let frames=0,lastT=Date.now(),errors=0,delay=500;
let dragging=false,startX=0,startY=0;
let screenW=1920,screenH=1080;
fetch('/info').then(r=>r.json()).then(j=>{screenW=j.width;screenH=j.height;st.textContent=screenW+'x'+screenH+' | Live'});
let snapshotMode=false;
function setupMjpeg(){
  img.src="/stream";
  img.onerror=()=>{snapshotMode=true;startSnapshot();};
  img.onload=()=>{st.textContent=screenW+'x'+screenH+' | Live MJPEG';};
}
function startSnapshot(){
  snapshotMode=true;
  img.src="/snapshot?t="+Date.now();
  function load(){
    let t=Date.now();let nx=new Image();
    nx.onload=function(){
      if(snapshotMode){
        img.src=nx.src;errors=0;
        if(delay>600)delay=600;
        frames++;
        let el=Date.now()-lastT;
        if(el>=2000){fpsEl.textContent=Math.round(frames*1000/el)+' FPS | Live';st.textContent=screenW+'x'+screenH+' | '+Math.round(frames*1000/el)+' FPS';frames=0;lastT=Date.now();}
        setTimeout(load,Math.max(80,delay-(Date.now()-t)));
      }
    };
    nx.onerror=function(){
      errors++;fpsEl.textContent='Reconnecting... ('+errors+')';st.textContent='Reconnecting...';
      delay=Math.min(delay*1.4,4000);
      setTimeout(load,Math.min(errors*600,3000));
    };
    nx.src='/snapshot?t='+t;
  }
  load();
}
setupMjpeg();
setTimeout(()=>{if(img.naturalWidth==0){startSnapshot();}},2500);
function getRatio(e){
  let rect=img.getBoundingClientRect();
  let x=(e.clientX-rect.left)/rect.width;
  let y=(e.clientY-rect.top)/rect.height;
  x=Math.max(0,Math.min(1,x));y=Math.max(0,Math.min(1,y));
  return{x,y};
}
function postInput(data){
  fetch('/input',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(data)}).catch(()=>{});
}
img.addEventListener('click',(e)=>{
  if(dragging)return;
  let r=getRatio(e);
  postInput({action:'click',x_ratio:r.x,y_ratio:r.y});
  fpsEl.textContent='Click '+(r.x*100).toFixed(1)+'% '+(r.y*100).toFixed(1)+'%';
  setTimeout(()=>fpsEl.textContent='Live',800);
});
img.addEventListener('contextmenu',(e)=>{
  e.preventDefault();
  let r=getRatio(e);
  postInput({action:'rclick',x_ratio:r.x,y_ratio:r.y});
  fpsEl.textContent='Right Click';
  setTimeout(()=>fpsEl.textContent='Live',800);
  return false;
});
img.addEventListener('dblclick',(e)=>{
  let r=getRatio(e);
  postInput({action:'dblclick',x_ratio:r.x,y_ratio:r.y});
});
img.addEventListener('mousedown',(e)=>{
  if(e.button!==0)return;
  dragging=true;
  let r=getRatio(e);
  startX=r.x;startY=r.y;
});
img.addEventListener('mouseup',(e)=>{
  if(!dragging)return;
  dragging=false;
  let r=getRatio(e);
  let dx=Math.abs(r.x-startX),dy=Math.abs(r.y-startY);
  if(dx>0.02||dy>0.02){
    postInput({action:'drag',x_ratio:startX,y_ratio:startY,x2_ratio:r.x,y2_ratio:r.y});
    fpsEl.textContent='Drag';
  }
});
img.addEventListener('wheel',(e)=>{
  e.preventDefault();
  postInput({action:'scroll',amount:-Math.sign(e.deltaY)*300});
},{passive:false});
img.addEventListener('touchstart',(e)=>{
  if(e.touches.length==1){
    let t=e.touches[0];let r=getRatio(t);
    startX=r.x;startY=r.y;
  }
},{passive:true});
img.addEventListener('touchend',(e)=>{
  if(e.changedTouches.length==1){
    let t=e.changedTouches[0];let r=getRatio(t);
    let dx=Math.abs(r.x-startX),dy=Math.abs(r.y-startY);
    if(dx<0.02&&dy<0.02){postInput({action:'click',x_ratio:r.x,y_ratio:r.y});}
    else{postInput({action:'drag',x_ratio:startX,y_ratio:startY,x2_ratio:r.x,y2_ratio:r.y});}
  }
},{passive:true});
document.addEventListener('keydown',(e)=>{
  if(document.activeElement.id==='typeText')return;
  if(e.repeat)return;
  let key=e.key.toLowerCase();
  if(['enter','tab','escape','backspace','delete','arrowup','arrowdown','arrowleft','arrowright'].includes(key)){
    e.preventDefault();
    let map={'arrowup':'up','arrowdown':'down','arrowleft':'left','arrowright':'right'};
    postInput({action:'key',keys:map[key]||key});
  }else if(e.ctrlKey||e.altKey||e.metaKey){
    let combo=[];
    if(e.ctrlKey)combo.push('ctrl');
    if(e.altKey)combo.push('alt');
    if(e.metaKey)combo.push('win');
    if(key.length===1)combo.push(key);
    else if(!['control','alt','meta','shift'].includes(key))combo.push(key);
    if(combo.length>1){e.preventDefault();postInput({action:'hotkey',keys:combo.join('+')});}
  }else if(key.length===1){
    postInput({action:'type',text:e.key});
  }
});
function sendType(){
  let el=document.getElementById('typeText');
  let t=el.value;
  if(!t)return;
  postInput({action:'type',text:t});
  el.value='';
  fpsEl.textContent='Typed '+t.length+' chars';
}
function sendKey(k){postInput({action:'key',keys:k})}
function sendHotkey(k){postInput({action:'hotkey',keys:k})}
document.getElementById('typeText').addEventListener('keydown',(e)=>{
  if(e.key==='Enter'){e.preventDefault();sendType();}
});
</script>
</body>
</html>
"""


def build_flask_app():
    from flask import Flask, Response, request, jsonify
    import numpy as np
    import cv2
    import mss
    import logging
    logging.getLogger("werkzeug").setLevel(logging.ERROR)

    app = Flask(__name__)
    screen_size = {"w": 1920, "h": 1080}
    try:
        with mss.mss() as sct:
            mon = sct.monitors[1] if len(sct.monitors) > 1 else sct.monitors[0]
            screen_size["w"], screen_size["h"] = mon["width"], mon["height"]
    except Exception:
        pass

    def primary_monitor(sct):
        return sct.monitors[1] if len(sct.monitors) > 1 else sct.monitors[0]

    def capture_jpeg(width=960, quality=35):
        with mss.mss() as sct:
            sct_img = sct.grab(primary_monitor(sct))
            frame = cv2.cvtColor(np.array(sct_img), cv2.COLOR_BGRA2BGR)
            h, w = frame.shape[:2]
            frame_resized = cv2.resize(frame, (width, int(h * (width / w))))
            _, buffer = cv2.imencode(".jpg", frame_resized, [int(cv2.IMWRITE_JPEG_QUALITY), quality])
            return buffer.tobytes(), w, h

    def gen_frames():
        while not State.stop_all.is_set():
            try:
                data, _, _ = capture_jpeg(width=960, quality=40)
                yield (b'--frame\r\n'
                       b'Content-Type: image/jpeg\r\n'
                       b'Content-Length: ' + str(len(data)).encode() + b'\r\n\r\n' +
                       data + b'\r\n')
                time.sleep(0.12)
            except Exception as exc:
                print(f"Frame gen error: {exc}", flush=True)
                time.sleep(0.5)

    @app.route("/")
    def index():
        return Response(INDEX_HTML, mimetype="text/html")

    @app.route("/snapshot")
    def snapshot():
        try:
            data, _, _ = capture_jpeg()
            return Response(data, mimetype="image/jpeg", headers={
                "Cache-Control": "no-store, no-cache, must-revalidate",
                "Pragma": "no-cache", "Expires": "0"})
        except Exception as exc:
            return Response(f"Error: {exc}", status=500)

    @app.route("/stream")
    def stream():
        return Response(gen_frames(), mimetype="multipart/x-mixed-replace; boundary=frame")

    @app.route("/info")
    def info():
        return jsonify({"width": screen_size["w"], "height": screen_size["h"]})

    @app.route("/input", methods=["POST"])
    def input_route():
        try:
            data = request.get_json(force=True) or {}
            action = (data.get("action") or "").lower()
            pyautogui = ensure_pyautogui()
            if not pyautogui:
                return jsonify({"ok": False, "error": "pyautogui missing"})

            def to_coords(d):
                if "x_ratio" in d and "y_ratio" in d:
                    return int(float(d["x_ratio"]) * screen_size["w"]), int(float(d["y_ratio"]) * screen_size["h"])
                if "x" in d and "y" in d:
                    return int(d["x"]), int(d["y"])
                return None, None

            if action == "click":
                x, y = to_coords(data)
                pyautogui.click(x, y) if x is not None else pyautogui.click()
                return jsonify({"ok": True})
            if action in ("rclick", "rightclick"):
                x, y = to_coords(data)
                pyautogui.rightClick(x, y) if x is not None else pyautogui.rightClick()
                return jsonify({"ok": True})
            if action in ("dblclick", "doubleclick", "double_click"):
                x, y = to_coords(data)
                pyautogui.doubleClick(x, y) if x is not None else pyautogui.doubleClick()
                return jsonify({"ok": True})
            if action == "move":
                x, y = to_coords(data)
                if x is not None:
                    pyautogui.moveTo(x, y)
                return jsonify({"ok": True})
            if action == "drag":
                x, y = to_coords(data)
                x2r, y2r = data.get("x2_ratio"), data.get("y2_ratio")
                if x is not None and x2r is not None:
                    pyautogui.moveTo(x, y, duration=0.05)
                    pyautogui.dragTo(int(float(x2r) * screen_size["w"]),
                                     int(float(y2r) * screen_size["h"]), duration=0.4, button="left")
                elif x is not None and "x2" in data and "y2" in data:
                    pyautogui.moveTo(x, y, duration=0.05)
                    pyautogui.dragTo(int(data["x2"]), int(data["y2"]), duration=0.4)
                return jsonify({"ok": True})
            if action == "scroll":
                pyautogui.scroll(int(data.get("amount", 300)))
                return jsonify({"ok": True})
            if action == "type":
                text = data.get("text", "")
                if text:
                    if len(text) > 80 or any(ord(c) > 127 for c in text):
                        try:
                            import pyperclip
                            pyperclip.copy(text)
                            pyautogui.hotkey("ctrl", "v")
                        except Exception:
                            pyautogui.write(text, interval=0.005)
                    else:
                        pyautogui.write(text, interval=0.01)
                return jsonify({"ok": True})
            if action == "key":
                k = (data.get("keys") or data.get("key") or "").lower().strip()
                if k:
                    mapping = {"arrowup": "up", "arrowdown": "down", "arrowleft": "left", "arrowright": "right"}
                    pyautogui.press(mapping.get(k, k))
                return jsonify({"ok": True})
            if action == "hotkey":
                keys = data.get("keys") or ""
                if keys:
                    parts = [p.strip().lower() for p in re.split(r"\+", keys) if p.strip()]
                    mp = {"cmd": "win", "super": "win", "windows": "win",
                          "return": "enter", "esc": "escape", "del": "delete"}
                    pyautogui.hotkey(*[mp.get(p, p) for p in parts])
                return jsonify({"ok": True})
            if action == "press":
                keys = data.get("keys") or data.get("key") or ""
                if keys:
                    if "+" in keys:
                        pyautogui.hotkey(*[p.strip() for p in keys.split("+")])
                    else:
                        pyautogui.press(keys)
                return jsonify({"ok": True})
            return jsonify({"ok": False, "error": f"unknown action {action}"})
        except Exception as exc:
            print(f"input error {exc}", flush=True)
            return jsonify({"ok": False, "error": str(exc)})

    @app.route("/stop-stream")
    def stop_stream():
        stop_both(notify=False)
        return Response("Screen sharing and PhoneFS stopped", mimetype="text/plain")

    return app


def start_screen_server():
    """Bind Flask synchronously so a port problem is visible immediately."""
    from werkzeug.serving import make_server
    with State.lock:
        if State.flask_server is not None:
            return True
        app = build_flask_app()
        port = SCREEN_PORT
        if is_port_open(port):
            port = choose_free_port()
            print(f"[screen] port {SCREEN_PORT} busy, using {port}", flush=True)
        server = make_server("0.0.0.0", port, app, threaded=True)
        State.flask_server = server
        State.screen_port = port
        State.flask_started = True
        threading.Thread(target=server.serve_forever, daemon=True, name="Screen web server").start()
    print(f"[screen] web server listening on 0.0.0.0:{port}", flush=True)
    return True


# ----------------------------------------------------------------------------
# Screen tunnel (same supervisor/fallback strategy as the runner)
# ----------------------------------------------------------------------------

def run_screen_tunnel(chat_id):
    attempt = 0
    while not State.stop_all.is_set():
        attempt += 1
        process = None
        logs = []
        url_sent = False
        try:
            for _ in range(15):
                if State.stop_all.is_set():
                    return
                if is_port_open(State.screen_port):
                    break
                time.sleep(1)
            else:
                raise RuntimeError(f"Screen web server is not responding on port {State.screen_port}")

            cfd = find_cloudflared()
            origin = f"http://127.0.0.1:{State.screen_port}"
            commands = [
                [cfd, "tunnel", "--url", origin],
                [cfd, "tunnel", "--url", f"http://localhost:{State.screen_port}"],
                [cfd, "tunnel", "--url", origin, "--protocol", "http2"],
                [cfd, "tunnel", "--url", origin, "--loglevel", "debug"],
                [cfd, "tunnel", "--url", origin, "--no-autoupdate"],
            ]
            command = commands[(attempt - 1) % len(commands)]
            env = child_environment()
            env.update(TUNNEL_ORIGIN_CERT="", FORCE_COLOR="0", CLOUDFLARED_NO_AUTOUPDATE="1")
            with State.lock:
                if State.stop_all.is_set():
                    return
                print(f"[screen tunnel] Starting: {' '.join(command)}", flush=True)
                process = subprocess.Popen(
                    command, stdin=subprocess.DEVNULL,
                    stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                    text=True, encoding="utf-8", errors="replace", bufsize=1,
                    env=env, **process_options(),
                )
                State.screen_proc = process
                State.screen_url = None

            output = queue.Queue()

            def pump(stream, output_queue):
                try:
                    for line in stream:
                        output_queue.put(line)
                finally:
                    output_queue.put(None)

            threading.Thread(target=pump, args=(process.stdout, output), daemon=True).start()
            deadline = time.monotonic() + 40
            while not State.stop_all.is_set():
                try:
                    line = output.get(timeout=0.5)
                except queue.Empty:
                    line = None
                if line:
                    clean = ANSI_RE.sub("", line.strip())
                    logs.append(clean)
                    logs = logs[-30:]
                    match = re.search(r"https://[a-zA-Z0-9-]+\.trycloudflare\.com", clean)
                    if match and not url_sent and process.poll() is None:
                        with State.lock:
                            if State.stop_all.is_set():
                                return
                            State.screen_url = match.group(0)
                            url_sent = True
                            print("[screen tunnel] URL obtained; notifying the Telegram chat", flush=True)
                            send_message(chat_id,
                                f"Screen sharing online (local port {State.screen_port}): {State.screen_url}/\n"
                                "Open on phone/PC for full mouse and keyboard control.\n"
                                "If the page initially shows 502, wait a few seconds and refresh.\n\n"
                                + status_text())
                code = process.poll()
                if code is not None:
                    raise RuntimeError(f"Screen tunnel disconnected (exit {code})")
                if not url_sent and time.monotonic() >= deadline:
                    raise RuntimeError("No screen tunnel URL after 40 seconds")
        except Exception as exc:
            if not State.stop_all.is_set():
                detail = "\n".join(logs)[-1800:]
                send_message(chat_id,
                    f"Screen tunnel: {exc}. Retrying (attempt {attempt}).\n{detail}\n"
                    "PhoneFS is independent. Send 'livestream status' for both services.")
        finally:
            terminate_process_tree(process)
            if process and process.stdout:
                process.stdout.close()
            with State.lock:
                State.screen_proc = None
                State.screen_url = None
        if State.stop_all.wait(5):
            return


def start_screen_tunnel(chat_id):
    with State.lock:
        if State.screen_tunnel_thread and State.screen_tunnel_thread.is_alive():
            return
        State.screen_active = True
        State.screen_tunnel_thread = threading.Thread(
            target=run_screen_tunnel, args=(chat_id,), daemon=True, name="Screen tunnel")
        State.screen_tunnel_thread.start()


# ----------------------------------------------------------------------------
# PhoneFS session (same flow as PhoneFSService._run / _attempt)
# ----------------------------------------------------------------------------

def run_phonefs(chat_id):
    try:
        with State.lock:
            State.pfs_state = "installing"
            State.pfs_error = None
            State.pfs_url = None
        agent = install_phonefs()
        if State.pfs_stop.is_set() or State.stop_all.is_set():
            return
        cfd = find_cloudflared()

        State.pfs_config_dir = Path(tempfile.mkdtemp(prefix="session-", dir=agent.parent))
        # A fresh config directory prevents an earlier saved password from
        # silently overriding the one generated for this session.
        result = subprocess.run(
            [sys.executable, "-u", str(agent), "--config-dir", str(State.pfs_config_dir), "set-password"],
            input=State.pfs_password + "\n", capture_output=True, text=True,
            encoding="utf-8", errors="replace", timeout=30,
            env=child_environment(), cwd=agent.parent, **process_options(),
        )
        if result.returncode:
            detail = _redact(result.stderr or result.stdout)[-1200:]
            raise RuntimeError("PhoneFS password setup failed: " + detail)
        if State.pfs_stop.is_set() or State.stop_all.is_set():
            return

        for attempt in range(1, 4):
            with State.lock:
                State.pfs_port = choose_free_port(PREFERRED_PORTS)
                State.pfs_state = "connecting"
                State.pfs_error = None
                State.pfs_logs.clear()
            code, was_online = phonefs_attempt(chat_id, agent, cfd, attempt)
            if State.pfs_stop.is_set() or State.stop_all.is_set():
                return
            with State.lock:
                State.pfs_url = None
            if was_online:
                with State.lock:
                    State.pfs_state = "stopped" if code == 0 else "failed"
                send_message(chat_id, f"PhoneFS stopped (exit {code}). Screen sharing is unchanged. "
                                      "Send 'livestream restart' to start PhoneFS again.")
                return
            if attempt < 3:
                send_message(chat_id, f"PhoneFS could not connect (attempt {attempt}/3). "
                                      "Retrying with a separate tunnel; screen sharing is unchanged.")
                if State.pfs_stop.wait(3) or State.stop_all.wait(3):
                    return
        raise RuntimeError(State.pfs_error or "PhoneFS tunnel failed to start")
    except Exception as exc:
        with State.lock:
            State.pfs_error = _redact(exc)
            State.pfs_state = "failed"
        send_message(chat_id, "PhoneFS setup failed: " + _redact(exc) +
                              "\nScreen sharing is unchanged. Send 'livestream restart' to retry.")
    finally:
        terminate_process_tree(State.pfs_proc)
        with State.lock:
            State.pfs_proc = None
            State.pfs_url = None
        if State.pfs_config_dir:
            shutil.rmtree(State.pfs_config_dir, ignore_errors=True)
        if State.pfs_stop.is_set() or State.stop_all.is_set():
            with State.lock:
                State.pfs_state = "stopped"


def phonefs_attempt(chat_id, agent, cfd, attempt):
    command = [sys.executable, "-u", str(agent), "--config-dir", str(State.pfs_config_dir),
               "tunnel", "--port", str(State.pfs_port), "--hours", "6",
               "--cloudflared", cfd, "--prefix", "", "--no-qr", "--no-pty", "--verbose"]
    if attempt > 1:
        command += ["--protocol", "http2"]
    output = queue.Queue()
    with State.lock:
        if State.pfs_stop.is_set() or State.stop_all.is_set():
            return 0, False
        process = subprocess.Popen(command, stdin=subprocess.DEVNULL,
                                   stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                   text=True, encoding="utf-8", errors="replace", bufsize=1,
                                   env=child_environment(), cwd=agent.parent, **process_options())
        State.pfs_proc = process
    print(f"[phonefs] Starting verified agent on local port {State.pfs_port}", flush=True)

    def pump():
        try:
            for line in process.stdout:
                output.put(line)
        finally:
            output.put(None)

    threading.Thread(target=pump, daemon=True).start()
    deadline = time.monotonic() + 115  # the bundle waits up to 90s for cloudflared
    candidate = None
    online = False
    next_probe = 0
    try:
        while not State.pfs_stop.is_set() and not State.stop_all.is_set():
            try:
                line = output.get(timeout=0.5)
            except queue.Empty:
                line = None
            if line:
                clean = _redact(line.strip())
                State.pfs_logs.append(clean)
                match = URL_RE.search(clean)
                if match:
                    candidate = match.group(0) + "/"
            code = process.poll()
            if code is not None:
                if not online or code != 0:
                    phase = "before startup" if not online else "after connecting"
                    with State.lock:
                        State.pfs_error = "Agent exited %s (exit %s). %s" % (
                            phase, code, "\n".join(State.pfs_logs)[-1500:])
                return code, online
            if candidate and not online and time.monotonic() >= next_probe:
                next_probe = time.monotonic() + 1
                if _phonefs_login_ready(State.pfs_port, State.pfs_password):
                    with State.lock:
                        if State.pfs_stop.is_set() or State.stop_all.is_set():
                            return 0, False
                        State.pfs_url = candidate
                        State.pfs_state = "online"
                        online = True
                        print("[phonefs] Login verified; notifying the Telegram chat", flush=True)
                        send_message(chat_id, connection_text())
            if not online and time.monotonic() >= deadline:
                with State.lock:
                    State.pfs_error = ("Timed out waiting for a PhoneFS URL and a successful login. "
                                       + "\n".join(State.pfs_logs)[-1500:])
                return 2, False
        return 0, online
    finally:
        terminate_process_tree(process)
        if process.stdout:
            process.stdout.close()
        with State.lock:
            if State.pfs_proc is process:
                State.pfs_proc = None


def start_phonefs(chat_id):
    """Idempotent: reuses a running session, restarts a dead/failed one."""
    with State.lock:
        if State.pfs_thread and State.pfs_thread.is_alive() and not State.pfs_stop.is_set():
            send_message(chat_id, status_text())
            return
        State.pfs_stop = threading.Event()
        State.pfs_password = generate_password()
        State.pfs_state = "starting"
        State.pfs_thread = threading.Thread(target=run_phonefs, args=(chat_id,),
                                            daemon=True, name="PhoneFS")
        send_message(chat_id, "Setting up PhoneFS automatically on a second port; "
                              "its link and generated password will follow.")
        State.pfs_thread.start()


def stop_phonefs():
    with State.lock:
        State.pfs_stop.set()
        State.pfs_state = "stopping"
        process, thread = State.pfs_proc, State.pfs_thread
    terminate_process_tree(process)
    if thread and thread is not threading.current_thread():
        thread.join(timeout=5)
    with State.lock:
        State.pfs_url = None
        State.pfs_state = "stopped"


def stop_both(chat_id=None, notify=True):
    """Close both public tunnels; keep the local web server for reuse."""
    with State.lock:
        State.screen_active = False
        process, thread = State.screen_proc, State.screen_tunnel_thread
        State.screen_url = None
    terminate_process_tree(process)
    if thread and thread is not threading.current_thread():
        thread.join(timeout=5)
    with State.lock:
        State.screen_proc = None
        State.screen_tunnel_thread = None
    stop_phonefs()
    if notify and chat_id is not None:
        send_message(chat_id, "Screen sharing and PhoneFS stopped; both public tunnels are closed.")


def restart_both(chat_id):
    send_message(chat_id, "Restarting screen sharing + PhoneFS; a new PhoneFS password will be generated.")
    stop_both(notify=False)
    with State.lock:
        State.pfs_password = generate_password()
    start_screen_tunnel(chat_id)
    start_phonefs(chat_id)


def shutdown_everything():
    State.stop_all.set()
    stop_both(notify=False)
    if State.flask_server:
        try:
            State.flask_server.shutdown()
            State.flask_server.server_close()
        except Exception:
            pass


# ----------------------------------------------------------------------------
# Telegram chat resolution + command loop
# ----------------------------------------------------------------------------

def resolve_chat_id():
    if ALLOWED_CHAT_ID:
        print(f"[telegram] Using configured chat id {ALLOWED_CHAT_ID}", flush=True)
        return ALLOWED_CHAT_ID
    print("[telegram] No TELEGRAM_CHAT_ID set: send ANY message to your bot now "
          "(you have 3 minutes)...", flush=True)
    try:
        result = api_get("getUpdates", {"offset": -1}, timeout=15).get("result") or []
        offset = result[-1]["update_id"] + 1 if result else 0
    except Exception:
        offset = 0
    deadline = time.monotonic() + 180
    while time.monotonic() < deadline and not State.stop_all.is_set():
        try:
            updates = api_get("getUpdates", {"offset": offset, "timeout": 25}, timeout=35).get("result", [])
        except Exception:
            time.sleep(2)
            continue
        for upd in updates:
            offset = upd["update_id"] + 1
            chat = (upd.get("message") or upd.get("edited_message") or {}).get("chat") or {}
            if chat.get("id"):
                print(f"[telegram] Claimed chat id {chat['id']}", flush=True)
                return int(chat["id"])
    raise SystemExit("No Telegram chat claimed within 3 minutes; set TELEGRAM_CHAT_ID and retry.")


def command_loop(chat_id):
    """Same owner-chat model as the runner: after claiming, other chats are ignored."""
    try:
        result = api_get("getUpdates", {"offset": -1}, timeout=15).get("result") or []
        offset = result[-1]["update_id"] + 1 if result else 0
    except Exception:
        offset = 0
    print("[telegram] Command loop running: 'livestream status' | 'livestream restart' | "
          "'stop stream' | '/stop'", flush=True)
    while not State.stop_all.is_set():
        try:
            updates = api_get("getUpdates", {"offset": offset, "timeout": 30}, timeout=40).get("result", [])
        except Exception:
            if State.stop_all.wait(3):
                return
            continue
        for upd in updates:
            offset = upd["update_id"] + 1
            msg = upd.get("message") or {}
            cid = (msg.get("chat") or {}).get("id")
            if cid != chat_id:
                continue
            text = (msg.get("text") or "").strip().lower()
            if not text:
                continue
            if text in ("livestream status", "live status", "stream status", "status"):
                send_message(chat_id, status_text())
            elif text in ("livestream restart", "live restart", "stream restart",
                          "restart stream", "restart livestream", "reconnect"):
                threading.Thread(target=restart_both, args=(chat_id,), daemon=True).start()
            elif text in ("livestream", "live", "stream", "start stream"):
                # Idempotent, like on the runner: reuses healthy services.
                with State.lock:
                    screen_alive = State.screen_tunnel_thread and State.screen_tunnel_thread.is_alive()
                if not screen_alive:
                    start_screen_tunnel(chat_id)
                start_phonefs(chat_id)
            elif text in ("stop stream", "stop livestream", "stop live", "close stream"):
                threading.Thread(target=stop_both, args=(chat_id,), daemon=True).start()
            elif text in ("/stop", "stop bot", "exit"):
                send_message(chat_id, "Stopping local livestream; closing both tunnels...")
                State.stop_all.set()
                return
            elif text in ("/start", "/help", "help"):
                send_message(chat_id,
                    "Local PC livestream is running.\n\n"
                    "- 'livestream status' - both links, credentials & diagnostics\n"
                    "- 'livestream restart' - restart both with a fresh PhoneFS password\n"
                    "- 'stop stream' - close both public tunnels\n"
                    "- '/stop' - shut everything down and exit this script")
            else:
                send_message(chat_id, "This local script only handles livestream commands: "
                                      "'livestream status', 'livestream restart', 'stop stream', '/stop'.")
    return


# ----------------------------------------------------------------------------
# Main
# ----------------------------------------------------------------------------

def main():
    global TOKEN, BASE
    if not TOKEN:
        print("ERROR: set the TELEGRAM_BOT_TOKEN environment variable first.")
        print("  Windows cmd:     set TELEGRAM_BOT_TOKEN=123456:ABC...")
        print("  PowerShell:      $env:TELEGRAM_BOT_TOKEN='123456:ABC...'")
        print("  Linux/macOS:     export TELEGRAM_BOT_TOKEN=123456:ABC...")
        sys.exit(1)
    BASE = f"https://api.telegram.org/bot{TOKEN}"
    try:
        me = api_get("getMe", timeout=15)
        if not me.get("ok"):
            raise RuntimeError(me)
        print(f"[telegram] Bot: @{me['result'].get('username')}", flush=True)
    except Exception as exc:
        print(f"ERROR: Telegram token check failed: {exc}")
        sys.exit(1)

    if os.name != "nt":
        print("WARNING: the pinned PhoneFS agent is a Windows build; on this OS only "
              "screen sharing will start.", flush=True)

    print("SECURITY NOTE: both links give full control of this PC. Keep them private.", flush=True)

    # 1. Claim the Telegram chat first (credentials are only ever sent there).
    chat_id = resolve_chat_id()
    State.chat_id = chat_id

    # 2. Screen sharing: Flask server on port 5000 (or next free), then tunnel 1.
    try:
        start_screen_server()
    except Exception as exc:
        send_message(chat_id, f"Screen sharing could not start: {exc}")
        print(f"[screen] FATAL: {exc}", flush=True)
        sys.exit(1)
    send_message(chat_id, "Starting screen sharing alongside PhoneFS on its own port...\n"
                          "(same setup as the GitHub runner 'livestream')")
    start_screen_tunnel(chat_id)

    # 3. PhoneFS: second tunnel + generated password (Windows only).
    if os.name == "nt":
        start_phonefs(chat_id)
    else:
        send_message(chat_id, "PhoneFS skipped: the fixed PhoneFS agent is a Windows build.")

    # 4. Serve Telegram commands until /stop or Ctrl+C.
    try:
        command_loop(chat_id)
    except KeyboardInterrupt:
        pass
    finally:
        print("\n[main] Shutting down...", flush=True)
        if State.chat_id:
            send_message(State.chat_id, "Local livestream shutting down; closing both tunnels.")
        shutdown_everything()
        print("[main] Both tunnels closed. Bye.", flush=True)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        shutdown_everything()
