import requests
import subprocess
import tempfile
import time
import sys
import os
import glob
import threading
import signal
import re
import json
import shutil
import platform
import zipfile
import pathlib
import functools

from phonefs_service import PhoneFSService, child_environment, process_options, terminate_process_tree

TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN")
ALLOWED_CHAT_ID = os.environ.get("ALLOWED_CHAT_ID")
if ALLOWED_CHAT_ID:
    ALLOWED_CHAT_ID = int(ALLOWED_CHAT_ID)

BASE  = f"https://api.telegram.org/bot{TOKEN}"
TICK  = chr(96) * 3

current_process = None
livestream_proc = None
livestream_url = None
livestream_active = False
livestream_flask_started = False
livestream_server = None
livestream_tunnel_thread = None
livestream_stop_event = None
livestream_chat_id = None
livestream_lock = threading.RLock()
remote_operation_lock = threading.RLock()
SCREEN_W = 1920
SCREEN_H = 1080

# ---------- Telegram helpers ----------

def send_message(chat_id, text, parse_mode='Markdown'):
    try:
        if not text:
            return
        # Telegram markdown escaping tricky, fallback to plain if fails
        if len(text) > 4000:
            text = text[:3900] + "\n...(truncated)"
        payload = {"chat_id": chat_id, "text": text, "disable_web_page_preview": True}
        if parse_mode:
            payload["parse_mode"] = parse_mode
        r = requests.post(f"{BASE}/sendMessage", json=payload, timeout=15)
        if r.status_code != 200:
            # fallback without markdown
            payload.pop("parse_mode", None)
            requests.post(f"{BASE}/sendMessage", json=payload, timeout=15)
        return r.json()
    except Exception as e:
        print(f"[send error] {e}", flush=True)

def send_document(chat_id, file_path):
    try:
        if not os.path.exists(file_path):
            send_message(chat_id, f"Error: `{file_path}` not found.")
            return
        with open(file_path, 'rb') as f:
            return requests.post(f"{BASE}/sendDocument", data={"chat_id": chat_id}, files={"document": f}, timeout=60).json()
    except Exception as e:
        print(f"[send_doc error] {e}", flush=True)
        send_message(chat_id, f"Send document error: {e}")

def send_photo(chat_id, file_path):
    try:
        if not os.path.exists(file_path):
            send_message(chat_id, f"Photo not found: `{file_path}`")
            return
        with open(file_path, 'rb') as f:
            requests.post(f"{BASE}/sendPhoto", data={"chat_id": chat_id}, files={"photo": f}, timeout=30)
    except Exception as e:
        print(f"[send_photo error] {e}", flush=True)

def send_large_text(chat_id, text, prefix=""):
    """Send large output as message or document"""
    try:
        if not text:
            text = "(No output)"
        full = prefix + text if prefix else text
        if len(full) > 3800:
            with tempfile.NamedTemporaryFile(mode="w", suffix=".txt", delete=False, encoding="utf-8") as f:
                f.write(full)
                t_name = f.name
            send_document(chat_id, t_name)
            try: os.unlink(t_name)
            except: pass
        else:
            send_message(chat_id, f"{TICK}\n{full}\n{TICK}")
    except Exception as e:
        send_message(chat_id, f"Error sending output: {e}")

def download_file(chat_id, file_id, file_name):
    try:
        file_name = os.path.basename(file_name) or f"file_{int(time.time())}"
        # sanitize
        file_name = file_name.replace("/", "_").replace("\\", "_")
        file_info = requests.get(f"{BASE}/getFile", params={"file_id": file_id}, timeout=10).json()
        if not file_info.get("ok"):
            send_message(chat_id, f"Download failed: {file_info}")
            return
        url = f"https://api.telegram.org/file/bot{TOKEN}/{file_info['result']['file_path']}"
        data = requests.get(url, timeout=60).content
        with open(file_name, 'wb') as f:
            f.write(data)
        send_message(chat_id, f"Saved `{file_name}` ({len(data)} bytes) -> `{os.path.abspath(file_name)}`")
    except Exception as e:
        send_message(chat_id, f"Download error: {e}")

def run_command(chat_id, cmd, is_python=False):
    global current_process
    def target():
        global current_process
        try:
            if is_python:
                with tempfile.NamedTemporaryFile(mode="w", suffix=".py", delete=False, encoding="utf-8") as f:
                    f.write(cmd)
                    fname = f.name
                p_cmd = [sys.executable, fname]
            else:
                p_cmd = cmd
                fname = None
            # need shell=True for shell commands string, keep stdin pipe for interactive input via "input <text>"
            current_process = subprocess.Popen(p_cmd, shell=not is_python, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, stdin=subprocess.PIPE, text=True, cwd=os.getcwd(), bufsize=1)
            out, _ = current_process.communicate(timeout=300)
            current_process = None
            if fname:
                try: os.unlink(fname)
                except: pass
            res = (out or "").strip() or "(No output)"
            send_large_text(chat_id, res)
        except subprocess.TimeoutExpired:
            try: current_process.kill()
            except: pass
            current_process = None
            send_message(chat_id, "Command timed out after 300s and was killed.")
        except Exception as e:
            current_process = None
            send_message(chat_id, f"Error: {e}")
    threading.Thread(target=target, daemon=True).start()

# ---------- Screenshot ----------
def take_screenshot(chat_id, mode="normal"):
    filename = f"screenshot_{int(time.time())}.png"
    try:
        import mss
        with mss.mss() as sct:
            # support hd / low quality variants via post-processing later if needed
            sct.shot(output=filename)
        send_photo(chat_id, filename)
        if os.path.exists(filename):
            os.remove(filename)
        send_message(chat_id, f"CWD: `{os.getcwd()}`")
    except Exception as e:
        send_message(chat_id, f"Screenshot error: {e}")

def take_screenshot_bytes(quality=40, width=854):
    """Helper for livestream: returns jpeg bytes"""
    try:
        import mss
        import numpy as np
        import cv2
        with mss.mss() as sct:
            monitor = sct.monitors[1] if len(sct.monitors) > 1 else sct.monitors[0]
            sct_img = sct.grab(monitor)
            frame = np.array(sct_img)
            frame = cv2.cvtColor(frame, cv2.COLOR_BGRA2BGR)
            h, w = frame.shape[:2]
            new_h = int(h * (width / w))
            frame_resized = cv2.resize(frame, (width, new_h))
            _, buffer = cv2.imencode(".jpg", frame_resized, [int(cv2.IMWRITE_JPEG_QUALITY), quality])
            return buffer.tobytes(), w, h
    except Exception as e:
        print(f"[screenshot_bytes] {e}")
        return None, 0, 0

# ---------- Input helpers (pyautogui based) ----------

def _ensure_pyautogui():
    try:
        import pyautogui
        pyautogui.FAILSAFE = False
        pyautogui.PAUSE = 0.01
        return pyautogui
    except Exception as e:
        print(f"pyautogui import error: {e}")
        return None

def do_type(chat_id, text, use_clipboard=False):
    """Robust typing: supports unicode, long text, special chars"""
    if not text:
        send_message(chat_id, "Usage: `type <text>` - types text into active window\nAliases: `paste <text>`, `typepaste <text>` uses clipboard for reliability")
        return
    try:
        pyautogui = _ensure_pyautogui()
        if not pyautogui:
            send_message(chat_id, "pyautogui not available")
            return
        # For long text or unicode or if user explicitly wants clipboard
        needs_clipboard = use_clipboard or len(text) > 120 or any(ord(c) > 127 for c in text) or "\n" in text
        if needs_clipboard:
            try:
                # Try pyperclip first, fallback to tkinter, fallback to powershell Set-Clipboard on windows
                copied = False
                try:
                    import pyperclip
                    pyperclip.copy(text)
                    copied = True
                except:
                    pass
                if not copied:
                    try:
                        import tkinter
                        r = tkinter.Tk()
                        r.withdraw()
                        r.clipboard_clear()
                        r.clipboard_append(text)
                        r.update()
                        r.destroy()
                        copied = True
                    except:
                        pass
                if not copied and os.name == 'nt':
                    # powershell fallback
                    ps_cmd = f'powershell -command "Set-Clipboard -Value @\'\n{text}\n\'@"'
                    subprocess.run(ps_cmd, shell=True)
                    copied = True
                time.sleep(0.2)
                pyautogui.hotkey('ctrl', 'v')
                time.sleep(0.2)
                send_message(chat_id, f"Pasted {len(text)} chars via clipboard")
                return
            except Exception as e:
                print(f"clipboard paste fallback failed {e}, trying direct type")
        # Direct typing with interval for reliability
        # pyautogui.write handles \n as enter? Better split
        lines = text.split("\n")
        for i, line in enumerate(lines):
            if line:
                pyautogui.write(line, interval=0.01)
            if i < len(lines) - 1:
                pyautogui.press('enter')
                time.sleep(0.05)
        send_message(chat_id, f"Typed {len(text)} chars")
    except Exception as e:
        send_message(chat_id, f"Type error: {e}")

def do_key(chat_id, combo):
    """Press keys / hotkeys. Combo like 'enter', 'ctrl+c', 'alt+f4', 'win+r', 'ctrl+shift+t'"""
    if not combo:
        send_message(chat_id, "Usage: `press <key>` or `key <key>`\nExamples:\n- `press enter`\n- `press ctrl+c`\n- `press alt+f4`\n- `press win+r`\n- `press f5`\nSpecial keys: enter, tab, esc, backspace, delete, up/down/left/right, home, end, pageup/pagedown, f1..f12, win, printscreen")
        return
    try:
        pyautogui = _ensure_pyautogui()
        if not pyautogui:
            send_message(chat_id, "pyautogui not available")
            return
        # Normalize combo: split by + and lower
        parts = [p.strip().lower() for p in re.split(r'\+', combo) if p.strip()]
        # Map aliases
        key_map = {
            "return": "enter", "esc": "escape", "del": "delete", "ins": "insert",
            "pgup": "pageup", "pgdn": "pagedown", "prtsc": "printscreen", "apps": "apps",
            "cmd": "win", "windows": "win", "super": "win", "option": "alt"
        }
        normalized = [key_map.get(k, k) for k in parts]
        # Validate keys: pyautogui has KEY_NAMES
        if len(normalized) == 1:
            pyautogui.press(normalized[0])
        else:
            pyautogui.hotkey(*normalized)
        send_message(chat_id, f"Pressed `{combo}`")
    except Exception as e:
        send_message(chat_id, f"Key error `{combo}`: {e}")

