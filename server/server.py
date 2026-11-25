#!/usr/bin/env python3
"""
PC Drop - tiny local "send from phone to PC" server.
No dependencies. Python 3.8+. Stdlib only.

Run:   python server.py
Then open the dashboard on your PC, and POST from your phone
(iOS Shortcut in the Share Sheet, or any phone browser).

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


def safe_name(name):
    name = os.path.basename(name or "").strip()
    name = re.sub(r"[^\w.\- ()\[\]]", "_", name)  # keeps Greek/accents, drops <>:"/\\|?*
    return name[:120] or "file"


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
        fn = "%s_%s_%s" % (time.strftime("%Y%m%d_%H%M%S"), uuid.uuid4().hex[:4], safe_name(name))
        path = os.path.join(RECV_DIR, fn)
        tmp = path + ".part"
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
        os.replace(tmp, path)
        return fn, total

    def do_POST(self):
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
                for leftover in os.listdir(RECV_DIR):
                    if leftover.endswith(".part"):
                        try:
                            os.remove(os.path.join(RECV_DIR, leftover))
                        except OSError:
                            pass
                raise
            items = load_index()
            items.append({"id": uuid.uuid4().hex[:8], "kind": "file",
                          "time": time.time(), "name": fn, "size": total})
            save_index(items)
            log("%s POST %s | app upload | %d bytes" % (self.client_address[0], self.path, total))
            log("  -> saved file %s (%.1f KB)" % (fn, total / 1024))
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

        items = load_index()
        added = 0
        for name, data in files:
            if not data:
                continue
            fn = "%s_%s_%s" % (time.strftime("%Y%m%d_%H%M%S"), uuid.uuid4().hex[:4], safe_name(name))
            with open(os.path.join(RECV_DIR, fn), "wb") as f:
                f.write(data)
            items.append({"id": uuid.uuid4().hex[:8], "kind": "file",
                          "time": time.time(), "name": fn, "size": len(data)})
            log("  -> saved file %s (%.1f KB)" % (fn, len(data) / 1024))
            added += 1

        for key in ("data", "text", "url"):
            if fields.get(key, "").strip():
                items.append({"id": uuid.uuid4().hex[:8], "kind": "text",
                              "time": time.time(), "text": fields[key].strip()})
                log("  -> saved text: %s" % fields[key].strip()[:80])
                added += 1

        if not added:
            log("  -> nothing usable in request")
        save_index(items)
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
<header><h1>PC Drop</h1><small>local only - things sent from your phone show up here</small></header>
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
    +`<div class=meta>${(it.size/1024).toFixed(1)} KB · ${when}</div></div>`;
  }else{
   let safe=it.text.replace(/[&<>]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;'}[c]));
   let link=/^https?:\\/\\//.test(it.text)?`<a href="${it.text}" target=_blank>${safe}</a>`:safe;
   d.innerHTML=`<div class=txt>${link}<div class=meta>${when}</div></div>`
    +`<button onclick='navigator.clipboard.writeText(${JSON.stringify(it.text)})'>Copy</button>`;
  }
  L.appendChild(d);
 }
}
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
    if TOKEN:
        print("  Token auth: ON")
    print("Ctrl+C to stop.")
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("\nbye")


if __name__ == "__main__":
    main()
