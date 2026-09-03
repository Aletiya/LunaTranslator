"""
CDP Web AI Translator for LunaTranslator
Supports: Google Gemini Web (gemini.google.com) and ChatGPT Web (chatgpt.com)
Contributed by Aletiya
"""

import os
import sys
import time
import json
import socket
import struct
import base64
import hashlib
import urllib.request
import urllib.parse
import subprocess
import threading
from traceback import print_exc

try:
    from translator.basetranslator import basetrans, GptTextWithDict
except ImportError:
    class basetrans:
        def __init__(self, typename):
            self.typename = typename
            self.config = {}
    class GptTextWithDict:
        def __init__(self, rawtext=None, parsedtext=None):
            self.rawtext = rawtext
            self.parsedtext = parsedtext

# Dynamically enhance in-memory configuration schemas without modifying files on disk
try:
    from myutils.config import translatorsetting, globalconfig
    if "cdp_chatgpt" in translatorsetting:
        argstype = translatorsetting["cdp_chatgpt"].get("argstype", {})
        if "usewhich" in argstype:
            argstype["usewhich"]["list"] = ["Google Gemini Web", "OpenAI ChatGPT Web"]
except Exception:
    pass


class SimpleWebSocket:
    """Self-contained RFC 6455 WebSocket client using Python standard library."""
    def __init__(self, url, timeout=15):
        parsed = urllib.parse.urlparse(url)
        self.host = parsed.hostname
        self.port = parsed.port or (443 if parsed.scheme == "wss" else 80)
        self.path = parsed.path
        if parsed.query:
            self.path += "?" + parsed.query
        
        self.sock = socket.create_connection((self.host, self.port), timeout=timeout)
        self._handshake()

    def _handshake(self):
        key = base64.b64encode(os.urandom(16)).decode('utf-8')
        headers = [
            f"GET {self.path} HTTP/1.1",
            f"Host: {self.host}:{self.port}",
            "Upgrade: websocket",
            "Connection: Upgrade",
            f"Sec-WebSocket-Key: {key}",
            "Sec-WebSocket-Version: 13",
            "\r\n"
        ]
        self.sock.sendall("\r\n".join(headers).encode('utf-8'))
        
        resp = b""
        while b"\r\n\r\n" not in resp:
            chunk = self.sock.recv(1024)
            if not chunk:
                raise ConnectionError("Connection closed during WebSocket handshake")
            resp += chunk
        
        status_line = resp.split(b"\r\n")[0].decode('utf-8', errors='ignore')
        if "101" not in status_line:
            raise ConnectionError(f"WebSocket handshake failed: {status_line}")

    def send(self, message: str):
        payload = message.encode('utf-8')
        length = len(payload)
        header = bytearray([0x81])
        mask_key = os.urandom(4)
        
        if length < 126:
            header.append(0x80 | length)
        elif length < 65536:
            header.append(0x80 | 126)
            header.extend(struct.pack("!H", length))
        else:
            header.append(0x80 | 127)
            header.extend(struct.pack("!Q", length))
            
        header.extend(mask_key)
        masked = bytearray(payload[i] ^ mask_key[i % 4] for i in range(length))
        self.sock.sendall(header + masked)

    def recv(self) -> str:
        header = self.sock.recv(2)
        if not header or len(header) < 2:
            raise ConnectionError("WebSocket connection closed")
        
        length = header[1] & 0x7F
        if length == 126:
            ext = self.sock.recv(2)
            length = struct.unpack("!H", ext)[0]
        elif length == 127:
            ext = self.sock.recv(8)
            length = struct.unpack("!Q", ext)[0]
            
        is_masked = bool(header[1] & 0x80)
        mask = self.sock.recv(4) if is_masked else None
        
        data = bytearray()
        while len(data) < length:
            chunk = self.sock.recv(min(length - len(data), 65536))
            if not chunk:
                raise ConnectionError("Unexpected EOF reading WebSocket payload")
            data.extend(chunk)
            
        if is_masked:
            for i in range(len(data)):
                data[i] ^= mask[i % 4]
                
        return data.decode('utf-8', errors='replace')

    def close(self):
        try:
            self.sock.close()
        except:
            pass