def do_mouse(chat_id, action, args_text):
    """Mouse controls: click, rightclick, double, move, drag, scroll, pos"""
    try:
        pyautogui = _ensure_pyautogui()
        if not pyautogui:
            send_message(chat_id, "pyautogui not available")
            return
        action = action.lower()
        args = args_text.strip() if args_text else ""
        if action in ("pos", "position", "where", "mousepos"):
            x, y = pyautogui.position()
            w, h = pyautogui.size()
            send_message(chat_id, f"Mouse at ({x}, {y}) | Screen {w}x{h} | CWD `{os.getcwd()}`")
            return
        if action == "move":
            m = re.findall(r'-?\d+', args)
            if len(m) >= 2:
                x, y = int(m[0]), int(m[1])
                pyautogui.moveTo(x, y, duration=0.2)
                send_message(chat_id, f"Moved to ({x}, {y})")
            else:
                send_message(chat_id, "Usage: `move <x> <y>` e.g. `move 500 300`")
            return
        if action in ("click", "leftclick"):
            # Check if args has coordinates like "100 200"
            m = re.findall(r'-?\d+', args)
            if len(m) >= 2:
                x, y = int(m[0]), int(m[1])
                # Optional button count? default single
                pyautogui.click(x, y)
                send_message(chat_id, f"Clicked at ({x}, {y})")
            elif args:
                # Try UI automation by name
                ui_automation(chat_id, "click", args)
            else:
                # click current position
                pyautogui.click()
                send_message(chat_id, "Clicked at current position")
            return
        if action in ("rclick", "rightclick", "right_click"):
            m = re.findall(r'-?\d+', args)
            if len(m) >= 2:
                x, y = int(m[0]), int(m[1])
                pyautogui.rightClick(x, y)
                send_message(chat_id, f"Right-clicked at ({x}, {y})")
            elif args:
                # fallback: try to find control then right click? Use pyautogui right click at current?
                # For now just right click current
                pyautogui.rightClick()
                send_message(chat_id, f"Right-clicked (searched `{args}` not precise, clicked current)")
            else:
                pyautogui.rightClick()
                send_message(chat_id, "Right-clicked")
            return
        if action in ("doubleclick", "dclick", "double_click", "double click"):
            m = re.findall(r'-?\d+', args)
            if len(m) >= 2:
                x, y = int(m[0]), int(m[1])
                pyautogui.doubleClick(x, y)
                send_message(chat_id, f"Double-clicked at ({x}, {y})")
            elif args and not re.match(r'^\d', args):
                ui_automation(chat_id, "double_click", args)
            else:
                pyautogui.doubleClick()
                send_message(chat_id, "Double-clicked")
            return
        if action == "drag":
            m = re.findall(r'-?\d+', args)
            if len(m) == 4:
                x1, y1, x2, y2 = map(int, m)
                pyautogui.moveTo(x1, y1, duration=0.1)
                pyautogui.dragTo(x2, y2, duration=0.5, button='left')
                send_message(chat_id, f"Dragged from ({x1},{y1}) to ({x2},{y2})")
            elif len(m) == 2:
                x, y = map(int, m)
                pyautogui.dragTo(x, y, duration=0.5, button='left')
                send_message(chat_id, f"Dragged to ({x},{y})")
            else:
                send_message(chat_id, "Usage: `drag <x1> <y1> <x2> <y2>` or `drag <x> <y>`")
            return
        if action in ("scroll", "wheel"):
            # args like "500", "-500", "up 3", "down 5"
            if not args:
                send_message(chat_id, "Usage: `scroll <amount>` (positive up, negative down) or `scroll up 500` / `scroll down 500`")
                return
            arg_low = args.lower()
            if "up" in arg_low:
                m = re.findall(r'-?\d+', args)
                amount = int(m[0]) if m else 500
                pyautogui.scroll(amount)
                send_message(chat_id, f"Scrolled up {amount}")
            elif "down" in arg_low:
                m = re.findall(r'-?\d+', args)
                amount = int(m[0]) if m else 500
                pyautogui.scroll(-abs(amount))
                send_message(chat_id, f"Scrolled down {amount}")
            else:
                m = re.findall(r'-?\d+', args)
                if m:
                    amount = int(m[0])
                    pyautogui.scroll(amount)
                    send_message(chat_id, f"Scrolled {amount}")
                else:
                    send_message(chat_id, "Invalid scroll amount")
            return
        if action in ("hold", "mousedown"):
            pyautogui.mouseDown()
            send_message(chat_id, "Mouse down")
            return
        if action in ("release", "mouseup"):
            pyautogui.mouseUp()
            send_message(chat_id, "Mouse up")
            return
        send_message(chat_id, f"Unknown mouse action `{action}`. Try: click, rclick, doubleclick, move, drag, scroll, pos")
    except Exception as e:
        send_message(chat_id, f"Mouse error `{action} {args_text}`: {e}")

def do_clipboard(chat_id, action, text=None):
    try:
        if action == "get":
            copied = None
            try:
                import pyperclip
                copied = pyperclip.paste()
            except:
                pass
            if copied is None:
                try:
                    import tkinter
                    r = tkinter.Tk()
                    r.withdraw()
                    copied = r.clipboard_get()
                    r.destroy()
                except Exception as e:
                    copied = f"(clipboard unavailable: {e})"
            if os.name == 'nt' and (copied is None or "unavailable" in str(copied)):
                try:
                    out = subprocess.check_output("powershell -command Get-Clipboard", shell=True, text=True, timeout=5)
                    copied = out.strip()
                except: pass
            send_large_text(chat_id, copied or "(empty)", prefix="Clipboard:\n")
        elif action == "set":
            if text is None:
                send_message(chat_id, "Usage: `clip set <text>` or `copyclip <text>`")
                return
            try:
                import pyperclip
                pyperclip.copy(text)
                send_message(chat_id, f"Clipboard set ({len(text)} chars)")
                return
            except:
                pass
            try:
                # fallback powershell
                # need to handle quoting
                safe = text.replace("'", "''")
                subprocess.run(f"powershell -command \"Set-Clipboard -Value '{safe}'\"", shell=True)
                send_message(chat_id, f"Clipboard set via powershell ({len(text)} chars)")
            except Exception as e:
                send_message(chat_id, f"Clipboard set error: {e}")
        elif action == "clear":
            do_clipboard(chat_id, "set", "")
    except Exception as e:
        send_message(chat_id, f"Clipboard error: {e}")

