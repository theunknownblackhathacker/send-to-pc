#!/usr/bin/env python3
"""
PC Drop - tiny local "send from phone to PC" server.
No dependencies. Python 3.8+. Stdlib only.

Run:   python server.py
Send from the PC Drop iPhone app (share sheet), an iOS Shortcut, or any browser.
Everything received is put on the Windows clipboard: just Ctrl+V.

Received files + text land in ./received/ next to this script.
"""

import os
import io
import re
import json
import time
import uuid
import html
import mimetypes
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

HERE = os.path.dirname(os.path.abspath(__file__))
RECV_DIR = os.path.join(HERE, "received")
INDEX = os.path.join(HERE, "index.json")

PORT = int(os.environ.get("PCDROP_PORT", "8765"))
TOKEN = os.environ.get("PCDROP_TOKEN", "").strip()  # empty = no auth (LAN only)

os.makedirs(RECV_DIR, exist_ok=True)


def log(msg):
    line = "[%s] %s" % (time.strftime("%H:%M:%S"), msg)
    try:
        print(line, flush=True)
    except Exception:
        pass  # no console (pythonw) or console can't show the characters
    try:
        with open(os.path.join(HERE, "pcdrop.log"), "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except Exception:
        pass


def load_index():
    try:
        with open(INDEX, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return []


def save_index(items):
    tmp = INDEX + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(items, f, ensure_ascii=False, indent=0)
    os.replace(tmp, INDEX)


INDEX_LOCK = threading.Lock()


def add_item(item):
    """Append one received item to index.json (thread-safe) and put it on the PC clipboard."""
    with INDEX_LOCK:
        items = load_index()
        items.append(item)
        save_index(items)
    Clipboard.put_item(item)


# ---------------------------------------------------------------------------
# PC clipboard: everything received is ready to paste with Ctrl+V.
#   text/link   -> text
#   file(s)     -> file list (paste in Explorer, Discord, WhatsApp, Telegram, ...)
#   one image   -> also as a picture (paste in Paint, Word, chat boxes, ...)
# Items arriving within BATCH_SECONDS of each other are pasted together.
# Turn off with:  set PCDROP_CLIPBOARD=0
# ---------------------------------------------------------------------------

CLIP_PS1 = r"""
param([string]$Mode, [string]$In)
$ErrorActionPreference = 'Stop'
Add-Type -AssemblyName System.Windows.Forms
Add-Type -AssemblyName System.Drawing
$data = New-Object System.Windows.Forms.DataObject
if ($Mode -eq 'text') {
    $t = [System.IO.File]::ReadAllText($In, [System.Text.Encoding]::UTF8)
    $data.SetText($t, [System.Windows.Forms.TextDataFormat]::UnicodeText)
} else {
    $paths = [System.IO.File]::ReadAllLines($In, [System.Text.Encoding]::UTF8)
    $list = New-Object System.Collections.Specialized.StringCollection
    foreach ($p in $paths) { if ($p) { [void]$list.Add($p) } }
    $data.SetFileDropList($list)
    if ($list.Count -eq 1) {
        $ext = [System.IO.Path]::GetExtension($list[0]).ToLower()
        if (@('.jpg', '.jpeg', '.png', '.gif', '.bmp', '.tif', '.tiff', '.webp') -contains $ext) {
            try {
                $bytes = [System.IO.File]::ReadAllBytes($list[0])
                $ms = New-Object System.IO.MemoryStream(, $bytes)
                $img = [System.Drawing.Image]::FromStream($ms)
                $o = 0
                try { $o = $img.GetPropertyItem(0x0112).Value[0] } catch {}
                switch ($o) {
                    2 { $img.RotateFlip([System.Drawing.RotateFlipType]::RotateNoneFlipX) }
                    3 { $img.RotateFlip([System.Drawing.RotateFlipType]::Rotate180FlipNone) }
                    4 { $img.RotateFlip([System.Drawing.RotateFlipType]::Rotate180FlipX) }
                    5 { $img.RotateFlip([System.Drawing.RotateFlipType]::Rotate90FlipX) }
                    6 { $img.RotateFlip([System.Drawing.RotateFlipType]::Rotate90FlipNone) }
                    7 { $img.RotateFlip([System.Drawing.RotateFlipType]::Rotate270FlipX) }
                    8 { $img.RotateFlip([System.Drawing.RotateFlipType]::Rotate270FlipNone) }
                }
                $bmp = New-Object System.Drawing.Bitmap($img)
                $data.SetImage($bmp)
                $png = New-Object System.IO.MemoryStream
                $bmp.Save($png, [System.Drawing.Imaging.ImageFormat]::Png)
                $data.SetData('PNG', $false, $png)
            } catch {
                Write-Output ('image part skipped: ' + $_.Exception.Message)
            }
        }
    }
}
[System.Windows.Forms.Clipboard]::SetDataObject($data, $true, 20, 100)
Write-Output 'ok'
"""


class Clipboard:
    ENABLED = os.name == "nt" and os.environ.get("PCDROP_CLIPBOARD", "1") != "0"
    BATCH_SECONDS = 8

    _queue = []            # pending jobs; only the newest one matters
    _cond = threading.Condition()
    _batch = []            # file paths of the current batch
    _batch_time = 0.0
    _thread = None
    _script = os.path.join(HERE, "pcdrop_clipboard.ps1")

    @classmethod
    def start(cls):
        if not cls.ENABLED:
            return
        with open(cls._script, "w", encoding="utf-8-sig") as f:
            f.write(CLIP_PS1)
        cls._thread = threading.Thread(target=cls._worker, daemon=True)
        cls._thread.start()

    @classmethod
    def put_item(cls, item):
        if item.get("kind") == "file":
            cls.put_file(os.path.join(RECV_DIR, item["name"]))
        elif item.get("text"):
            cls.put_text(item["text"])

    @classmethod
    def put_text(cls, text):
        if not cls.ENABLED:
            return
        with cls._cond:
            cls._batch = []
            cls._queue.append(("text", text))
            cls._cond.notify()

    @classmethod
    def put_file(cls, path, fresh=False):
        if not cls.ENABLED:
            return
        with cls._cond:
            now = time.time()
            if fresh or not cls._batch or now - cls._batch_time > cls.BATCH_SECONDS:
                cls._batch = []
            if path not in cls._batch:
                cls._batch.append(path)
            cls._batch_time = now
            cls._queue.append(("files", list(cls._batch)))
            cls._cond.notify()

    @classmethod
    def _worker(cls):
        import subprocess
        import tempfile
        while True:
            with cls._cond:
                while not cls._queue:
                    cls._cond.wait()
                mode, payload = cls._queue[-1]   # newest wins (it includes the whole batch)
                cls._queue.clear()
            try:
                fd, tmp = tempfile.mkstemp(prefix="pcdrop_", suffix=".txt")
                with os.fdopen(fd, "w", encoding="utf-8") as f:
                    f.write(payload if mode == "text" else "\n".join(payload))
                flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
                r = subprocess.run(
                    ["powershell.exe", "-NoProfile", "-NonInteractive", "-STA",
                     "-ExecutionPolicy", "Bypass", "-File", cls._script, "-Mode", mode, "-In", tmp],
                    capture_output=True, text=True, timeout=60, creationflags=flags)
                out = (r.stdout + r.stderr).strip()
                if r.returncode == 0 and out.endswith("ok"):
                    what = "text" if mode == "text" else "%d file(s)" % len(payload)
                    log("  -> on PC clipboard: %s (Ctrl+V)" % what)
                    if "skipped" in out:
                        log("     " + out.splitlines()[0])
                else:
                    log("  -> clipboard failed: %s" % (out[-300:] or r.returncode))
            except Exception as e:
                log("  -> clipboard failed: %s" % e)
            finally:
                try:
                    os.remove(tmp)
                except Exception:
                    pass


def safe_name(name):
    name = os.path.basename(name or "").strip()
    name = re.sub(r"[^\w.\- ()\[\]@+,=#&'!]", "_", name)  # keeps Greek/accents, drops <>:"/\\|?*
    name = name.strip(" .") or "file"
    if len(name) > 150:  # shorten the base, never the extension (.ipa, .iso, .tar.gz, ...)
        base, ext = os.path.splitext(name)
        if base.lower().endswith(".tar"):
            base, ext = base[:-4], ".tar" + ext
        name = base[:150 - len(ext)] + ext
    return name


_NAME_LOCK = threading.Lock()


def reserve_name(name):
    """Original file name in received/, or 'name (2).ext' if taken. Creates an empty
    placeholder so two uploads can't pick the same name."""
    name = safe_name(name)
    base, ext = os.path.splitext(name)
    if base.lower().endswith(".tar"):
        base, ext = base[:-4], ".tar" + ext
    with _NAME_LOCK:
        n = 1
        candidate = name
        while os.path.exists(os.path.join(RECV_DIR, candidate)):
            n += 1
            candidate = "%s (%d)%s" % (base, n, ext)
        open(os.path.join(RECV_DIR, candidate), "wb").close()
    return candidate


def parse_multipart(body, boundary):
    parts = []
    delim = b"--" + boundary
    for seg in body.split(delim):
        if seg.startswith(b"\r\n"):
            seg = seg[2:]
        if seg.endswith(b"\r\n"):
            seg = seg[:-2]
        if not seg or seg == b"--" or b"\r\n\r\n" not in seg:
            continue
        head, data = seg.split(b"\r\n\r\n", 1)
        headers = {}
        for line in head.split(b"\r\n"):
            if b":" in line:
                k, v = line.split(b":", 1)
                headers[k.decode(errors="replace").strip().lower()] = v.decode(errors="replace").strip()
        cd = headers.get("content-disposition", "")
        name = filename = None
        for tok in cd.split(";"):
            tok = tok.strip()
            if tok.startswith("name="):
                name = tok[5:].strip('"')
            elif tok.startswith("filename="):
                filename = tok[9:].strip('"')
        parts.append({"name": name, "filename": filename, "data": data})
    return parts


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *a):
        pass  # quiet

    def _send(self, code, body, ctype="text/html; charset=utf-8", extra=None):
        if isinstance(body, str):
            body = body.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _auth_ok(self, fields):
        if not TOKEN:
            return True
        q = self.path.split("?", 1)[1] if "?" in self.path else ""
        if ("t=" + TOKEN) in q:
            return True
        if fields.get("t") == TOKEN or self.headers.get("X-Token") == TOKEN:
            return True
        return False

    def do_GET(self):
        path = self.path.split("?", 1)[0]
        if path == "/":
            return self._send(200, PAGE)
        if path == "/items":
            return self._send(200, json.dumps(load_index()[::-1]),
                              "application/json; charset=utf-8")
        if path.startswith("/file/"):
            from urllib.parse import unquote, quote
            fn = safe_name(unquote(path[len("/file/"):]))
            fp = os.path.join(RECV_DIR, fn)
            if os.path.isfile(fp):
                ctype = mimetypes.guess_type(fp)[0] or "application/octet-stream"
                with open(fp, "rb") as f:
                    data = f.read()
                return self._send(200, data, ctype,
                                  {"Content-Disposition": "attachment; filename*=UTF-8''%s" % quote(fn)})
            return self._send(404, "not found", "text/plain")
        return self._send(404, "not found", "text/plain")

    def _read_body(self):
        # supports Content-Length and chunked uploads (iOS sometimes streams files chunked)
        if "chunked" in self.headers.get("Transfer-Encoding", "").lower():
            data = bytearray()
            while True:
                line = self.rfile.readline().strip()
                size = int(line.split(b";")[0] or b"0", 16)
                if size == 0:
                    while self.rfile.readline() not in (b"\r\n", b"\n", b""):
                        pass
                    break
                data += self.rfile.read(size)
                self.rfile.readline()
            return bytes(data)
        length = int(self.headers.get("Content-Length") or 0)
        return self.rfile.read(length) if length else b""

    def _stream_to_file(self, name):
        """PC Drop app upload: raw body + X-Filename header. Streamed to disk, any size."""
        fn = reserve_name(name)
        path = os.path.join(RECV_DIR, fn)
        tmp = path + ".part"
        total = 0
        try:
            total = self._copy_body(tmp)
            os.replace(tmp, path)
        except BaseException:
            for leftover in (tmp, path):  # only this upload's own files
                try:
                    os.remove(leftover)
                except OSError:
                    pass
            raise
        return fn, total

    def _copy_body(self, tmp):
        total = 0
        with open(tmp, "wb") as f:
            if "chunked" in self.headers.get("Transfer-Encoding", "").lower():
                while True:
                    line = self.rfile.readline().strip()
                    size = int(line.split(b";")[0] or b"0", 16)
                    if size == 0:
                        while self.rfile.readline() not in (b"\r\n", b"\n", b""):
                            pass
                        break
                    left = size
                    while left:
                        chunk = self.rfile.read(min(left, 1 << 20))
                        if not chunk:
                            raise ConnectionResetError("upload cut off")
                        f.write(chunk)
                        left -= len(chunk)
                        total += len(chunk)
                    self.rfile.readline()
            else:
                left = int(self.headers.get("Content-Length") or 0)
                while left:
                    chunk = self.rfile.read(min(left, 1 << 20))
                    if not chunk:
                        raise ConnectionResetError("upload cut off")
                    f.write(chunk)
                    left -= len(chunk)
                    total += len(chunk)
        return total

    def _clip(self):
        from urllib.parse import parse_qs
        q = parse_qs(self.path.split("?", 1)[1] if "?" in self.path else "")
        item_id = (q.get("id") or [""])[0]
        self._read_body()
        if not Clipboard.ENABLED:
            return self._send(400, "clipboard is only available on Windows", "text/plain")
        for it in load_index():
            if it.get("id") == item_id:
                if it.get("kind") == "file":
                    Clipboard.put_file(os.path.join(RECV_DIR, it["name"]), fresh=True)
                else:
                    Clipboard.put_text(it.get("text", ""))
                log("Copy on PC: %s" % (it.get("name") or it.get("text", "")[:60]))
                return self._send(200, "ok", "text/plain")
        return self._send(404, "not found", "text/plain")

    def do_POST(self):
        if self.path.startswith("/clip"):
            return self._clip()
        xname = self.headers.get("X-Filename")
        if xname is not None:
            from urllib.parse import unquote
            name = unquote(xname) or "file"
            if TOKEN and self.headers.get("X-Token") != TOKEN and ("t=" + TOKEN) not in self.path:
                log("%s POST %s | rejected: bad token" % (self.client_address[0], self.path))
                self.close_connection = True
                return self._send(403, "bad token", "text/plain")
            try:
                fn, total = self._stream_to_file(name)
            except Exception as e:
                log("%s POST %s | upload failed: %s" % (self.client_address[0], self.path, e))
                raise
            log("%s POST %s | app upload | %d bytes" % (self.client_address[0], self.path, total))
            log("  -> saved file %s (%.1f KB)" % (fn, total / 1024))
            add_item({"id": uuid.uuid4().hex[:8], "kind": "file",
                      "time": time.time(), "name": fn, "size": total})
            return self._send(200, "ok (1 received)", "text/plain")

        body = self._read_body()
        ctype = self.headers.get("Content-Type", "")
        ctype_l = ctype.lower()
        files = []   # (filename, bytes)
        fields = {}

        if ctype_l.startswith("multipart/form-data"):
            m = re.search(r"boundary=([^;]+)", ctype)
            boundary = m.group(1).strip().strip('"').encode() if m else b""
            for p in parse_multipart(body, boundary):
                if p["filename"] is not None:
                    files.append((p["filename"] or "file", p["data"]))
                elif p["name"]:
                    fields[p["name"]] = p["data"].decode("utf-8", errors="replace")
        elif ctype_l.startswith("application/x-www-form-urlencoded"):
            from urllib.parse import parse_qs
            for k, v in parse_qs(body.decode("utf-8", errors="replace")).items():
                fields[k] = v[0]
        elif ctype_l.startswith("application/json"):
            try:
                obj = json.loads(body.decode("utf-8", errors="replace"))
                if isinstance(obj, dict):
                    fields.update({k: str(v) for k, v in obj.items()})
                else:
                    fields["data"] = str(obj)
            except Exception:
                fields["data"] = body.decode("utf-8", errors="replace")
        elif ctype_l.startswith("text/") or not ctype_l:
            fields["data"] = body.decode("utf-8", errors="replace")
        else:
            # raw binary body (image/jpeg, application/pdf, ...) -> save as file
            ext = mimetypes.guess_extension(ctype.split(";")[0].strip()) or ".bin"
            fname = self.headers.get("X-Filename") or ("upload" + ext)
            files.append((fname, body))

        log("%s POST %s | type=%s | %d bytes | files=%d fields=%s" % (
            self.client_address[0], self.path, ctype.split(";")[0] or "-",
            len(body), len(files), list(fields.keys())))

        if not self._auth_ok(fields):
            log("  -> rejected: bad token")
            return self._send(403, "bad token", "text/plain")

        added = 0
        for name, data in files:
            if not data:
                continue
            fn = reserve_name(name)
            with open(os.path.join(RECV_DIR, fn), "wb") as f:
                f.write(data)
            log("  -> saved file %s (%.1f KB)" % (fn, len(data) / 1024))
            add_item({"id": uuid.uuid4().hex[:8], "kind": "file",
                      "time": time.time(), "name": fn, "size": len(data)})
            added += 1

        for key in ("data", "text", "url"):
            if fields.get(key, "").strip():
                log("  -> saved text: %s" % fields[key].strip()[:80])
                add_item({"id": uuid.uuid4().hex[:8], "kind": "text",
                          "time": time.time(), "text": fields[key].strip()})
                added += 1

        if not added:
            log("  -> nothing usable in request")
        return self._send(200, "ok (%d received)" % added, "text/plain")


PAGE = """<!doctype html><html><head><meta charset=utf-8>
<meta name=viewport content="width=device-width,initial-scale=1">
<title>PC Drop</title><style>
*{box-sizing:border-box}body{font:15px/1.4 system-ui,sans-serif;margin:0;background:#0f1115;color:#e6e6e6}
header{padding:14px 18px;background:#171a21;border-bottom:1px solid #262b36;position:sticky;top:0}
h1{font-size:17px;margin:0}main{max-width:820px;margin:0 auto;padding:18px}
.up{background:#171a21;border:1px solid #262b36;border-radius:12px;padding:14px;margin-bottom:18px}
input,textarea{width:100%;background:#0f1115;border:1px solid #2c333f;color:#e6e6e6;border-radius:8px;padding:10px;margin:6px 0;font:inherit}
button{background:#3b82f6;color:#fff;border:0;border-radius:8px;padding:10px 16px;font:inherit;cursor:pointer}
.item{background:#171a21;border:1px solid #262b36;border-radius:12px;padding:12px 14px;margin-bottom:10px;display:flex;gap:12px;align-items:center}
.item .meta{color:#8b93a1;font-size:12px}.item .txt{word-break:break-word;flex:1}
a{color:#7cb3ff}.row{display:flex;gap:8px;align-items:center}.grow{flex:1}
small{color:#8b93a1}
</style></head><body>
<header><h1>PC Drop</h1><small>local only - everything you send is also on this PC's clipboard: just Ctrl+V</small></header>
<main>
<div class=up>
 <div class=row><input id=t placeholder="paste text or a link" class=grow>
 <button onclick=sendText()>Send</button></div>
 <input id=f type=file multiple>
 <button onclick=sendFile()>Upload file(s)</button>
</div>
<div id=list></div>
</main>
<script>
async function load(){
 let r=await fetch('/items');let items=await r.json();
 let L=document.getElementById('list');L.innerHTML='';
 for(const it of items){
  let d=document.createElement('div');d.className='item';
  let when=new Date(it.time*1000).toLocaleString();
  if(it.kind==='file'){
   d.innerHTML=`<div class=txt>📎 <a href="/file/${encodeURIComponent(it.name)}">${it.name}</a>`
    +`<div class=meta>${(it.size/1024).toFixed(1)} KB · ${when}</div></div>`
    +`<button onclick="clip('${it.id}',this)">Copy on PC</button>`;
  }else{
   let safe=it.text.replace(/[&<>]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;'}[c]));
   let link=/^https?:\\/\\//.test(it.text)?`<a href="${it.text}" target=_blank>${safe}</a>`:safe;
   d.innerHTML=`<div class=txt>${link}<div class=meta>${when}</div></div>`
    +`<button onclick="clip('${it.id}',this)">Copy on PC</button>`;
  }
  L.appendChild(d);
 }
}
async function clip(id,b){let r=await fetch('/clip?id='+id,{method:'POST'});
 b.textContent=r.ok?'Copied ✓':'Failed';setTimeout(()=>b.textContent='Copy on PC',1500);}
async function sendText(){let v=document.getElementById('t').value;if(!v)return;
 let fd=new FormData();fd.append('data',v);await fetch('/send',{method:'POST',body:fd});
 document.getElementById('t').value='';load();}
async function sendFile(){let fs=document.getElementById('f').files;if(!fs.length)return;
 let fd=new FormData();for(const f of fs)fd.append('file',f);
 await fetch('/send',{method:'POST',body:fd});document.getElementById('f').value='';load();}
load();setInterval(load,2000);
</script></body></html>"""


class QuietServer(ThreadingHTTPServer):
    daemon_threads = True

    def handle_error(self, request, client_address):
        import sys
        exc = sys.exc_info()[1]
        # phones/browsers drop idle keep-alive sockets all the time -> ignore
        if isinstance(exc, (ConnectionResetError, ConnectionAbortedError, BrokenPipeError, TimeoutError)):
            return
        super().handle_error(request, client_address)


def main():
    srv = QuietServer(("0.0.0.0", PORT), Handler)
    print("PC Drop running.")
    print("  Dashboard (this PC):  http://localhost:%d" % PORT)
    import socket
    ips = set()
    try:
        for info in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
            ips.add(info[4][0])
    except Exception:
        pass
    for ip in sorted(ips):
        tag = "  <- Meshnet" if ip.startswith("100.") else ""
        print("  From phone:           http://%s:%d%s" % (ip, PORT, tag))
    print("  Files saved to:       %s" % RECV_DIR)
    Clipboard.start()
    print("  Auto-copy to clipboard: %s" % ("ON (Ctrl+V to paste)" if Clipboard.ENABLED else "off"))
    if TOKEN:
        print("  Token auth: ON")
    print("Ctrl+C to stop.")
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("\nbye")


if __name__ == "__main__":
    main()