class WebAIController:
    """Controls browser automation, CDP connection, session readiness, and message dispatching."""
    def __init__(self, config):
        self.config = config
        self.ws = None
        self._msg_id = 0
        self.lock = threading.Lock()
        self.service_type = self.config.get("usewhich", 0)  # 0: Gemini Web, 1: ChatGPT Web

    @property
    def target_url(self):
        if self.service_type == 1:
            return "https://chatgpt.com/"
        return "https://gemini.google.com/app"

    def get_browser_path(self):
        custom = self.config.get("chromepath", "")
        candidates = [
            custom,
            r"C:\Program Files\Google\Chrome\Application\chrome.exe",
            r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
            r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
            r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
        ]
        for p in candidates:
            if p and os.path.isfile(p):
                return p
        return None

    def get_cache_dir(self):
        # Uses src/chrome_cache which is already ignored by LunaTranslator's original .gitignore
        base = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", "chrome_cache"))
        service_name = "gemini_profile" if self.service_type == 0 else "chatgpt_profile"
        profile_path = os.path.join(base, service_name)
        os.makedirs(profile_path, exist_ok=True)
        return profile_path

    def ensure_browser_running(self):
        port = self.config.get("debugport", 9222)
        try:
            urllib.request.urlopen(f"http://127.0.0.1:{port}/json/version", timeout=1)
            return True
        except:
            pass

        browser_path = self.get_browser_path()
        if not browser_path:
            raise FileNotFoundError("Google Chrome or Microsoft Edge was not found on this system.")

        cache_dir = self.get_cache_dir()
        cmd = [
            browser_path,
            f"--remote-debugging-port={port}",
            f"--user-data-dir={cache_dir}",
            "--no-first-run",
            self.target_url
        ]
        subprocess.Popen(cmd)

        for _ in range(30):
            time.sleep(0.5)
            try:
                urllib.request.urlopen(f"http://127.0.0.1:{port}/json/version", timeout=1)
                return True
            except:
                continue
        raise TimeoutError("Failed to connect to the browser debugging port.")

    def send_cdp(self, method: str, params: dict = None) -> dict:
        with self.lock:
            self._msg_id += 1
            msg = {"id": self._msg_id, "method": method, "params": params or {}}
            self.ws.send(json.dumps(msg))
            resp = json.loads(self.ws.recv())
            if "error" in resp:
                raise RuntimeError(f"CDP Error: {resp['error']}")
            return resp.get("result", {})

    def evaluate_js(self, js: str) -> any:
        res = self.send_cdp("Runtime.evaluate", {
            "expression": js,
            "returnByValue": True,
            "awaitPromise": True
        })
        return res.get("result", {}).get("value")

    def connect_target_tab(self):
        port = self.config.get("debugport", 9222)
        req = urllib.request.urlopen(f"http://127.0.0.1:{port}/json/list")
        tabs = json.loads(req.read().decode("utf-8"))

        target_domain = "gemini.google.com" if self.service_type == 0 else "chatgpt.com"
        target_tab = None

        for t in tabs:
            if target_domain in t.get("url", ""):
                target_tab = t
                break

        if not target_tab:
            for t in tabs:
                if t.get("type") == "page":
                    target_tab = t
                    break

        if not target_tab:
            raise RuntimeError("No suitable browser tab found.")

        ws_url = target_tab.get("webSocketDebuggerUrl")
        if not ws_url:
            raise RuntimeError("Failed to retrieve webSocketDebuggerUrl for tab.")

        if self.ws:
            try:
                self.ws.close()
            except:
                pass

        self.ws = SimpleWebSocket(ws_url)

        current_url = self.evaluate_js("window.location.href")
        if target_domain not in current_url:
            self.send_cdp("Page.navigate", {"url": self.target_url})
            time.sleep(2)

    def wait_for_ready_state(self, timeout_sec=180):
        """Waits until user has logged in and the chat input element is available in DOM."""
        start = time.time()
        while time.time() - start < timeout_sec:
            try:
                if self.service_type == 0:  # Gemini Web
                    check_js = """
                    (() => {
                        const url = window.location.href;
                        const isLogin = url.includes('accounts.google.com');
                        const editor = document.querySelector('rich-textarea div.ql-editor') || 
                                       document.querySelector('div[contenteditable="true"]');
                        return { ready: !!editor && !isLogin, isLogin: isLogin };
                    })()
                    """
                else:  # ChatGPT Web
                    check_js = """
                    (() => {
                        const url = window.location.href;
                        const isLogin = url.includes('/auth/') || url.includes('/login');
                        const input = document.querySelector('#prompt-textarea');
                        return { ready: !!input && !isLogin, isLogin: isLogin };
                    })()
                    """
                
                status = self.evaluate_js(check_js)
                if status and status.get("ready"):
                    return True
            except Exception:
                pass
            time.sleep(1)
        raise TimeoutError("Timed out waiting for login or chat input readiness in browser.")

    def translate(self, prompt: str, text: str) -> str:
        self.ensure_browser_running()
        if not self.ws:
            self.connect_target_tab()

        self.wait_for_ready_state(timeout_sec=60)

        full_text = f"{prompt}\n\n{text}" if prompt else text

        if self.service_type == 0:
            return self._translate_gemini(full_text)
        else:
            return self._translate_chatgpt(full_text)

    def _translate_gemini(self, text: str) -> str:
        # Count previous message elements
        prev_count = self.evaluate_js("document.querySelectorAll('message-content, model-response').length") or 0

        # Inject input text into rich-textarea editor
        inject_js = f"""
        (() => {{
            let editor = document.querySelector('rich-textarea div.ql-editor') || document.querySelector('div[contenteditable="true"]');
            if (!editor) return false;
            editor.focus();
            document.execCommand('selectAll', false, null);
            document.execCommand('insertText', false, {json.dumps(text)});
            editor.dispatchEvent(new Event('input', {{ bubbles: true, cancelable: true }}));
            return true;
        }})()
        """
        if not self.evaluate_js(inject_js):
            raise RuntimeError("Failed to inject input text into Gemini editor.")

        time.sleep(0.2)

        # Trigger send button click
        send_js = """
        (() => {
            let btn = document.querySelector('button.send-button, button[aria-label*="Send"], button[aria-label*="Gửi"], .send-button-container button');
            if (btn && !btn.disabled) {
                btn.click();
                return true;
            }
            return false;
        })()
        """
        self.evaluate_js(send_js)

        # Poll for response completion
        start_time = time.time()
        while time.time() - start_time < 45:
            time.sleep(0.5)
            state_js = """
            (() => {
                let responses = document.querySelectorAll('message-content, model-response');
                let stopBtn = document.querySelector('button[aria-label*="Stop"], button[aria-label*="Dừng"]');
                let isGenerating = !!stopBtn;
                let lastText = responses.length > 0 ? responses[responses.length - 1].innerText.trim() : "";
                return {
                    count: responses.length,
                    isGenerating: isGenerating,
                    lastText: lastText
                };
            })()
            """
            state = self.evaluate_js(state_js)
            if state and state.get("count", 0) > prev_count:
                if not state.get("isGenerating") and state.get("lastText"):
                    return state["lastText"]

        raise TimeoutError("Gemini response timed out or was interrupted.")

    def _translate_chatgpt(self, text: str) -> str:
        # Count previous assistant message elements
        prev_count = self.evaluate_js("document.querySelectorAll('div[data-message-author-role=\"assistant\"]').length") or 0

        # Inject input text into prompt-textarea
        inject_js = f"""
        (() => {{
            let input = document.querySelector('#prompt-textarea');
            if (!input) return false;
            input.focus();
            document.execCommand('selectAll', false, null);
            document.execCommand('insertText', false, {json.dumps(text)});
            input.dispatchEvent(new Event('input', {{ bubbles: true, cancelable: true }}));
            return true;
        }})()
        """
        if not self.evaluate_js(inject_js):
            raise RuntimeError("Failed to inject input text into ChatGPT textarea.")

        time.sleep(0.2)

        # Trigger send button click
        send_js = """
        (() => {
            let btn = document.querySelector('button[data-testid="send-button"]');
            if (btn && !btn.disabled) {
                btn.click();
                return true;
            }
            return false;
        })()
        """
        self.evaluate_js(send_js)

        # Poll for response completion
        start_time = time.time()
        while time.time() - start_time < 45:
            time.sleep(0.5)
            state_js = """
            (() => {
                let replies = document.querySelectorAll('div[data-message-author-role=\"assistant\"]');
                let stopBtn = document.querySelector('button[data-testid=\"stop-button\"]');
                let isGenerating = !!stopBtn;
                let lastText = replies.length > 0 ? replies[replies.length - 1].innerText.trim() : "";
                return {
                    count: replies.length,
                    isGenerating: isGenerating,
                    lastText: lastText
                };
            })()
            """
            state = self.evaluate_js(state_js)
            if state and state.get("count", 0) > prev_count:
                if not state.get("isGenerating") and state.get("lastText"):
                    return state["lastText"]

        raise TimeoutError("ChatGPT response timed out or was interrupted.")


class TS(basetrans):
    """Main Translator Engine for LunaTranslator GUI."""
    DEFAULT_PROMPT = "Translate the following Visual Novel dialogue accurately and naturally, matching character tone and context. Output only the translation without additional explanations:"

    def __init__(self, typename):
        super().__init__(typename)
        self.controller = None

    def init(self):
        try:
            self.controller = WebAIController(self.config)
            threading.Thread(target=self.controller.ensure_browser_running, daemon=True).start()
        except Exception as e:
            print_exc()

    def translate(self, content):
        if isinstance(content, GptTextWithDict):
            text = content.parsedtext or content.rawtext
        else:
            text = str(content)

        text = text.strip()
        if not text:
            return ""

        if not self.controller:
            self.controller = WebAIController(self.config)

        custom_prompt = self.config.get("custom_prompt", "").strip()
        if not custom_prompt:
            custom_prompt = self.DEFAULT_PROMPT

        return self.controller.translate(custom_prompt, text)