# ---------- File helpers ----------
def handle_file_commands(chat_id, orig_text, text_lower):
    """Returns True if handled, else False"""
    # pwd / cwd
    if text_lower in ("pwd", "cwd", "where", "whereami", "dir pwd"):
        send_message(chat_id, f"CWD: `{os.getcwd()}`\nFiles: {len(os.listdir('.'))} items")
        return True
    # ls / dir / ll
    if text_lower.startswith("ls") or text_lower.startswith("dir ") or text_lower in ("ll", "list", "files"):
        # parse path
        arg = ""
        if text_lower.startswith("ls"):
            arg = orig_text[2:].strip()
        elif text_lower.startswith("dir "):
            arg = orig_text[4:].strip()
        elif text_lower.startswith("ll"):
            arg = orig_text[2:].strip()
        target = arg.strip('\"\'') if arg else "."
        try:
            if not os.path.exists(target):
                send_message(chat_id, f"Path not found: `{target}`")
                return True
            if os.path.isfile(target):
                st = os.stat(target)
                send_message(chat_id, f"File: `{target}`\nSize: {st.st_size} bytes\nModified: {time.ctime(st.st_mtime)}")
                return True
            items = os.listdir(target)
            # detailed
            lines = []
            for it in sorted(items)[:200]:
                p = os.path.join(target, it)
                try:
                    st = os.stat(p)
                    typ = "DIR " if os.path.isdir(p) else "FILE"
                    size = st.st_size if os.path.isfile(p) else "-"
                    lines.append(f"{typ} {size:>10}  {it}")
                except:
                    lines.append(f"???? {it}")
            if not lines:
                lines = ["(empty)"]
            header = f"Listing `{os.path.abspath(target)}` ({len(items)} items):\n"
            body = "\n".join(lines)
            if len(items) > 200:
                body += f"\n... and {len(items)-200} more"
            send_large_text(chat_id, header + body)
        except Exception as e:
            send_message(chat_id, f"ls error: {e}")
        return True
    if text_lower.startswith("cat ") or text_lower.startswith("read ") or text_lower.startswith("show ") or text_lower.startswith("typefile "):
        # cat file
        arg = orig_text.split(" ", 1)[1].strip().strip('\"\'') if " " in orig_text else ""
        if not arg:
            send_message(chat_id, "Usage: `cat <file>`")
            return True
        try:
            if not os.path.exists(arg):
                send_message(chat_id, f"File not found: `{arg}`")
                return True
            if os.path.isdir(arg):
                send_message(chat_id, f"`{arg}` is a directory, use `ls {arg}`")
                return True
            size = os.path.getsize(arg)
            if size > 2*1024*1024:
                send_message(chat_id, f"File too large ({size} bytes), sending as document...")
                send_document(chat_id, arg)
                return True
            with open(arg, 'r', encoding='utf-8', errors='ignore') as f:
                content = f.read(8000)
                if len(content) == 8000:
                    content += "\n...(truncated, use `get <file>` for full file)"
            send_large_text(chat_id, content, prefix=f"File `{arg}`:\n")
        except Exception as e:
            send_message(chat_id, f"cat error: {e}")
        return True
    if text_lower.startswith("get ") or text_lower.startswith("download ") or text_lower.startswith("send ") or text_lower.startswith("share ") or text_lower.startswith("fetch "):
        arg = orig_text.split(" ", 1)[1].strip().strip('\"\'') if " " in orig_text else ""
        if not arg:
            send_message(chat_id, "Usage: `get <file>` - sends file to chat")
            return True
        try:
            if not os.path.exists(arg):
                # try glob
                matches = glob.glob(arg)
                if matches:
                    for m in matches[:5]:
                        send_document(chat_id, m)
                    if len(matches) > 5:
                        send_message(chat_id, f"Sent 5 of {len(matches)} matches")
                else:
                    send_message(chat_id, f"File not found: `{arg}`")
                return True
            if os.path.isdir(arg):
                # zip directory and send
                clean = arg.rstrip('/\\')
                zip_name = f"{os.path.basename(clean)}.zip"
                shutil.make_archive(zip_name.replace('.zip',''), 'zip', arg)
                send_document(chat_id, zip_name.replace('.zip','') + '.zip')
                try: os.remove(zip_name.replace('.zip','') + '.zip')
                except: pass
                return True
            send_document(chat_id, arg)
        except Exception as e:
            send_message(chat_id, f"get error: {e}")
        return True
    if text_lower.startswith("mkdir "):
        arg = orig_text.split(" ", 1)[1].strip().strip('\"\'')
        try:
            os.makedirs(arg, exist_ok=True)
            send_message(chat_id, f"Created directory `{arg}`")
        except Exception as e:
            send_message(chat_id, f"mkdir error: {e}")
        return True
    if text_lower.startswith("touch "):
        arg = orig_text.split(" ", 1)[1].strip().strip('\"\'')
        try:
            pathlib.Path(arg).touch(exist_ok=True)
            send_message(chat_id, f"Touched `{arg}`")
        except Exception as e:
            send_message(chat_id, f"touch error: {e}")
        return True
    if text_lower.startswith("rm ") or text_lower.startswith("del ") or text_lower.startswith("remove "):
        arg = orig_text.split(" ", 1)[1].strip().strip('\"\'')
        if not arg:
            send_message(chat_id, "Usage: `rm <file_or_dir>`")
            return True
        # safety: prevent rm / or rm -rf /
        if arg.strip() in ("/", "C:\\", "C:/", ".", ".."):
            send_message(chat_id, "Refusing to delete that path")
            return True
        try:
            if os.path.isdir(arg) and not os.path.islink(arg):
                shutil.rmtree(arg)
                send_message(chat_id, f"Removed directory `{arg}`")
            elif os.path.exists(arg):
                os.remove(arg)
                send_message(chat_id, f"Removed `{arg}`")
            else:
                # glob?
                matches = glob.glob(arg)
                if matches:
                    for m in matches:
                        if os.path.isdir(m):
                            shutil.rmtree(m)
                        else:
                            os.remove(m)
                    send_message(chat_id, f"Removed {len(matches)} items matching `{arg}`")
                else:
                    send_message(chat_id, f"Not found: `{arg}`")
        except Exception as e:
            send_message(chat_id, f"rm error: {e}")
        return True
    if text_lower.startswith("rmdir "):
        arg = orig_text.split(" ", 1)[1].strip().strip('\"\'')
        try:
            os.rmdir(arg)
            send_message(chat_id, f"Removed directory `{arg}`")
        except Exception as e:
            send_message(chat_id, f"rmdir error: {e}")
        return True
    if text_lower.startswith("mv ") or text_lower.startswith("move "):
        parts = orig_text.split(" ", 2)
        if len(parts) < 3:
            # try split by space but allow quoted?
            send_message(chat_id, "Usage: `mv <src> <dst>`")
            return True
        # Better split preserving quotes? Simplistic.
        # Use shlex?
        import shlex
        try:
            toks = shlex.split(orig_text)
            src, dst = toks[1], toks[2]
        except:
            src, dst = parts[1], parts[2]
        try:
            shutil.move(src, dst)
            send_message(chat_id, f"Moved `{src}` -> `{dst}`")
        except Exception as e:
            send_message(chat_id, f"mv error: {e}")
        return True
    if text_lower.startswith("cp ") or text_lower.startswith("copy "):
        import shlex
        try:
            toks = shlex.split(orig_text)
            if len(toks) < 3:
                send_message(chat_id, "Usage: `cp <src> <dst>`")
                return True
            src, dst = toks[1], toks[2]
        except:
            parts = orig_text.split(" ", 2)
            src, dst = parts[1], parts[2]
        try:
            if os.path.isdir(src):
                shutil.copytree(src, dst, dirs_exist_ok=True)
            else:
                shutil.copy2(src, dst)
            send_message(chat_id, f"Copied `{src}` -> `{dst}`")
        except Exception as e:
            send_message(chat_id, f"cp error: {e}")
        return True
    if text_lower.startswith("write "):
        # write <file> <content> or write <file>\n<content>
        rest = orig_text[6:].strip()
        if not rest:
            send_message(chat_id, "Usage: `write <file> <content>`\nExample: `write notes.txt Hello world`")
            return True
        # split first token as file, rest as content
        # Use shlex to handle quoted filename?
        import shlex
        try:
            toks = shlex.split(rest)
            if len(toks) >= 1:
                fname = toks[0]
                # find fname position in rest to get content preserving spaces
                idx = rest.find(fname)
                content_start = idx + len(fname)
                content = rest[content_start:].lstrip()
                # If content was quoted, shlex already unquoted? Better reconstruct: if len(toks)>1 join remainder
                if not content and len(toks) > 1:
                    content = " ".join(toks[1:])
            else:
                send_message(chat_id, "Invalid write syntax")
                return True
        except:
            parts = rest.split(" ", 1)
            fname = parts[0]
            content = parts[1] if len(parts) > 1 else ""
        # handle \n escape
        content = content.replace("\\n", "\n")
        try:
            os.makedirs(os.path.dirname(fname) or ".", exist_ok=True)
            with open(fname, 'w', encoding='utf-8') as f:
                f.write(content)
            send_message(chat_id, f"Wrote {len(content)} chars to `{fname}`")
        except Exception as e:
            send_message(chat_id, f"write error: {e}")
        return True
    if text_lower.startswith("append "):
        rest = orig_text[7:].strip()
        if not rest:
            send_message(chat_id, "Usage: `append <file> <content>`")
            return True
        import shlex
        try:
            toks = shlex.split(rest)
            fname = toks[0]
            idx = rest.find(fname)
            content = rest[idx+len(fname):].lstrip()
            if not content and len(toks) > 1:
                content = " ".join(toks[1:])
        except:
            parts = rest.split(" ", 1)
            fname = parts[0]
            content = parts[1] if len(parts)>1 else ""
        content = content.replace("\\n", "\n")
        try:
            with open(fname, 'a', encoding='utf-8') as f:
                f.write(content)
            send_message(chat_id, f"Appended {len(content)} chars to `{fname}`")
        except Exception as e:
            send_message(chat_id, f"append error: {e}")
        return True
    if text_lower.startswith("edit "):
        # show file for editing hint
        arg = orig_text.split(" ", 1)[1].strip().strip('\"\'')
        try:
            with open(arg, 'r', encoding='utf-8', errors='ignore') as f:
                content = f.read(4000)
            send_message(chat_id, f"Editing `{arg}` - current content (first 4000 chars):\n{TICK}\n{content}\n{TICK}\nUse `write {arg} <new content>` to overwrite.")
        except Exception as e:
            send_message(chat_id, f"edit error: {e}")
        return True
    if text_lower.startswith("find "):
        # find <pattern> [path]
        rest = orig_text[5:].strip()
        if not rest:
            send_message(chat_id, "Usage: `find <pattern> [path]` e.g. `find *.py .`")
            return True
        parts = rest.split()
        pattern = parts[0]
        path = parts[1] if len(parts) > 1 else "."
        try:
            matches = []
            for root, dirs, files in os.walk(path):
                for f in files:
                    if glob.fnmatch.fnmatch(f, pattern) or pattern.lower() in f.lower():
                        matches.append(os.path.join(root, f))
                        if len(matches) >= 100:
                            break
                if len(matches) >= 100:
                    break
            if matches:
                send_large_text(chat_id, "\n".join(matches), prefix=f"Found {len(matches)} matches for `{pattern}` in `{path}`:\n")
            else:
                send_message(chat_id, f"No matches for `{pattern}` in `{path}`")
        except Exception as e:
            send_message(chat_id, f"find error: {e}")
        return True
    if text_lower.startswith("tree"):
        arg = orig_text[4:].strip().strip('\"\'') or "."
        try:
            lines = []
            max_depth = 3
            # Check if arg includes depth e.g. "tree . 2"
            parts = arg.split()
            path = parts[0] if parts else "."
            if len(parts) > 1 and parts[1].isdigit():
                max_depth = int(parts[1])
                path = parts[0]
            elif arg.isdigit():
                max_depth = int(arg)
                path = "."
            else:
                path = arg if arg else "."
            def walk(p, prefix="", depth=0):
                if depth > max_depth:
                    return
                try:
                    items = sorted(os.listdir(p))
                except:
                    return
                for i, it in enumerate(items[:80]):
                    is_last = i == len(items)-1
                    full = os.path.join(p, it)
                    connector = "└── " if is_last else "├── "
                    lines.append(prefix + connector + it + ("/" if os.path.isdir(full) else ""))
                    if os.path.isdir(full) and depth < max_depth:
                        extension = "    " if is_last else "│   "
                        walk(full, prefix+extension, depth+1)
                    if len(lines) >= 200:
                        break
            lines.append(path)
            walk(path)
            send_large_text(chat_id, "\n".join(lines[:200]))
        except Exception as e:
            send_message(chat_id, f"tree error: {e}")
        return True
    if text_lower in ("du", "disk", "df", "disk usage") or text_lower.startswith("du ") or text_lower.startswith("df ") or text_lower.startswith("du\t"):
        arg = ""
        if " " in text_lower:
            arg = orig_text.split(" ",1)[1].strip().strip('\"\'')
        target = arg if arg else "."
        try:
            total, used, free = shutil.disk_usage(target if os.path.exists(target) else ".")
            def fmt(b): 
                for unit in ['B','KB','MB','GB','TB']:
                    if b < 1024: return f"{b:.1f}{unit}"
                    b/=1024
                return f"{b:.1f}PB"
            info = f"Disk usage for `{os.path.abspath(target)}`:\nTotal: {fmt(total)}\nUsed: {fmt(used)}\nFree: {fmt(free)}"
            # also cwd size?
            try:
                size = 0
                for dirpath, dirnames, filenames in os.walk(target if os.path.isdir(target) else "."):
                    for f in filenames:
                        fp = os.path.join(dirpath, f)
                        try: size+=os.path.getsize(fp)
                        except: pass
                    if size > 500*1024*1024: break
                info += f"\nFolder size: {fmt(size)}"
            except: pass
            send_message(chat_id, info)
        except Exception as e:
            send_message(chat_id, f"du error: {e}")
        return True
    if text_lower.startswith("zip "):
        rest = orig_text[4:].strip()
        if not rest:
            send_message(chat_id, "Usage: `zip <src> [dst.zip]`")
            return True
        import shlex
        try:
            toks = shlex.split(rest)
            src = toks[0]
            dst = toks[1] if len(toks)>1 else (src.rstrip('/\\') + ".zip")
        except:
            parts = rest.split()
            src = parts[0]
            dst = parts[1] if len(parts)>1 else src + ".zip"
        try:
            if os.path.isdir(src):
                shutil.make_archive(dst.replace('.zip',''), 'zip', os.path.dirname(src) or ".", os.path.basename(src))
            else:
                with zipfile.ZipFile(dst, 'w') as z:
                    z.write(src)
            send_message(chat_id, f"Zipped `{src}` -> `{dst}` ({os.path.getsize(dst)} bytes)")
            # optionally send?
        except Exception as e:
            send_message(chat_id, f"zip error: {e}")
        return True
    if text_lower.startswith("unzip "):
        rest = orig_text[6:].strip()
        if not rest:
            send_message(chat_id, "Usage: `unzip <archive.zip> [dst_dir]`")
            return True
        import shlex
        try:
            toks = shlex.split(rest)
            src = toks[0]
            dst = toks[1] if len(toks)>1 else "."
        except:
            parts = rest.split()
            src = parts[0]
            dst = parts[1] if len(parts)>1 else "."
        try:
            with zipfile.ZipFile(src, 'r') as z:
                z.extractall(dst)
            send_message(chat_id, f"Unzipped `{src}` -> `{dst}`")
        except Exception as e:
            send_message(chat_id, f"unzip error: {e}")
        return True
    if text_lower.startswith("wget ") or text_lower.startswith("curl ") or text_lower.startswith("download url "):
        # wget <url> [outfile]
        rest = orig_text.split(" ",1)[1].strip() if " " in orig_text else ""
        if not rest:
            send_message(chat_id, "Usage: `wget <url> [output_file]`")
            return True
        parts = rest.split()
        url = parts[0]
        out = parts[1] if len(parts)>1 else os.path.basename(url.split("?")[0]) or "downloaded_file"
        out = out.strip('\"\'')
        try:
            send_message(chat_id, f"Downloading `{url}` -> `{out}`...")
            r = requests.get(url, stream=True, timeout=60)
            r.raise_for_status()
            total = 0
            with open(out, 'wb') as f:
                for chunk in r.iter_content(chunk_size=8192):
                    if chunk:
                        f.write(chunk)
                        total += len(chunk)
            send_message(chat_id, f"Downloaded `{out}` ({total} bytes)")
        except Exception as e:
            send_message(chat_id, f"wget error: {e}")
        return True
    if text_lower.startswith("cd "):
        arg = orig_text[3:].strip().strip('\"\'')
        if not arg:
            arg = os.path.expanduser("~")
        try:
            os.chdir(arg)
            send_message(chat_id, f"CWD: `{os.getcwd()}`")
        except Exception as e:
            send_message(chat_id, f"cd error: {e}")
        return True
    return False

def handle_system_commands(chat_id, orig_text, text_lower):
    if text_lower in ("ps", "tasks", "processes", "top", "tasklist"):
        try:
            # try psutil first
            lines = []
            try:
                import psutil
                procs = list(psutil.process_iter(['pid','name','cpu_percent','memory_info']))
                # sort by memory
                procs = sorted(procs, key=lambda p: (p.info['memory_info'].rss if p.info['memory_info'] else 0), reverse=True)[:40]
                for p in procs:
                    try:
                        rss = p.info['memory_info'].rss // (1024*1024) if p.info['memory_info'] else 0
                        lines.append(f"{p.info['pid']:>6} {rss:>5}MB {p.info['name']}")
                    except: pass
                header = f"Top processes ({len(procs)}):\nPID   MEM   NAME\n"
                send_large_text(chat_id, header + "\n".join(lines))
                return True
            except:
                pass
            # fallback to tasklist / ps
            if os.name == 'nt':
                out = subprocess.check_output("tasklist", shell=True, text=True, timeout=10)
            else:
                out = subprocess.check_output("ps aux | head -n 50", shell=True, text=True, timeout=10)
            send_large_text(chat_id, out)
        except Exception as e:
            send_message(chat_id, f"ps error: {e}")
        return True
    if text_lower.startswith("kill "):
        arg = orig_text[5:].strip()
        if not arg:
            send_message(chat_id, "Usage: `kill <pid_or_name>`")
            return True
        try:
            # try pid numeric
            if arg.isdigit():
                pid = int(arg)
                if os.name == 'nt':
                    subprocess.run(f"taskkill /PID {pid} /F", shell=True)
                else:
                    os.kill(pid, signal.SIGTERM)
                send_message(chat_id, f"Killed PID {pid}")
            else:
                # kill by name
                if os.name == 'nt':
                    subprocess.run(f'taskkill /IM \"{arg}\" /F', shell=True)
                else:
                    subprocess.run(f"pkill -f {arg}", shell=True)
                send_message(chat_id, f"Killed `{arg}`")
        except Exception as e:
            send_message(chat_id, f"kill error: {e}")
        return True
    if text_lower.startswith("killall "):
        arg = orig_text[8:].strip()
        try:
            if os.name == 'nt':
                subprocess.run(f'taskkill /IM \"{arg}\" /F', shell=True)
            else:
                subprocess.run(f"pkill -f {arg}", shell=True)
            send_message(chat_id, f"Killed all `{arg}`")
        except Exception as e:
            send_message(chat_id, f"killall error: {e}")
        return True
    if text_lower in ("sysinfo", "info", "stats", "status", "system", "neofetch", "sys"):
        try:
            info = []
            info.append(f"System: {platform.system()} {platform.release()} {platform.version()}")
            info.append(f"Machine: {platform.machine()} {platform.processor()}")
            info.append(f"Python: {platform.python_version()} | CWD: {os.getcwd()}")
            info.append(f"Hostname: {platform.node()} | User: {os.environ.get('USERNAME') or os.environ.get('USER')}")
            try:
                import psutil
                mem = psutil.virtual_memory()
                info.append(f"RAM: {mem.total//(1024*1024)}MB total, {mem.available//(1024*1024)}MB avail ({mem.percent}% used)")
                info.append(f"CPU: {psutil.cpu_percent(interval=1)}% | Cores: {psutil.cpu_count()}")
                disk = psutil.disk_usage('.')
                info.append(f"Disk: {disk.total//(1024*1024*1024)}GB total, {disk.free//(1024*1024*1024)}GB free ({disk.percent}% used)")
                boot = time.ctime(psutil.boot_time())
                info.append(f"Boot: {boot}")
            except:
                pass
            try:
                total, used, free = shutil.disk_usage(".")
                info.append(f"Disk (shutil): {total//(1024*1024*1024)}GB total {free//(1024*1024*1024)}GB free")
            except: pass
            info.append(f"Env PATH entries: {len(os.environ.get('PATH','').split(os.pathsep))}")
            send_large_text(chat_id, "\n".join(info))
        except Exception as e:
            send_message(chat_id, f"sysinfo error: {e}")
        return True
    if text_lower.startswith("env"):
        arg = orig_text[3:].strip()
        try:
            if arg:
                val = os.environ.get(arg) or os.environ.get(arg.upper()) or "(not set)"
                send_message(chat_id, f"`{arg}` = `{val}`")
            else:
                lines = [f"{k}={v}" for k,v in sorted(os.environ.items())]
                send_large_text(chat_id, "\n".join(lines), prefix="Environment:\n")
        except Exception as e:
            send_message(chat_id, f"env error: {e}")
        return True
    if text_lower in ("uptime", "up", "boot time"):
        try:
            import psutil
            boot = psutil.boot_time()
            up = time.time() - boot
            hrs = up // 3600
            mins = (up % 3600)//60
            send_message(chat_id, f"Uptime: {int(hrs)}h {int(mins)}m | Boot: {time.ctime(boot)}")
        except:
            try:
                out = subprocess.check_output("uptime", shell=True, text=True, timeout=5)
                send_message(chat_id, out)
            except Exception as e:
                send_message(chat_id, f"uptime error: {e}")
        return True
    if text_lower in ("whoami", "hostname", "date", "time", "id"):
        try:
            if text_lower == "whoami":
                out = subprocess.check_output("whoami", shell=True, text=True, timeout=5)
                send_message(chat_id, f"`{out.strip()}`")
            elif text_lower == "hostname":
                send_message(chat_id, f"`{platform.node()}`")
            elif text_lower in ("date", "time"):
                send_message(chat_id, f"`{time.ctime()}`")
            else:
                out = subprocess.check_output(text_lower, shell=True, text=True, timeout=5)
                send_large_text(chat_id, out)
        except Exception as e:
            send_message(chat_id, f"error: {e}")
        return True
    if text_lower.startswith("ip ") or text_lower in ("ip", "ipconfig", "ifconfig", "network"):
        try:
            if os.name == 'nt':
                out = subprocess.check_output("ipconfig", shell=True, text=True, timeout=10)
            else:
                out = subprocess.check_output("ip addr; echo ---; hostname -I", shell=True, text=True, timeout=10)
            send_large_text(chat_id, out)
        except Exception as e:
            send_message(chat_id, f"network error: {e}")
        return True
    if text_lower in ("reboot", "restart", "shutdown"):
        send_message(chat_id, f"`{text_lower}` requested - not executed automatically for safety. Use `/shutdown /r /t 0` or `/reboot` via shell if needed.")
        return True
    return False

def handle_window_commands(chat_id, orig_text, text_lower):
    # apps already handled elsewhere, but keep here for unified
    if text_lower == "apps":
        ui_automation(chat_id, "available_apps", "list")
        return True
    if text_lower in ("opened apps", "opened", "windows", "winlist", "tasklist windows", "list windows"):
        # Enhanced window list via pyautogui + uiautomation
        try:
            # Try uiautomation first
            if os.name == 'nt':
                import uiautomation as auto
                root = auto.GetRootControl()
                wins = [w.Name for w in root.GetChildren() if w.Name]
                if wins:
                    send_message(chat_id, "Opened Apps:\\n" + "\\n".join(wins[:80]))
                    return True
        except: pass
        try:
            import pyautogui
            titles = pyautogui.getAllTitles() if hasattr(pyautogui, 'getAllTitles') else []
            if titles:
                filtered = [t for t in titles if t.strip()]
                send_message(chat_id, "Windows:\\n" + "\\n".join(filtered[:80]))
                return True
        except: pass
        ui_automation(chat_id, "opened_apps")
        return True
    if text_lower.startswith("open "):
        ui_automation(chat_id, "available_apps", orig_text[5:].strip())
        return True
    if text_lower.startswith("browser ") or text_lower.startswith("openurl ") or text_lower.startswith("open url "):
        # browser <url>
        arg = orig_text.split(" ",1)[1].strip() if " " in orig_text else ""
        if not arg:
            send_message(chat_id, "Usage: `browser <url>` e.g. `browser https://google.com`")
            return True
        if not arg.startswith("http"):
            arg = "https://" + arg
        try:
            import webbrowser
            webbrowser.open(arg)
            send_message(chat_id, f"Opened browser `{arg}`")
        except:
            try:
                if os.name == 'nt':
                    os.startfile(arg)
                else:
                    subprocess.Popen(["xdg-open", arg])
                send_message(chat_id, f"Opened `{arg}`")
            except Exception as e:
                send_message(chat_id, f"browser error: {e}")
        return True
    if text_lower.startswith("focus ") or text_lower.startswith("activate ") or text_lower.startswith("switch "):
        target = orig_text.split(" ",1)[1].strip() if " " in orig_text else ""
        if not target:
            send_message(chat_id, "Usage: `focus <window name>`")
            return True
        try:
            found = False
            if os.name == 'nt':
                import uiautomation as auto
                root = auto.GetRootControl()
                def find_and_focus(ctrl, name):
                    nonlocal found
                    if found: return
                    if name.lower() in (ctrl.Name or "").lower() and ctrl.ControlTypeName == "WindowControl":
                        try:
                            ctrl.SetFocus()
                            found = True
                            return
                        except: pass
                    for child in ctrl.GetChildren():
                        find_and_focus(child, name)
                        if found: return
                find_and_focus(root, target)
            if not found:
                # try pyautogui getWindows
                try:
                    import pyautogui
                    wins = pyautogui.getAllWindows() if hasattr(pyautogui, 'getAllWindows') else []
                    for w in wins:
                        if target.lower() in w.title.lower():
                            w.activate()
                            found = True
                            break
                except: pass
            if found:
                send_message(chat_id, f"Focused `{target}`")
            else:
                send_message(chat_id, f"Window `{target}` not found. Use `windows` to list.")
        except Exception as e:
            send_message(chat_id, f"focus error: {e}")
        return True
    if text_lower.startswith("close ") or text_lower.startswith("killwindow "):
        target = orig_text.split(" ",1)[1].strip() if " " in orig_text else ""
        try:
            # try Alt+F4 if focused window matches, else find window and close via uiautomation
            if target:
                # try to focus then close
                handle_window_commands(chat_id, f"focus {target}", f"focus {target.lower()}")
                time.sleep(0.5)
            pyautogui = _ensure_pyautogui()
            if pyautogui:
                pyautogui.hotkey('alt', 'f4')
                send_message(chat_id, f"Sent Alt+F4 to close `{target or 'active window'}`")
            else:
                send_message(chat_id, "pyautogui not available")
        except Exception as e:
            send_message(chat_id, f"close error: {e}")
        return True
    if text_lower in ("minimize", "maximize", "min", "max"):
        try:
            pyautogui = _ensure_pyautogui()
            if not pyautogui:
                return True
            if text_lower in ("minimize", "min"):
                pyautogui.hotkey('win', 'down')
                send_message(chat_id, "Minimized (Win+Down)")
            else:
                pyautogui.hotkey('win', 'up')
                send_message(chat_id, "Maximized (Win+Up)")
        except Exception as e:
            send_message(chat_id, f"window error: {e}")
        return True
    if text_lower.startswith("minimize ") or text_lower.startswith("maximize "):
        # minimize window <name>
        send_message(chat_id, "Tip: `minimize`/`maximize` works on active window. First `focus <window>` then `minimize`.")
        # still try
        try:
            pyautogui = _ensure_pyautogui()
            action = "minimize" if text_lower.startswith("minimize") else "maximize"
            target = orig_text.split(" ",1)[1].strip()
            # focus then action
            if target:
                handle_window_commands(chat_id, f"focus {target}", f"focus {target.lower()}")
                time.sleep(0.5)
            if pyautogui:
                if action == "minimize":
                    pyautogui.hotkey('win', 'down')
                else:
                    pyautogui.hotkey('win', 'up')
                send_message(chat_id, f"{action.capitalize()}d `{target}`")
        except Exception as e:
            send_message(chat_id, f"error: {e}")
        return True
    return False

# ---------- UI Automation (original extended) ----------
def ui_automation(chat_id, action, params=None):
    if os.name != 'nt':
        send_message(chat_id, "UI Automation only supported on Windows.")
        return
    try:
        import uiautomation as auto
        import pyautogui
        root = auto.GetRootControl()

        if action == "opened_apps":
            wins = [w.Name for w in root.GetChildren() if w.Name]
            send_message(chat_id, "Opened Apps:\\n" + "\\n".join(wins[:80]) if wins else "No windows found")
            return

        elif action == "available_apps":
            apps = {}
            try:
                for path in [os.path.join(os.environ['USERPROFILE'], 'Desktop'), r'C:\\Users\\Public\\Desktop']:
                    if os.path.exists(path):
                        for f in os.listdir(path):
                            if f.endswith('.lnk'):
                                apps[f.replace('.lnk', '').lower()] = os.path.join(path, f)
                for path in [os.path.join(os.environ.get('AppData', ''), r'Microsoft\\Windows\\Start Menu\\Programs'),
                             r'C:\\ProgramData\\Microsoft\\Windows\\Start Menu\\Programs']:
                    if os.path.exists(path):
                        for root_dir, dirs, files in os.walk(path):
                            for f in files:
                                if f.endswith('.lnk'):
                                    apps[f.replace('.lnk', '').lower()] = os.path.join(root_dir, f)
            except:
                pass
            if params == "list":
                res = sorted([k.capitalize() for k in apps.keys()])
                header = "Available Apps (first 100):\\n"
                body = "\\n".join(res[:100]) if res else "None found"
                # also add common system apps
                body += "\\n\\nTip: `open <app>` e.g. `open notepad`, `open chrome`"
                send_message(chat_id, header + body)
            else:
                target = params.lower()
                match = apps.get(target)
                if not match:
                    common = {"paint": "mspaint", "notepad": "notepad", "calc": "calc", "cmd": "cmd", "explorer": "explorer", "chrome": "chrome", "edge": "msedge", "firefox": "firefox", "code": "code", "vscode": "code", "word": "winword", "excel": "excel"}
                    match = common.get(target)
                if match:
                    try:
                        os.startfile(match) if os.path.exists(match) else subprocess.Popen(match, shell=True)
                        send_message(chat_id, f"Opening `{params}`...")
                    except Exception as e:
                        try: subprocess.Popen(match, shell=True); send_message(chat_id, f"Opening `{params}`...")
                        except Exception as e2: send_message(chat_id, f"Failed to open `{params}`: {e2}")
                else:
                    # try start via shell directly
                    try:
                        subprocess.Popen(params, shell=True)
                        send_message(chat_id, f"Trying to open `{params}` via shell...")
                    except:
                        send_message(chat_id, f"Could not find app `{params}`. Try `apps` to list.")

        elif action == "list_buttons":
            curr_win = auto.GetForegroundControl()
            while curr_win and curr_win.ControlTypeName != "WindowControl":
                curr_win = curr_win.GetParentControl()
            if not curr_win:
                send_message(chat_id, "No active window found. Click on a window first.")
                return
            controls = []
            def find_controls(ctrl, depth=0):
                if depth > 8:
                    return
                interactive_types = ["ButtonControl", "MenuItemControl", "ListItemControl", "TreeItemControl", "TabItemControl", "HyperlinkControl", "SplitButtonControl", "CheckBoxControl", "RadioButtonControl", "EditControl", "ComboBoxControl"]
                if ctrl.ControlTypeName in interactive_types:
                    if ctrl.Name:
                        controls.append(f"{ctrl.ControlTypeName[:-7]}: {ctrl.Name}")
                if len(controls) > 200:
                    return
                for child in ctrl.GetChildren():
                    find_controls(child, depth + 1)
            find_controls(curr_win)
            unique_controls = sorted(list(set(controls)))
            res = "\\n".join(unique_controls[:100])
            send_message(chat_id, f"Controls in `{curr_win.Name}` ({len(unique_controls)}):\\n" + (res or "No controls found. Try clicking a different window."))

        elif action == "click" or action == "double_click":
            def perform_action(ctrl, target):
                if target.lower() in (ctrl.Name or "").lower():
                    try:
                        if action == "click":
                            ctrl.Click()
                        else:
                            ctrl.DoubleClick()
                        return True
                    except: pass
                for child in ctrl.GetChildren():
                    if perform_action(child, target):
                        return True
                return False
            if perform_action(root, params):
                send_message(chat_id, f"{'Clicked' if action == 'click' else 'Double-clicked'} `{params}`")
            else:
                send_message(chat_id, f"Could not find control `{params}`. Use `buttons` to list, or `click 100 200` for coordinates.")

        elif action == "press":
            try:
                pyautogui.hotkey(*params.split("+"))
                send_message(chat_id, f"Pressed `{params}`")
            except Exception as e:
                send_message(chat_id, f"Press error: {e}")

        elif action == "type":
            try:
                pyautogui.write(params)
                send_message(chat_id, f"Typed `{params}`")
            except Exception as e:
                send_message(chat_id, f"Type error: {e}")

    except Exception as e:
        send_message(chat_id, f"UI Error: {e}")

# ---------- Livestream + automatic PhoneFS ----------
def _find_cloudflared():
    for candidate in ("cloudflared", "cloudflared.exe", r"C:\Windows\System32\cloudflared.exe",
                      "/usr/local/bin/cloudflared", "/usr/bin/cloudflared"):
        found = shutil.which(candidate)
        if found:
            return found
        if os.path.isfile(candidate):
            return candidate
    return "cloudflared"


def _is_port_open(port=5000, host="127.0.0.1", timeout=1):
    import socket
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


phonefs_service = PhoneFSService(
    notify=lambda chat_id, text: send_message(chat_id, text, parse_mode=None),
    cloudflared=_find_cloudflared,
    desktop_url=lambda: livestream_url,
)


def _remote_chat_allowed(chat_id, claim=False):
    """Never give an active session's links/password to a different chat."""
    global livestream_chat_id
    with livestream_lock:
        allowed = livestream_chat_id is None or livestream_chat_id == chat_id
        if allowed and claim:
            livestream_chat_id = chat_id
    if not allowed:
        send_message(chat_id, "Remote access is controlled by another Telegram chat; links and credentials are private.", parse_mode=None)
    return allowed


def _start_desktop_tunnel(chat_id, stop_event):
    global livestream_tunnel_thread
    with livestream_lock:
        if stop_event.is_set() or stop_event is not livestream_stop_event:
            return
        if livestream_tunnel_thread and livestream_tunnel_thread.is_alive():
            return
        livestream_tunnel_thread = threading.Thread(
            target=run_tunnel_with_autorestart, args=(chat_id, stop_event),
            daemon=True, name="Screen tunnel",
        )
        livestream_tunnel_thread.start()


def run_tunnel_with_autorestart(chat_id, stop_event):
    """One supervisor per screen session; old threads cannot restart a new session."""
    global livestream_proc, livestream_url
    import queue
    attempt = 0
    while not stop_event.is_set():
        attempt += 1
        process = None
        logs = []
        url_sent = False
        try:
            for _ in range(15):
                if stop_event.is_set():
                    return
                if _is_port_open(5000):
                    break
                stop_event.wait(1)
            else:
                raise RuntimeError("Screen web server is not responding on port 5000")

            cfd = _find_cloudflared()
            origin = "http://127.0.0.1:5000"
            # Preserve the previously working plain command first; use HTTP/2
            # on retries for networks that block QUIC.
            commands = [
                [cfd, "tunnel", "--url", origin],
                [cfd, "tunnel", "--url", "http://localhost:5000"],
                [cfd, "tunnel", "--url", origin, "--protocol", "http2"],
                [cfd, "tunnel", "--url", origin, "--loglevel", "debug"],
                [cfd, "tunnel", "--url", origin, "--no-autoupdate"],
            ]
            command = commands[(attempt - 1) % len(commands)]
            env = child_environment()
            env.update(TUNNEL_ORIGIN_CERT="", FORCE_COLOR="0", CLOUDFLARED_NO_AUTOUPDATE="1")
            with livestream_lock:
                if stop_event.is_set() or stop_event is not livestream_stop_event:
                    return
                print(f"[screen tunnel] Starting: {' '.join(command)}", flush=True)
                process = subprocess.Popen(
                    command, stdin=subprocess.DEVNULL,
                    stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                    text=True, encoding="utf-8", errors="replace", bufsize=1,
                    env=env, **process_options(),
                )
                livestream_proc = process
                livestream_url = None

            output = queue.Queue()

            def pump(stream, output_queue):
                try:
                    for line in stream:
                        output_queue.put(line)
                finally:
                    output_queue.put(None)

            threading.Thread(target=pump, args=(process.stdout, output), daemon=True).start()
            deadline = time.monotonic() + 40
            while not stop_event.is_set():
                try:
                    line = output.get(timeout=0.5)
                except queue.Empty:
                    line = None
                if line:
                    clean = re.sub(r'\x1b\[[0-?]*[ -/]*[@-~]', '', line.strip())
                    logs.append(clean)
                    logs = logs[-30:]
                    # The screen URL itself grants control; raw cloudflared
                    # output belongs only in the owning chat's diagnostics.
                    match = re.search(r'https://[a-zA-Z0-9-]+\.trycloudflare\.com', clean)
                    if match and not url_sent and process.poll() is None:
                        with livestream_lock:
                            if stop_event.is_set() or stop_event is not livestream_stop_event:
                                return
                            livestream_url = match.group(0)
                            url_sent = True
                            print("[screen tunnel] URL obtained; notifying the owning Telegram chat", flush=True)
                            send_message(chat_id,
                                f"Screen sharing online (local port 5000): {livestream_url}\n"
                                "Open on phone/PC for full mouse and keyboard control.\n"
                                "If the page initially shows 502, wait a few seconds and refresh.\n\n"
                                + phonefs_service.status_text(chat_id), parse_mode=None)
                code = process.poll()
                if code is not None:
                    raise RuntimeError(f"Screen tunnel disconnected (exit {code})")
                if not url_sent and time.monotonic() >= deadline:
                    raise RuntimeError("No screen tunnel URL after 40 seconds")
        except Exception as exc:
            if not stop_event.is_set():
                detail = "\n".join(logs)[-1800:]
                send_message(chat_id,
                    f"Screen tunnel: {exc}. Retrying (attempt {attempt}).\n{detail}\n"
                    "PhoneFS is independent. Use livestream status for both services.", parse_mode=None)
        finally:
            terminate_process_tree(process)
            if process and process.stdout:
                process.stdout.close()
            with livestream_lock:
                if stop_event is livestream_stop_event:
                    livestream_proc = None
                    livestream_url = None
        if stop_event.wait(5):
            return


def livestream_status(chat_id):
    if not _remote_chat_allowed(chat_id):
        return True
    port_ok = _is_port_open(5000)
    proc_ok = livestream_proc is not None and livestream_proc.poll() is None
    send_message(chat_id,
        f"Screen sharing status:\n- Active: {livestream_active}\n"
        f"- Web server: {livestream_flask_started} (port 5000 open={port_ok})\n"
        f"- Tunnel alive: {proc_ok}\n"
        f"- Screen URL: {livestream_url or ('connecting' if livestream_active else 'stopped')}\n"
        f"- Screen: {SCREEN_W}x{SCREEN_H}\n\n"
        + phonefs_service.status_text(chat_id), parse_mode=None)
    return True


def _serialize_remote_operation(function):
    @functools.wraps(function)
    def serialized(*args, **kwargs):
        # Telegram commands and the browser's stop button can arrive together.
        # Serialize whole operations without holding the state lock while
        # joining a supervisor that needs that state lock for its cleanup.
        with remote_operation_lock:
            return function(*args, **kwargs)
    return serialized


@_serialize_remote_operation
def livestream_stop(chat_id=None, notify=True):
    global livestream_active, livestream_url, livestream_proc, livestream_tunnel_thread
    if chat_id is not None and not _remote_chat_allowed(chat_id):
        return False
    with livestream_lock:
        livestream_active = False
        if livestream_stop_event:
            livestream_stop_event.set()
        process, thread = livestream_proc, livestream_tunnel_thread
        livestream_url = None
    terminate_process_tree(process)
    if thread and thread is not threading.current_thread():
        thread.join(timeout=5)
    with livestream_lock:
        livestream_proc = None
        livestream_tunnel_thread = None
        # Keep the existing web server for reuse. Starting another Flask server
        # on port 5000 after stop/restart used to cause an address-in-use error.
    phonefs_service.stop(chat_id)
    if notify and chat_id is not None:
        send_message(chat_id, "Screen sharing and PhoneFS stopped; both public tunnels are closed.", parse_mode=None)
    return True


@_serialize_remote_operation
def livestream_restart(chat_id):
    if not _remote_chat_allowed(chat_id):
        return True
    send_message(chat_id, "Restarting screen sharing + PhoneFS; a new PhoneFS password will be generated.", parse_mode=None)
    livestream_stop(chat_id, notify=False)
    livestream(chat_id)
    return True


def shutdown_remote_access():
    livestream_stop(notify=False)
    if livestream_server:
        livestream_server.shutdown()
        livestream_server.server_close()


@_serialize_remote_operation
def livestream(chat_id):
    global livestream_url, livestream_active, livestream_stop_event
    if not _remote_chat_allowed(chat_id, claim=True):
        return
    # PhoneFS setup is asynchronous and must not delay the working desktop.
    # Repeating livestream retries a failed/stopped PhoneFS session without
    # replacing a healthy screen tunnel or creating duplicate listeners.
    phonefs_service.start(chat_id)
    with livestream_lock:
        if livestream_active:
            message = (f"Screen sharing already running: {livestream_url}" if livestream_url
                       else "Screen sharing is starting; wait for its link.")
            stop_event = None
        else:
            livestream_stop_event = threading.Event()
            stop_event = livestream_stop_event
            livestream_active = True
            livestream_url = None
            message = "Starting screen sharing on port 5000 alongside PhoneFS on its own port..."
        reuse_server = livestream_flask_started and livestream_server is not None
    send_message(chat_id, message, parse_mode=None)
    if stop_event is None:
        return
    if reuse_server:
        _start_desktop_tunnel(chat_id, stop_event)
        return

    def run_server():
        global livestream_active, livestream_flask_started, livestream_server, SCREEN_W, SCREEN_H
        try:
            from flask import Flask, Response, request, jsonify
            from werkzeug.serving import make_server
            import numpy as np
            import cv2
            import mss

            app = Flask(__name__)
            # suppress flask logs
            import logging
            log = logging.getLogger('werkzeug')
            log.setLevel(logging.ERROR)

            # detect screen size once
            try:
                with mss.mss() as sct:
                    mon = sct.monitors[1] if len(sct.monitors) > 1 else sct.monitors[0]
                    SCREEN_W = mon["width"]
                    SCREEN_H = mon["height"]
            except:
                pass

            def capture_jpeg(width=854, quality=35):
                monitor = mss.mss().monitors[1] if len(mss.mss().monitors) > 1 else mss.mss().monitors[0]
                # we use fresh mss each time to avoid thread issues
                with mss.mss() as sct:
                    monitor = sct.monitors[1] if len(sct.monitors) > 1 else sct.monitors[0]
                    sct_img = sct.grab(monitor)
                    frame = np.array(sct_img)
                    frame = cv2.cvtColor(frame, cv2.COLOR_BGRA2BGR)
                    h, w = frame.shape[:2]
                    new_h = int(h * (width / w))
                    frame_resized = cv2.resize(frame, (width, new_h))
                    _, buffer = cv2.imencode(".jpg", frame_resized, [int(cv2.IMWRITE_JPEG_QUALITY), quality])
                    return buffer.tobytes(), w, h

            def gen_frames():
                while True:
                    try:
                        with mss.mss() as sct:
                            monitor = sct.monitors[1] if len(sct.monitors) > 1 else sct.monitors[0]
                            sct_img = sct.grab(monitor)
                            frame = np.array(sct_img)
                            frame = cv2.cvtColor(frame, cv2.COLOR_BGRA2BGR)
                            h, w = frame.shape[:2]
                            new_w = 960
                            new_h = int(h * (new_w / w))
                            frame_resized = cv2.resize(frame, (new_w, new_h))
                            _, buffer = cv2.imencode(".jpg", frame_resized, [int(cv2.IMWRITE_JPEG_QUALITY), 40])
                            frame_bytes = buffer.tobytes()
                            yield (b'--frame\r\n'
                                   b'Content-Type: image/jpeg\r\n'
                                   b'Content-Length: ' + str(len(frame_bytes)).encode() + b'\r\n\r\n' +
                                   frame_bytes + b'\r\n')
                        time.sleep(0.12)
                    except Exception as e:
                        print(f"Frame gen error: {e}")
                        time.sleep(0.5)

            @app.route("/")
            def index():
                html = """<!DOCTYPE html>
<html>
<head>
  <title>GitHub VM Remote Desktop</title>
  <meta name="viewport" content="width=device-width, initial-scale=1, maximum-scale=1, user-scalable=no">
  <style>
    *{box-sizing:border-box;margin:0;padding:0}
    body{background:#0a0a0a;color:#eee;font-family: -apple-system,BlinkMacSystemFont,Segoe UI,Roboto,Helvetica,Arial,sans-serif; min-height:100vh; display:flex; flex-direction:column}
    header{background:#1a1a1a; padding:8px 12px; display:flex; align-items:center; justify-content:space-between; position:sticky; top:0; z-index:10; border-bottom:1px solid #333}
    header h1{font-size:14px; font-weight:600}
    header .status{font-size:11px; color:#0f0}
    #toolbar{background:#222; padding:6px; display:flex; gap:6px; flex-wrap:wrap; align-items:center; border-bottom:1px solid #333}
    #toolbar button, #toolbar input{font-size:12px; padding:6px 10px; border-radius:6px; border:1px solid #444; background:#333; color:#fff}
    #toolbar button:active{background:#555}
    #toolbar input{background:#111; min-width:140px; flex:1}
    #viewport{flex:1; display:flex; align-items:center; justify-content:center; background:#000; position:relative; overflow:hidden; touch-action:none}
    #screen{max-width:100%; max-height: calc(100vh - 140px); width:auto; height:auto; display:block; cursor:crosshair; user-select:none; -webkit-user-drag:none}
    #overlay{position:absolute; inset:0; pointer-events:none; display:flex; align-items:flex-start; justify-content:center; padding-top:6px}
    #fps{font:11px monospace; background:rgba(0,0,0,0.7); color:#0f0; padding:3px 6px; border-radius:4px}
    .keys{display:flex; gap:4px; flex-wrap:wrap}
    .keys button{padding:5px 8px; font-size:11px}
    .hint{font-size:10px; color:#888; padding:6px; text-align:center; background:#111}
    .sep{width:1px; height:22px; background:#444; margin:0 2px}
  </style>
</head>
<body>
  <header>
    <h1>🖥️ GitHub VM Remote</h1>
    <div class="status" id="st">Connecting...</div>
  </header>
  <div id="toolbar">
    <input id="typeText" placeholder="Type text here & Send" />
    <button onclick="sendType()">Send (Paste)</button>
    <button onclick="sendKey('enter')">Enter</button>
    <button onclick="sendKey('backspace')">⌫</button>
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
    <button onclick="sendKey('up')">↑</button>
    <button onclick="sendKey('down')">↓</button>
    <button onclick="sendKey('left')">←</button>
    <button onclick="sendKey('right')">→</button>
    <button onclick="location.reload()">↻</button>
  </div>
  <div id="viewport">
    <img id="screen" src="/snapshot" alt="desktop" draggable="false">
    <div id="overlay"><div id="fps">Live</div></div>
  </div>
  <div class="hint">Tap / Click image to click • Right-click for right-click • Drag to select/drag • Scroll with wheel / two fingers • Use text box to type • Keyboard works when focused</div>
<script>
let img = document.getElementById('screen');
let st = document.getElementById('st');
let fpsEl = document.getElementById('fps');
let frames=0, lastT=Date.now(), errors=0, delay=500;
let dragging=false, startX=0, startY=0;
let screenW=1920, screenH=1080;

// get screen size
fetch('/info').then(r=>r.json()).then(j=>{screenW=j.width; screenH=j.height; st.textContent=`${screenW}x${screenH} | Live`})

// MJPEG vs snapshot fallback: try mjpeg first
let useMjpeg = true;
let mjpegImg = new Image();
let snapshotMode = false;

function setupMjpeg(){
  img.src = "/stream";
  img.onerror = ()=>{ snapshotMode=true; console.log("mjpeg failed, falling to snapshot"); startSnapshot(); }
  img.onload = ()=>{ st.textContent=`${screenW}x${screenH} | Live MJPEG`; }
}
function startSnapshot(){
  snapshotMode=true;
  img.src="/snapshot?t="+Date.now();
  function load(){
    let t=Date.now();
    let nx=new Image();
    nx.onload=function(){
      if(snapshotMode){
        img.src=nx.src;
        errors=0;
        if(delay>600) delay=600;
        frames++;
        let el=Date.now()-lastT;
        if(el>=2000){ fpsEl.textContent=Math.round(frames*1000/el)+' FPS | Live'; st.textContent=`${screenW}x${screenH} | ${Math.round(frames*1000/el)} FPS`; frames=0; lastT=Date.now();}
        setTimeout(load, Math.max(80, delay-(Date.now()-t)));
      }
    };
    nx.onerror=function(){
      errors++; fpsEl.textContent='Reconnecting... ('+errors+')'; st.textContent='Reconnecting...';
      delay=Math.min(delay*1.4,4000);
      setTimeout(load, Math.min(errors*600,3000));
    };
    nx.src='/snapshot?t='+t;
  }
  load();
}
// Try mjpeg with fallback after 3s if not loaded
setupMjpeg();
setTimeout(()=>{ if(img.naturalWidth==0){ startSnapshot(); } }, 2500);

function getRatio(e){
  let rect = img.getBoundingClientRect();
  let x = (e.clientX - rect.left) / rect.width;
  let y = (e.clientY - rect.top) / rect.height;
  // clamp
  x=Math.max(0,Math.min(1,x)); y=Math.max(0,Math.min(1,y));
  return {x, y}
}
function postInput(data){
  fetch('/input', {method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify(data)}).catch(()=>{})
}
img.addEventListener('click', (e)=>{
  if(dragging) return;
  let r=getRatio(e);
  postInput({action:'click', x_ratio:r.x, y_ratio:r.y})
  // visual feedback
  fpsEl.textContent=`Click ${(r.x*100).toFixed(1)}% ${(r.y*100).toFixed(1)}%`;
  setTimeout(()=>fpsEl.textContent='Live',800)
});
img.addEventListener('contextmenu', (e)=>{
  e.preventDefault();
  let r=getRatio(e);
  postInput({action:'rclick', x_ratio:r.x, y_ratio:r.y})
  fpsEl.textContent='Right Click';
  setTimeout(()=>fpsEl.textContent='Live',800)
  return false;
});
img.addEventListener('dblclick', (e)=>{
  let r=getRatio(e);
  postInput({action:'dblclick', x_ratio:r.x, y_ratio:r.y})
});
img.addEventListener('mousedown', (e)=>{
  if(e.button!==0) return;
  dragging=true;
  let r=getRatio(e);
  startX=r.x; startY=r.y;
});
img.addEventListener('mouseup', (e)=>{
  if(!dragging) return;
  dragging=false;
  let r=getRatio(e);
  let dx=Math.abs(r.x-startX), dy=Math.abs(r.y-startY);
  if(dx>0.02 || dy>0.02){
    postInput({action:'drag', x_ratio:startX, y_ratio:startY, x2_ratio:r.x, y2_ratio:r.y})
    fpsEl.textContent='Drag';
  }
});
img.addEventListener('wheel', (e)=>{
  e.preventDefault();
  postInput({action:'scroll', amount: -Math.sign(e.deltaY)*300})
},{passive:false});
img.addEventListener('touchstart', (e)=>{
  if(e.touches.length==1){
    let t=e.touches[0];
    let r=getRatio(t);
    startX=r.x; startY=r.y;
  }
},{passive:true});
img.addEventListener('touchend', (e)=>{
  if(e.changedTouches.length==1){
    let t=e.changedTouches[0];
    let r=getRatio(t);
    let dx=Math.abs(r.x-startX), dy=Math.abs(r.y-startY);
    if(dx<0.02 && dy<0.02){
      postInput({action:'click', x_ratio:r.x, y_ratio:r.y})
    } else {
      postInput({action:'drag', x_ratio:startX, y_ratio:startY, x2_ratio:r.x, y2_ratio:r.y})
    }
  }
},{passive:true});

// keyboard
document.addEventListener('keydown', (e)=>{
  // if typing in input, don't capture
  if(document.activeElement.id==='typeText') return;
  // avoid flooding
  if(e.repeat) return;
  let key = e.key.toLowerCase();
  // map
  if(key==='enter' || key==='tab' || key==='escape' || key==='backspace' || key==='delete' || key==='arrowup' || key==='arrowdown' || key==='arrowleft' || key==='arrowright'){
    e.preventDefault();
    let map={'arrowup':'up','arrowdown':'down','arrowleft':'left','arrowright':'right'};
    postInput({action:'key', keys: map[key]||key})
  } else if(e.ctrlKey || e.altKey || e.metaKey){
    let combo=[];
    if(e.ctrlKey) combo.push('ctrl');
    if(e.altKey) combo.push('alt');
    if(e.metaKey) combo.push('win');
    if(key.length===1) combo.push(key);
    else if(!['control','alt','meta','shift'].includes(key)) combo.push(key);
    if(combo.length>1){
      e.preventDefault();
      postInput({action:'hotkey', keys: combo.join('+')})
    }
  } else if(key.length===1){
    // single char typing via type action for reliability
    // send as type 1 char? Instead send key press
    // Use type for visible chars
    postInput({action:'type', text:e.key})
  }
});

function sendType(){
  let el=document.getElementById('typeText');
  let t=el.value;
  if(!t) return;
  postInput({action:'type', text:t});
  el.value='';
  fpsEl.textContent='Typed '+t.length+' chars';
}
function sendKey(k){ postInput({action:'key', keys:k}) }
function sendHotkey(k){ postInput({action:'hotkey', keys:k}) }
document.getElementById('typeText').addEventListener('keydown', (e)=>{
  if(e.key==='Enter'){ e.preventDefault(); sendType(); }
});
</script>
</body>
</html>"""
                return Response(html, mimetype='text/html')

            @app.route("/snapshot")
            def snapshot():
                try:
                    data, w, h = capture_jpeg(width=960, quality=35)
                    if data is None:
                        return Response("Capture failed", status=500)
                    return Response(data, mimetype='image/jpeg', headers={
                        'Cache-Control': 'no-store, no-cache, must-revalidate',
                        'Pragma': 'no-cache', 'Expires': '0'
                    })
                except Exception as e:
                    return Response(f"Error: {e}", status=500)

            @app.route("/stream")
            def stream():
                return Response(gen_frames(), mimetype='multipart/x-mixed-replace; boundary=frame')

            @app.route("/info")
            def info():
                return jsonify({"width": SCREEN_W, "height": SCREEN_H})

            @app.route("/input", methods=["POST"])
            def input_route():
                try:
                    data = request.get_json(force=True) or {}
                    action = (data.get("action") or "").lower()
                    pyautogui = _ensure_pyautogui()
                    if not pyautogui:
                        return jsonify({"ok": False, "error": "pyautogui missing"})
                    # Helper to convert ratio to coords
                    def to_coords(d):
                        # support both ratio and absolute
                        if "x_ratio" in d and "y_ratio" in d:
                            x = int(float(d["x_ratio"]) * SCREEN_W)
                            y = int(float(d["y_ratio"]) * SCREEN_H)
                            return x, y
                        elif "x" in d and "y" in d:
                            return int(d["x"]), int(d["y"])
                        else:
                            return None, None
                    if action == "click":
                        x,y = to_coords(data)
                        if x is not None:
                            pyautogui.click(x, y)
                        else:
                            pyautogui.click()
                        return jsonify({"ok": True})
                    elif action == "rclick" or action == "rightclick":
                        x,y = to_coords(data)
                        if x is not None:
                            pyautogui.rightClick(x, y)
                        else:
                            pyautogui.rightClick()
                        return jsonify({"ok": True})
                    elif action in ("dblclick", "doubleclick", "double_click"):
                        x,y = to_coords(data)
                        if x is not None:
                            pyautogui.doubleClick(x, y)
                        else:
                            pyautogui.doubleClick()
                        return jsonify({"ok": True})
                    elif action == "move":
                        x,y = to_coords(data)
                        if x is not None:
                            pyautogui.moveTo(x,y)
                        return jsonify({"ok": True})
                    elif action == "drag":
                        x,y = to_coords(data)
                        x2 = data.get("x2_ratio")
                        y2 = data.get("y2_ratio")
                        if x is not None and x2 is not None:
                            x2i = int(float(x2) * SCREEN_W)
                            y2i = int(float(y2) * SCREEN_H)
                            pyautogui.moveTo(x,y, duration=0.05)
                            pyautogui.dragTo(x2i, y2i, duration=0.4, button='left')
                        elif "x2" in data and "y2" in data:
                            pyautogui.moveTo(x,y, duration=0.05)
                            pyautogui.dragTo(int(data["x2"]), int(data["y2"]), duration=0.4)
                        return jsonify({"ok": True})
                    elif action == "scroll":
                        amt = int(data.get("amount", 300))
                        pyautogui.scroll(amt)
                        return jsonify({"ok": True})
                    elif action == "type":
                        text = data.get("text", "")
                        if text:
                            # use clipboard for long text via helper but here simple
                            if len(text) > 80 or any(ord(c)>127 for c in text):
                                try:
                                    import pyperclip
                                    pyperclip.copy(text)
                                    pyautogui.hotkey('ctrl','v')
                                except:
                                    pyautogui.write(text, interval=0.005)
                            else:
                                pyautogui.write(text, interval=0.01)
                        return jsonify({"ok": True})
                    elif action == "key":
                        keys = data.get("keys") or data.get("key") or ""
                        if keys:
                            # single key press
                            k = keys.lower().strip()
                            # map arrow aliases
                            if k in ("arrowup", "up"): pyautogui.press("up")
                            elif k in ("arrowdown", "down"): pyautogui.press("down")
                            elif k in ("arrowleft", "left"): pyautogui.press("left")
                            elif k in ("arrowright", "right"): pyautogui.press("right")
                            else: pyautogui.press(k)
                        return jsonify({"ok": True})
                    elif action == "hotkey":
                        keys = data.get("keys") or ""
                        if keys:
                            parts = [p.strip().lower() for p in re.split(r'\+', keys) if p.strip()]
                            # normalize
                            mp = {"cmd":"win","super":"win","windows":"win","return":"enter","esc":"escape","del":"delete"}
                            parts = [mp.get(p,p) for p in parts]
                            pyautogui.hotkey(*parts)
                        return jsonify({"ok": True})
                    elif action == "press":
                        keys = data.get("keys") or data.get("key") or ""
                        if keys:
                            if "+" in keys:
                                parts = [p.strip() for p in keys.split("+")]
                                pyautogui.hotkey(*parts)
                            else:
                                pyautogui.press(keys)
                        return jsonify({"ok": True})
                    else:
                        return jsonify({"ok": False, "error": f"unknown action {action}"})
                except Exception as e:
                    print(f"input error {e}")
                    return jsonify({"ok": False, "error": str(e)})

            @app.route("/stop-stream")
            def stop_stream():
                livestream_stop(notify=False)
                return Response("Screen sharing and PhoneFS stopped", mimetype='text/plain')

            with livestream_lock:
                if stop_event.is_set() or stop_event is not livestream_stop_event:
                    return
                if livestream_server is None:
                    # Bind synchronously so a port collision is caught here,
                    # rather than failing silently inside Flask's daemon thread.
                    livestream_server = make_server("0.0.0.0", 5000, app, threaded=True)
                    threading.Thread(target=livestream_server.serve_forever,
                                     daemon=True, name="Screen web server").start()
                livestream_flask_started = True
            _start_desktop_tunnel(chat_id, stop_event)

        except (Exception, SystemExit) as e:
            # werkzeug can raise SystemExit on a bind failure.
            with livestream_lock:
                if stop_event.is_set() or stop_event is not livestream_stop_event:
                    return
                livestream_active = False
                livestream_flask_started = livestream_server is not None
                stop_event.set()
            send_message(chat_id, f"Screen sharing error: {e}. PhoneFS is independent; use livestream status.", parse_mode=None)
            print(f"livestream error {e}", flush=True)

    threading.Thread(target=run_server, daemon=True, name="Screen startup").start()

WELCOME = (
    "*🖥️ GitHub VM - Full Remote Control*\n\n"
    "*📸 Screen & Remote:*\n"
    "- `screen` - Screenshot\n"
    "- `livestream` / `live` - Start Remote Desktop + PhoneFS automatically\n"
    "  Sends two links, VM user ID and a generated PhoneFS password\n"
    "- `livestream status` - Both links, credentials & diagnostics\n"
    "- `livestream restart` - Restart both with a fresh PhoneFS password\n"
    "- `stop stream` - Close both public tunnels\n\n"
    "*⌨️ Write / Keyboard / Mouse (NEW - Full Control):*\n"
    "- `type <text>` - Type text into active window (fast via clipboard if needed)\n"
    "- `paste <text>` / `typepaste <text>` - Paste via clipboard (best for long/unicode)\n"
    "- `press <key>` / `key <key>` / `hotkey <combo>` - Press keys\n"
    "  e.g. `press enter`, `press ctrl+c`, `press alt+f4`, `press win+r`, `press f5`\n"
    "- `click <x> <y>` - Click at coordinates (e.g. `click 500 300`)\n"
    "- `click <name>` - Click UI control by name\n"
    "- `rclick 100 200` / `rightclick` - Right click\n"
    "- `doubleclick 100 200` / `dclick` - Double click\n"
    "- `move 800 500` - Move mouse\n"
    "- `drag 100 100 500 500` - Drag\n"
    "- `scroll up 500` / `scroll down 500` / `scroll 300`\n"
    "- `pos` - Show mouse position & screen size\n\n"
    "*📁 Files:*\n"
    "- `pwd` / `ls [path]` / `cat <file>` / `get <file>` (send file)\n"
    "- `mkdir <dir>` / `rm <path>` / `mv <src> <dst>` / `cp <src> <dst>`\n"
    "- `write <file> <content>` - Write file\n"
    "- `append <file> <content>` - Append\n"
    "- `find <pattern> [path]` / `tree [path]` / `du [path]`\n"
    "- `zip <src> [dst]` / `unzip <zip> [dst]` / `wget <url> [file]`\n"
    "- `cd <path>` - Change directory\n"
    "- Send any file/photo to VM to upload\n\n"
    "*🪟 Windows & Apps:*\n"
    "- `apps` - List available apps\n"
    "- `windows` / `opened apps` - List open windows\n"
    "- `open <app>` - Open app (e.g. `open notepad`, `open chrome`)\n"
    "- `browser <url>` - Open URL\n"
    "- `focus <window>` - Focus window\n"
    "- `close <window>` - Close (Alt+F4)\n"
    "- `minimize` / `maximize`\n"
    "- `buttons` - List controls in active window\n\n"
    "*⚙️ System:*\n"
    "- `ps` - Processes\n"
    "- `kill <pid_or_name>` / `killall <name>`\n"
    "- `sysinfo` - CPU/RAM/Disk/OS\n"
    "- `env [var]` / `uptime` / `ip`\n"
    "- `clip get` / `clip set <text>` / `copyclip <text>` - Clipboard\n\n"
    "*💻 Shell / Code:*\n"
    "- `/<shell command>` - Run shell (e.g. `/dir`, `/pip list`)\n"
    "- `cmd <command>` / `exec <command>` - Explicit shell\n"
    "- Any other text runs as Python\n"
    "- `terminate` - Kill running task\n\n"
    "*💡 Tips:*\n"
    "- Use `livestream` then open link for phone/PC remote desktop like TeamViewer\n"
    "- Inside livestream: tap to click, type in box, use keyboard buttons\n"
    "- Via chat: `type Hello World` writes anywhere, `press win+r` opens Run, `click 900 500` clicks\n"
)

def main():
    if not TOKEN:
        print("No TELEGRAM_BOT_TOKEN", flush=True)
        sys.exit(1)

    try:
        r = requests.get(f"{BASE}/getUpdates", params={"offset": -1}, timeout=10).json()
        offset = r["result"][0]["update_id"] + 1 if r.get("result") else 0
    except:
        offset = 0

    print(f"Bot polling started. Allowed chat: {ALLOWED_CHAT_ID or 'any'} | CWD: {os.getcwd()}", flush=True)

    while True:
        try:
            updates = requests.get(f"{BASE}/getUpdates", params={"offset": offset, "timeout": 30}, timeout=40).json().get("result", [])
            for upd in updates:
                offset = upd["update_id"] + 1
                msg = upd.get("message", {})
                chat_id = msg.get("chat", {}).get("id")
                if not chat_id or (ALLOWED_CHAT_ID and chat_id != ALLOWED_CHAT_ID):
                    continue
                # Once remote access is claimed, protect credentials from other
                # chats' shell/file commands too (even if chat_id was not set).
                if livestream_chat_id is not None and chat_id != livestream_chat_id:
                    continue

                # file uploads -> save to VM
                if "document" in msg:
                    download_file(chat_id, msg["document"]["file_id"], msg["document"].get("file_name", "file"))
                    continue
                elif "photo" in msg:
                    download_file(chat_id, msg["photo"][-1]["file_id"], f"photo_{int(time.time())}.jpg")
                    continue

                orig_text = (msg.get("text") or "").strip()
                if not orig_text:
                    continue
                text = orig_text.lower()

                # ----- Help -----
                if text in ("/start", "/help", "help", "?", "menu"):
                    send_message(chat_id, WELCOME)
                    continue
                if text == "/stop":
                    send_message(chat_id, "Stopping bot...")
                    sys.exit(0)
                if text in ("screen", "screenshot", "ss", "capture", "screen hd", "screen low", "screencap"):
                    take_screenshot(chat_id, mode=text)
                    continue
                if text in ("livestream status", "live status", "stream status", "livestream url", "live url", "tunnel status", "tunnel url"):
                    livestream_status(chat_id)
                    continue
                if text in ("livestream restart", "live restart", "stream restart", "restart stream", "restart livestream", "tunnel restart") or text in ("livestream reconnect", "reconnect"):
                    livestream_restart(chat_id)
                    continue
                if text in ("livestream", "live", "stream", "remote", "desktop", "vs"):
                    livestream(chat_id)
                    continue
                if text in ("stop stream", "stop livestream", "stop live", "close stream"):
                    livestream_stop(chat_id)
                    continue
                if text in ("terminate", "killtask", "stop task", "cancel"):
                    if current_process:
                        try: current_process.terminate()
                        except: pass
                        send_message(chat_id, "Terminated running task.")
                    else:
                        send_message(chat_id, "No running task.")
                    continue

                # ----- Input: keyboard / mouse - prioritize these before file/system -----
                # type / paste
                if text.startswith("type ") or text.startswith("typepaste ") or text.startswith("paste ") or text.startswith("typefast "):
                    # preserve case
                    if text.startswith("typepaste "):
                        do_type(chat_id, orig_text[10:].strip(), use_clipboard=True)
                    elif text.startswith("paste "):
                        do_type(chat_id, orig_text[6:].strip(), use_clipboard=True)
                    elif text.startswith("typefast "):
                        do_type(chat_id, orig_text[9:].strip(), use_clipboard=True)
                    else: # type
                        do_type(chat_id, orig_text[5:].strip(), use_clipboard=False)
                    continue
                if text.startswith("key ") or text.startswith("press ") or text.startswith("hotkey ") or text.startswith("keys ") or text.startswith("key:"):
                    # extract after first space
                    if text.startswith("key "):
                        combo = orig_text[4:].strip()
                    elif text.startswith("press "):
                        combo = orig_text[6:].strip()
                    elif text.startswith("hotkey "):
                        combo = orig_text[7:].strip()
                    elif text.startswith("keys "):
                        combo = orig_text[5:].strip()
                    else:
                        combo = orig_text[4:].strip()
                    do_key(chat_id, combo)
                    continue
                # hold / release for keys
                if text.startswith("hold ") or text.startswith("release "):
                    # hold/release via pyautogui keyDown/keyUp
                    is_hold = text.startswith("hold ")
                    key = orig_text[5:].strip() if is_hold else orig_text[8:].strip()
                    try:
                        pyautogui = _ensure_pyautogui()
                        if pyautogui:
                            if is_hold:
                                pyautogui.keyDown(key)
                                send_message(chat_id, f"Holding `{key}`")
                            else:
                                pyautogui.keyUp(key)
                                send_message(chat_id, f"Released `{key}`")
                    except Exception as e:
                        send_message(chat_id, f"hold/release error: {e}")
                    continue
                # clipboard
                if text in ("clip get", "clipboard", "clipboard get", "pasteclip", "clip"):
                    do_clipboard(chat_id, "get")
                    continue
                if text.startswith("clip set ") or text.startswith("clipboard set ") or text.startswith("copyclip ") or text.startswith("copytext ") or text.startswith("setclip "):
                    if text.startswith("clip set "):
                        payload = orig_text[9:].strip()
                    elif text.startswith("clipboard set "):
                        payload = orig_text[14:].strip()
                    elif text.startswith("copyclip "):
                        payload = orig_text[9:].strip()
                    elif text.startswith("copytext "):
                        payload = orig_text[9:].strip()
                    else: # setclip
                        payload = orig_text[8:].strip()
                    do_clipboard(chat_id, "set", payload)
                    continue
                if text in ("clearclip", "clip clear", "clipboard clear"):
                    do_clipboard(chat_id, "clear")
                    send_message(chat_id, "Clipboard cleared")
                    continue
                # stdin to running process
                if text.startswith("input ") or text.startswith("sendinput ") or text.startswith("stdin ") or text == "input" or text == "stdin":
                    payload = orig_text.split(" ",1)[1] if " " in orig_text else ""
                    payload = payload.replace("\\n", "\n")
                    if not payload and text in ("input", "stdin"):
                        send_message(chat_id, "Usage: `input <text>` - sends text to running process stdin (for interactive commands)")
                        continue
                    if current_process and current_process.poll() is None:
                        try:
                            if current_process.stdin:
                                current_process.stdin.write(payload + "\n")
                                current_process.stdin.flush()
                                send_message(chat_id, f"Sent input to process ({len(payload)} chars)")
                            else:
                                send_message(chat_id, "Process stdin not available")
                        except Exception as e:
                            send_message(chat_id, f"input error: {e}")
                    else:
                        send_message(chat_id, "No running interactive process. `input <text>` only works when a command is running that waits for input.")
                    continue
                # mouse commands
                if text in ("pos", "position", "where", "mousepos", "mouse pos", "whereami mouse"):
                    do_mouse(chat_id, "pos", "")
                    continue
                if text.startswith("move ") or text.startswith("mousemove "):
                    arg = orig_text.split(" ",1)[1] if " " in orig_text else ""
                    # distinguish mouse move (coords) vs file move (file paths)
                    # mouse move is exactly two integers like "500 300"
                    tokens = arg.strip().split()
                    is_coords = len(tokens)==2 and all(tok.lstrip('-').isdigit() for tok in tokens)
                    if is_coords:
                        do_mouse(chat_id, "move", arg)
                        continue
                    # else fall through to file handler (mv/move for files)
                    pass
                if text.startswith("click ") or text == "click":
                    arg = orig_text[6:].strip() if len(orig_text) > 5 else ""
                    # detect coords vs name
                    # if arg is coords like "100 200" then mouse click, else UI automation
                    # We delegate to do_mouse which handles both, but for UI names containing numbers it may mis-handled
                    # Use heuristic: if arg contains only digits/spaces/comma and 2 numbers -> coords
                    coords = re.findall(r'-?\d+', arg)
                    if len(coords) >= 2 and re.match(r'^[\d\s,\.x]+$', arg):
                        do_mouse(chat_id, "click", arg)
                    elif arg:
                        # try to see if it's clearly coords with maybe "x"? For safety try do_mouse and fallback to UI
                        if len(coords)>=2:
                            do_mouse(chat_id, "click", arg)
                        else:
                            ui_automation(chat_id, "click", arg)
                    else:
                        do_mouse(chat_id, "click", "")
                    continue
                if text.startswith("rclick") or text.startswith("rightclick") or text.startswith("right click"):
                    # extract args after keyword
                    m = re.match(r'^(r?click|rightclick|right click)\s*(.*)', text)
                    arg_orig = ""
                    if " " in orig_text:
                        # find first space
                        arg_orig = orig_text.split(" ",1)[1]
                        # For "right click ..." need to handle "right click 100 200" -> orig_text after "right click "
                        if text.startswith("right click"):
                            arg_orig = orig_text[12:].strip()
                        elif text.startswith("rightclick"):
                            arg_orig = orig_text[11:].strip()
                        elif text.startswith("rclick"):
                            arg_orig = orig_text[7:].strip()
                    do_mouse(chat_id, "rclick", arg_orig)
                    continue
                if text.startswith("double click ") or text.startswith("doubleclick ") or text.startswith("dclick ") or text.startswith("dblclick "):
                    if text.startswith("double click "):
                        arg = orig_text[13:].strip()
                    elif text.startswith("doubleclick "):
                        arg = orig_text[12:].strip()
                    elif text.startswith("dclick "):
                        arg = orig_text[7:].strip()
                    else:
                        arg = orig_text[9:].strip()
                    do_mouse(chat_id, "doubleclick", arg)
                    continue
                if text.startswith("drag "):
                    arg = orig_text[5:].strip()
                    do_mouse(chat_id, "drag", arg)
                    continue
                if text.startswith("scroll ") or text.startswith("wheel ") or text == "scroll" or text == "wheel":
                    arg = orig_text.split(" ",1)[1] if " " in orig_text else ""
                    do_mouse(chat_id, "scroll", arg)
                    continue
                if text in ("holdmouse", "mousedown", "mouseup", "releasemouse"):
                    if text in ("holdmouse", "mousedown"):
                        do_mouse(chat_id, "hold", "")
                    else:
                        do_mouse(chat_id, "release", "")
                    continue

                # ----- Window / Apps -----
                if text in ("buttons", "controls", "list buttons", "show buttons"):
                    ui_automation(chat_id, "list_buttons")
                    continue
                if text == "opened apps" or text in ("apps", "list apps", "available apps") :
                    if text == "apps" or text.startswith("list apps"):
                        ui_automation(chat_id, "available_apps", "list")
                    else:
                        # opened apps - we try enhanced handler too
                        if not handle_window_commands(chat_id, orig_text, text):
                            ui_automation(chat_id, "opened_apps")
                    continue
                if text in ("windows", "winlist", "list windows"):
                    handle_window_commands(chat_id, orig_text, text)
                    continue
                if text.startswith("open ") or text.startswith("start "):
                    # check if it's file command open? Already handled file? but open app takes precedence
                    # If argument looks like file path with \ or / and exists, maybe open file?
                    # For now treat as app launch
                    ui_automation(chat_id, "available_apps", orig_text[5:].strip() if text.startswith("open ") else orig_text[6:].strip())
                    continue
                if text.startswith("browser ") or text.startswith("openurl ") or text.startswith("open url "):
                    handle_window_commands(chat_id, orig_text, text)
                    continue
                if text.startswith("focus ") or text.startswith("activate ") or text.startswith("switch "):
                    handle_window_commands(chat_id, orig_text, text)
                    continue
                if text.startswith("close ") or text.startswith("killwindow "):
                    handle_window_commands(chat_id, orig_text, text)
                    continue
                if text in ("minimize", "maximize", "min", "max") or text.startswith("minimize ") or text.startswith("maximize "):
                    handle_window_commands(chat_id, orig_text, text)
                    continue

                # ----- File commands -----
                if handle_file_commands(chat_id, orig_text, text):
                    continue
                # ----- System commands -----
                if handle_system_commands(chat_id, orig_text, text):
                    continue

                # ----- Direct shell vs python -----
                # explicit shell prefixes
                explicit_shell = False
                explicit_python = False
                if text.startswith("cmd ") or text.startswith("exec ") or text.startswith("shell "):
                    explicit_shell = True
                    # strip prefix
                    if text.startswith("cmd "):
                        orig_text = orig_text[4:].strip()
                    elif text.startswith("exec "):
                        orig_text = orig_text[5:].strip()
                    else:
                        orig_text = orig_text[6:].strip()
                elif text.startswith("py ") or text.startswith("python "):
                    explicit_python = True
                    if text.startswith("py "):
                        orig_text = orig_text[3:].strip()
                    else:
                        orig_text = orig_text[7:].strip()

                is_shell = False
                if explicit_shell:
                    is_shell = True
                elif explicit_python:
                    is_shell = False
                else:
                    # auto-detect: starts with / or known shell starters
                    shell_starters = ["pip ", "pip3 ", "npm ", "npx ", "git ", "python ", "py ", "node ", "yarn ", "cargo ", "dotnet ", "powershell", "pwsh ", "cmd ", "bash ", "sh ", "curl ", "wget ", "choco ", "winget ", "java ", "javac ", "go ", "rustc ", "docker ", "kubectl ", "terraform ", "aws ", "az ", "gcloud ", "ls ", "dir", "echo ", "cat ", "type ", "mkdir ", "rm ", "del ", "copy ", "move ", "cd ", "pwd", "whoami", "hostname", "set ", "env", "ipconfig", "ifconfig", "netstat", "tasklist", "ps", "kill"]
                    if text.startswith("/"):
                        is_shell = True
                        orig_text = orig_text[1:].strip()
                    elif any(text.startswith(s) for s in shell_starters):
                        is_shell = True
                    else:
                        # Check for shell-like patterns: contains "&&" or "|" or ">"
                        # but default to python for safety as before
                        is_shell = False

                run_command(chat_id, orig_text, is_python=not is_shell)

        except Exception as e:
            print(f"Main loop error: {e}", flush=True)
            time.sleep(5)


if __name__ == "__main__":
    # A normal /stop, Ctrl+C or termination also closes PhoneFS's child tunnel.
    signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))
    try:
        main()
    finally:
        shutdown_remote_access()
