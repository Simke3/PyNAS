import base64
import collections
import hashlib
import hmac
import html
import json
import mimetypes
import os
import platform
import secrets
import shutil
import socket
import sys
import threading
import time
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
 
import psutil
from PySide6.QtCore import QEvent, QRectF, Qt, QTimer, QUrl
from PySide6.QtGui import (QColor, QDesktopServices, QFont, QIcon, QKeySequence, QPainter,
                           QPainterPath, QPen, QShortcut)
from PySide6.QtWidgets import (
    QAbstractItemView, QApplication, QCheckBox, QDialog, QFileDialog, QFrame,
    QGridLayout, QHBoxLayout, QHeaderView, QInputDialog, QLabel, QLineEdit,
    QListWidget, QMainWindow, QMessageBox, QPlainTextEdit, QPushButton,
    QScrollArea, QSpinBox, QStackedWidget, QTableWidget, QTableWidgetItem,
    QVBoxLayout, QWidget,
)
 
BLUE = "#0095d5"
MAGENTA = "#a80fb5"
ORANGE = "#e8892b"
CARD_BG = "#252525"
CONFIG_PATH = os.path.join(os.path.expanduser("~"), ".pynas.json")
 
 
# ----------------------------------------------------------------- helpers
def fmt_size(n):
    n = float(n)
    for unit in ("B", "KiB", "MiB", "GiB", "TiB"):
        if n < 1024 or unit == "TiB":
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024
 
 
def folder_size(path):
    total = 0
    for root, _dirs, files in os.walk(path):
        for f in files:
            try:
                total += os.path.getsize(os.path.join(root, f))
            except OSError:
                pass
    return total
 
 
def cpu_model():
    try:
        with open("/proc/cpuinfo", encoding="utf-8") as f:
            for line in f:
                if line.lower().startswith("model name"):
                    return line.split(":", 1)[1].strip()
    except OSError:
        pass
    return platform.processor() or platform.machine()
 
 
def local_ips():
    out = []
    for name, addrs in psutil.net_if_addrs().items():
        for a in addrs:
            if a.family == socket.AF_INET and not a.address.startswith("127."):
                out.append((name, a.address))
    return out
 
 
# ------------------------------------------------------------------ config
class Config:
    def __init__(self):
        self.lock = threading.RLock()
        self.data = {
            "port": 8080,
            "data_root": os.path.join(os.path.expanduser("~"), "PyNAS"),
            "shares": [],   # {"name","path","readonly"}
            "users": {},    # name -> {"salt","hash"}
        }
        try:
            with open(CONFIG_PATH, encoding="utf-8") as f:
                self.data.update(json.load(f))
        except Exception:
            pass
        os.makedirs(self.data["data_root"], exist_ok=True)
        self._cache = {}
 
    def save(self):
        with self.lock:
            with open(CONFIG_PATH, "w", encoding="utf-8") as f:
                json.dump(self.data, f, indent=2)
 
    @staticmethod
    def _hash(pw, salt):
        return hashlib.pbkdf2_hmac("sha256", pw.encode(), bytes.fromhex(salt), 120_000).hex()
 
    def set_password(self, user, pw):
        salt = secrets.token_hex(16)
        with self.lock:
            self.data["users"][user] = {"salt": salt, "hash": self._hash(pw, salt)}
            self._cache.clear()
            self.save()
 
    def delete_user(self, user):
        with self.lock:
            self.data["users"].pop(user, None)
            self._cache.clear()
            self.save()
 
    def check(self, user, pw):
        key = (user, hashlib.sha256(pw.encode()).hexdigest())
        now = time.time()
        if self._cache.get(key, 0) > now:
            return True
        u = self.data["users"].get(user)
        if not u:
            return False
        ok = hmac.compare_digest(self._hash(pw, u["salt"]), u["hash"])
        if ok:
            self._cache[key] = now + 300
        return ok
 
 
# ------------------------------------------------------------ file server
PAGE_CSS = """
body{background:#1b1b1b;color:#ddd;font-family:Segoe UI,Arial,sans-serif;margin:0}
header{background:#111;padding:14px 24px;font-size:22px;color:#0095d5;font-weight:600}
main{padding:24px;max-width:1000px}
a{color:#0095d5;text-decoration:none} a:hover{text-decoration:underline}
table{width:100%;border-collapse:collapse;background:#252525;border:1px solid #333}
td,th{padding:9px 14px;text-align:left;border-bottom:1px solid #333}
th{color:#999;font-weight:500}
.up{margin:16px 0;padding:14px;background:#252525;border:1px solid #333}
button{background:#0095d5;color:#fff;border:0;padding:7px 16px;cursor:pointer}
.x{color:#e55;cursor:pointer}
"""
 
 
def make_handler(cfg, log):
    class Handler(BaseHTTPRequestHandler):
        server_version = "PyNAS/1.0"
 
        def log_message(self, fmt, *args):
            log(f"{self.client_address[0]} {fmt % args}")
 
        # --- helpers
        def _send(self, code, body=b"", ctype="text/html; charset=utf-8", extra=None):
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            for k, v in (extra or {}).items():
                self.send_header(k, v)
            self.end_headers()
            if self.command != "HEAD":
                self.wfile.write(body)
 
        def _page(self, title, inner):
            doc = (f"<!doctype html><meta charset=utf-8><title>{html.escape(title)}</title>"
                   f"<style>{PAGE_CSS}</style><header>PyNAS</header><main>{inner}</main>")
            self._send(200, doc.encode())
 
        def _auth(self):
            hdr = self.headers.get("Authorization", "")
            if hdr.startswith("Basic "):
                try:
                    user, _, pw = base64.b64decode(hdr[6:]).decode().partition(":")
                    if cfg.check(user, pw):
                        return user
                except Exception:
                    pass
            self._send(401, b"Authentication required", "text/plain",
                       {"WWW-Authenticate": 'Basic realm="PyNAS"'})
            return None
 
        def _resolve(self):
            path = urllib.parse.unquote(urllib.parse.urlsplit(self.path).path)
            parts = [p for p in path.split("/") if p]
            if not parts:
                return "root", None, path
            share = next((s for s in cfg.data["shares"] if s["name"] == parts[0]), None)
            if not share:
                return "none", None, path
            root = os.path.realpath(share["path"])
            full = os.path.realpath(os.path.join(root, *parts[1:]))
            if full != root and not full.startswith(root + os.sep):
                return "none", None, path
            return share, full, path
 
        # --- methods
        def do_GET(self):
            if not self._auth():
                return
            share, full, path = self._resolve()
            if share == "root":
                rows = "".join(
                    f"<tr><td><a href='/{urllib.parse.quote(s['name'])}/'>{html.escape(s['name'])}</a></td>"
                    f"<td>{'Read-only' if s['readonly'] else 'Read/Write'}</td></tr>"
                    for s in cfg.data["shares"])
                return self._page("Shares", f"<h2>Shares</h2><table><tr><th>Name</th><th>Access</th></tr>{rows}</table>")
            if share == "none" or not os.path.exists(full):
                return self._send(404, b"Not found", "text/plain")
            if os.path.isdir(full):
                if not path.endswith("/"):
                    return self._send(301, b"", extra={"Location": path + "/"})
                return self._listing(share, full, path)
            try:
                size = os.path.getsize(full)
                self.send_response(200)
                self.send_header("Content-Type", mimetypes.guess_type(full)[0] or "application/octet-stream")
                self.send_header("Content-Length", str(size))
                self.end_headers()
                with open(full, "rb") as f:
                    shutil.copyfileobj(f, self.wfile, 1 << 20)
            except (BrokenPipeError, ConnectionResetError):
                pass
 
        def _listing(self, share, full, path):
            entries = sorted(os.scandir(full), key=lambda e: (not e.is_dir(), e.name.lower()))
            rows = ""
            if path.strip("/").count("/") >= 0 and path.strip("/") != share["name"]:
                rows += "<tr><td><a href='../'>⬆ ..</a></td><td></td><td></td></tr>"
            else:
                rows += "<tr><td><a href='/'>⬆ Shares</a></td><td></td><td></td></tr>"
            for e in entries:
                q = urllib.parse.quote(e.name)
                if e.is_dir():
                    rows += f"<tr><td>📁 <a href='{q}/'>{html.escape(e.name)}</a></td><td>-</td><td></td></tr>"
                else:
                    x = "" if share["readonly"] else f"<span class=x data-n=\"{html.escape(e.name)}\" onclick='del(this.dataset.n)'>✕</span>"
                    rows += (f"<tr><td>📄 <a href='{q}'>{html.escape(e.name)}</a></td>"
                             f"<td>{fmt_size(e.stat().st_size)}</td><td>{x}</td></tr>")
            upload = ""
            if not share["readonly"]:
                upload = ("<div class=up><input type=file id=f multiple> <button onclick='up()'>Upload</button></div>"
                          "<script>async function up(){for(const f of document.getElementById('f').files)"
                          "{await fetch(location.pathname+encodeURIComponent(f.name),{method:'PUT',body:f});}location.reload();}"
                          "async function del(n){if(!confirm('Delete '+n+'?'))return;"
                          "await fetch(location.pathname+encodeURIComponent(n),{method:'DELETE'});location.reload();}</script>")
            self._page(path, f"<h2>{html.escape(path)}</h2>{upload}"
                             f"<table><tr><th>Name</th><th>Size</th><th></th></tr>{rows}</table>")
 
        def do_PUT(self):
            user = self._auth()
            if not user:
                return
            share, full, _ = self._resolve()
            if share in ("root", "none"):
                return self._send(404, b"Not found", "text/plain")
            if share["readonly"]:
                return self._send(403, b"Share is read-only", "text/plain")
            if os.path.isdir(full):
                return self._send(400, b"Is a directory", "text/plain")
            try:
                length = int(self.headers.get("Content-Length", "0"))
                os.makedirs(os.path.dirname(full), exist_ok=True)
                tmp = full + ".part"
                with open(tmp, "wb") as f:
                    left = length
                    while left > 0:
                        chunk = self.rfile.read(min(1 << 20, left))
                        if not chunk:
                            break
                        f.write(chunk)
                        left -= len(chunk)
                if left:
                    os.remove(tmp)
                    return self._send(400, b"Incomplete upload", "text/plain")
                os.replace(tmp, full)
                self._send(201, b"Created", "text/plain")
            except Exception as ex:  # noqa
                self._send(500, str(ex).encode(), "text/plain")
 
        def do_DELETE(self):
            if not self._auth():
                return
            share, full, _ = self._resolve()
            if share in ("root", "none") or not os.path.isfile(full):
                return self._send(404, b"Not found", "text/plain")
            if share["readonly"]:
                return self._send(403, b"Share is read-only", "text/plain")
            os.remove(full)
            self._send(200, b"Deleted", "text/plain")
 
    return Handler
 
 
class NasService:
    def __init__(self, cfg):
        self.cfg = cfg
        self.httpd = None
        self.logs = collections.deque(maxlen=500)
 
    def log(self, msg):
        self.logs.append(time.strftime("%H:%M:%S  ") + msg)
 
    @property
    def running(self):
        return self.httpd is not None
 
    def start(self):
        if not self.cfg.data["users"]:
            raise RuntimeError("Create at least one user in Credentials first.")
        self.httpd = ThreadingHTTPServer(("0.0.0.0", int(self.cfg.data["port"])),
                                         make_handler(self.cfg, self.log))
        self.httpd.daemon_threads = True
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()
        self.log(f"Service started on port {self.cfg.data['port']}")
 
    def stop(self):
        if self.httpd:
            self.httpd.shutdown()
            self.httpd.server_close()
            self.httpd = None
            self.log("Service stopped")
 
 
# ----------------------------------------------------------------- stats
class Stats:
    def __init__(self):
        psutil.cpu_percent(percpu=True)
        self.cpu = [0] * (psutil.cpu_count() or 1)
        self.cpu_avg = 0
        self.cpu_hist = collections.deque([0] * 60, maxlen=60)
        self.mem = psutil.virtual_memory()
        self.mem_hist = collections.deque([0] * 60, maxlen=60)
        self.rx = collections.deque([0] * 60, maxlen=60)
        self.tx = collections.deque([0] * 60, maxlen=60)
        self._last = (time.time(), psutil.net_io_counters())
 
    def tick(self):
        self.cpu = psutil.cpu_percent(percpu=True)
        self.cpu_avg = sum(self.cpu) / len(self.cpu)
        self.cpu_hist.append(self.cpu_avg)
        self.mem = psutil.virtual_memory()
        self.mem_hist.append(self.mem.percent)
        t, io = time.time(), psutil.net_io_counters()
        dt = max(t - self._last[0], 0.001)
        self.rx.append((io.bytes_recv - self._last[1].bytes_recv) * 8 / dt / 1e6)
        self.tx.append((io.bytes_sent - self._last[1].bytes_sent) * 8 / dt / 1e6)
        self._last = (t, io)
 
 
# --------------------------------------------------------------- charts
class BarChart(QWidget):
    def __init__(self, color=BLUE, parent=None):
        super().__init__(parent)
        self.values, self.color = [], QColor(color)
        self.setMinimumHeight(120)
 
    def set_values(self, v):
        self.values = list(v)
        self.update()
 
    def paintEvent(self, _):
        p = QPainter(self)
        w, h, l, b, t = self.width(), self.height(), 34, 20, 6
        p.setFont(QFont("Sans", 7))
        for v in (0, 20, 40, 60, 80, 100):
            y = t + (h - b - t) * (1 - v / 100)
            p.setPen(QColor("#333"))
            p.drawLine(l, int(y), w, int(y))
            p.setPen(QColor("#888"))
            p.drawText(QRectF(0, y - 6, l - 6, 12), Qt.AlignRight | Qt.AlignVCenter, str(v))
        n = len(self.values)
        if n:
            bw = (w - l - 4) / n
            for i, v in enumerate(self.values):
                bh = (h - b - t) * min(v, 100) / 100
                p.fillRect(QRectF(l + i * bw + bw * 0.15, h - b - bh, bw * 0.7, bh), self.color)
                if i % 5 == 0:
                    p.setPen(QColor("#888"))
                    p.drawText(QRectF(l + i * bw - 8, h - b + 2, 24, 14), Qt.AlignCenter, str(i + 1))
 
 
class Donut(QWidget):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.value = 0
        self.setMinimumSize(150, 150)
 
    def set_value(self, v):
        self.value = v
        self.update()
 
    def paintEvent(self, _):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        s = min(self.width(), self.height()) - 24
        r = QRectF((self.width() - s) / 2, (self.height() - s) / 2, s, s)
        pen = QPen(QColor("#333"), 12)
        p.setPen(pen)
        p.drawEllipse(r)
        pen.setColor(QColor(BLUE))
        pen.setCapStyle(Qt.FlatCap)
        p.setPen(pen)
        p.drawArc(r, 90 * 16, int(-self.value * 3.6 * 16))
        p.setPen(QColor("white"))
        p.setFont(QFont("Sans", 22))
        p.drawText(r.adjusted(0, -12, 0, -12), Qt.AlignCenter, f"{self.value:.0f}%")
        p.setFont(QFont("Sans", 9))
        p.drawText(r.adjusted(0, 34, 0, 34), Qt.AlignCenter, "Avg Usage")
 
 
class MemDonut(QWidget):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.segs = []
        self.setMinimumSize(170, 170)
 
    def set_segments(self, segs):
        self.segs = segs
        self.update()
 
    def paintEvent(self, _):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        s = min(self.width(), self.height()) - 10
        r = QRectF((self.width() - s) / 2, (self.height() - s) / 2, s, s)
        total = sum(v for v, _c in self.segs) or 1
        start = 90 * 16
        p.setPen(Qt.NoPen)
        for v, c in self.segs:
            span = int(-v / total * 360 * 16)
            p.setBrush(QColor(c))
            p.drawPie(r, start, span)
            start += span
        p.setBrush(QColor(CARD_BG))
        p.drawEllipse(r.adjusted(s * 0.25, s * 0.25, -s * 0.25, -s * 0.25))
 
 
class LineChart(QWidget):
    def __init__(self, colors=(BLUE,), fixed_max=None, unit="", parent=None):
        super().__init__(parent)
        self.series, self.colors, self.fixed_max, self.unit = [], [QColor(c) for c in colors], fixed_max, unit
        self.setMinimumHeight(120)
 
    def set_series(self, series):
        self.series = [list(s) for s in series]
        self.update()
 
    def paintEvent(self, _):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        w, h, l, b, t = self.width(), self.height(), 44, 8, 8
        mx = self.fixed_max or max([1.0] + [max(s) for s in self.series if s]) * 1.15
        p.setFont(QFont("Sans", 7))
        for i in range(5):
            y = t + (h - b - t) * i / 4
            p.setPen(QColor("#333"))
            p.drawLine(l, int(y), w, int(y))
            p.setPen(QColor("#888"))
            val = mx * (1 - i / 4)
            p.drawText(QRectF(0, y - 6, l - 6, 12), Qt.AlignRight | Qt.AlignVCenter,
                       f"{val:.0f}{self.unit}" if mx >= 10 else f"{val:.1f}{self.unit}")
        for idx, s in enumerate(self.series):
            if len(s) < 2:
                continue
            path = QPainterPath()
            for i, v in enumerate(s):
                x = l + (w - l - 4) * i / (len(s) - 1)
                y = t + (h - b - t) * (1 - min(v, mx) / mx)
                path.moveTo(x, y) if i == 0 else path.lineTo(x, y)
            if idx == 0 and len(self.series) > 1:
                fill = QPainterPath(path)
                fill.lineTo(w - 4, h - b)
                fill.lineTo(l, h - b)
                c = QColor(self.colors[0])
                c.setAlpha(60)
                p.fillPath(fill, c)
            p.setPen(QPen(self.colors[idx % len(self.colors)], 2))
            p.drawPath(path)
 
 
# ---------------------------------------------------------------- widgets
class Card(QFrame):
    def __init__(self, title, body=None, parent=None):
        super().__init__(parent)
        self.setObjectName("card")
        self.title, self.span = title, 1
        lay = QVBoxLayout(self)
        lay.setContentsMargins(16, 14, 16, 14)
        t = QLabel(title)
        t.setObjectName("cardTitle")
        lay.addWidget(t)
        if body is not None:
            lay.addWidget(body, 1)
        self.lay = lay
 
 
def page_title(text, parent_layout, button=None):
    row = QHBoxLayout()
    t = QLabel(text)
    t.setObjectName("pageTitle")
    row.addWidget(t)
    row.addStretch()
    if button:
        row.addWidget(button)
    parent_layout.addLayout(row)
    line = QFrame()
    line.setFixedHeight(1)
    line.setStyleSheet("background:#333")
    parent_layout.addWidget(line)
 
 
def make_table(headers):
    t = QTableWidget(0, len(headers))
    t.setHorizontalHeaderLabels(headers)
    t.setSelectionBehavior(QAbstractItemView.SelectRows)
    t.setEditTriggers(QAbstractItemView.NoEditTriggers)
    t.verticalHeader().setVisible(False)
    t.horizontalHeader().setSectionResizeMode(QHeaderView.Stretch)
    return t
 
 
def fill_table(t, rows):
    t.setRowCount(len(rows))
    for r, row in enumerate(rows):
        for c, v in enumerate(row):
            t.setItem(r, c, QTableWidgetItem(str(v)))
 
 
# ---------------------------------------------------------------- pages
class Dashboard(QWidget):
    def __init__(self, stats, cfg, goto):
        super().__init__()
        self.stats, self.cfg, self.goto, self._n = stats, cfg, goto, 0
        outer = QVBoxLayout(self)
        outer.setContentsMargins(16, 16, 16, 0)
        cfgbtn = QPushButton("Configure")
        page_title("Dashboard", outer, cfgbtn)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.NoFrame)
        inner = QWidget()
        grid = QGridLayout(inner)
        grid.setSpacing(14)
        self.grid, self.cards = grid, []
        scroll.setWidget(inner)
        outer.addWidget(scroll)
 
        # System information
        sysw = QWidget()
        sl = QHBoxLayout(sysw)
        logo = QLabel("◆\nPyNAS")
        logo.setAlignment(Qt.AlignCenter)
        logo.setStyleSheet(f"color:{BLUE};font-size:26px;font-weight:600;background:#2d2d2d;padding:18px")
        sl.addWidget(logo)
        form = QGridLayout()
        self.uptime = QLabel()
        for i, (k, v) in enumerate([("Platform:", platform.system() + " " + platform.machine()),
                                    ("Hostname:", socket.gethostname()),
                                    ("OS:", platform.platform()[:38]),
                                    ("Edition:", "Community"),
                                    ("Version:", "PyNAS 1.0"),
                                    ("Uptime:", "")]):
            a = QLabel(k)
            a.setStyleSheet("font-weight:bold")
            form.addWidget(a, i, 0)
            form.addWidget(self.uptime if k == "Uptime:" else QLabel(v), i, 1)
        sl.addLayout(form, 1)
        self._add(Card("System Information", sysw), 2)
 
        self.percore = BarChart(BLUE)
        self._add(Card("CPU Usage Per Core", self.percore), 2)
        self.donut = Donut()
        self._add(Card("CPU Usage", self.donut), 1)
        model = QLabel(cpu_model() + f"\n{psutil.cpu_count(logical=False) or '?'} cores / {psutil.cpu_count()} threads")
        model.setAlignment(Qt.AlignCenter)
        model.setWordWrap(True)
        model.setStyleSheet("font-size:14px")
        self._add(Card("CPU Model", model), 1)
 
        # Storage
        stw = QWidget()
        sv = QVBoxLayout(stw)
        self.stlabel = QLabel()
        self.stlabel.setTextFormat(Qt.RichText)
        sv.addWidget(self.stlabel)
        mk = QPushButton("＋ Create Dataset")
        mk.clicked.connect(lambda: self.goto(2))
        sv.addWidget(mk)
        self._add(Card("Storage", stw), 2)
 
        # Memory
        mw = QWidget()
        ml = QHBoxLayout(mw)
        self.memlabel = QLabel()
        self.memlabel.setTextFormat(Qt.RichText)
        ml.addWidget(self.memlabel)
        self.memdonut = MemDonut()
        ml.addWidget(self.memdonut)
        self._add(Card("Memory", mw), 2)
 
        self.recent = LineChart((BLUE,), fixed_max=100)
        self._add(Card("CPU Recent Usage", self.recent), 2)
 
        # Network
        nw = QWidget()
        nl = QVBoxLayout(nw)
        ips = "   ".join(f"{n}: {a}" for n, a in local_ips()) or "n/a"
        nl.addWidget(QLabel(f"Addresses:  {ips}"))
        self.netchart = LineChart((BLUE, ORANGE), unit=" Mb/s")
        nl.addWidget(self.netchart)
        self.netlabel = QLabel()
        nl.addWidget(self.netlabel)
        self._add(Card("Network", nw), 3)
 
        help_ = QLabel(
            "PyNAS shares folders over the network: create a user in <b>Credentials</b>, "
            "a folder in <b>Datasets</b>, then share it in <b>Shares</b>.<br><br>"
            "From another device, open the server address in a browser (upload, download, delete) "
            "or use <code>curl -u user:pass -T file http://IP:port/share/file</code>.<br><br>"
            "PyNAS is an independent project and is not affiliated with TrueNAS.")
        help_.setWordWrap(True)
        help_.setTextFormat(Qt.RichText)
        help_.setAlignment(Qt.AlignTop)
        self._add(Card("PyNAS Help", help_), 3)
        for c in range(6):
            grid.setColumnStretch(c, 1)
        cfgbtn.clicked.connect(self.configure)
        self.layout_cards()
 
    def _add(self, card, span=1):
        card.span = span
        self.cards.append(card)
 
    def layout_cards(self):
        hidden = set(self.cfg.data.get("hidden_cards", []))
        for c in self.cards:
            self.grid.removeWidget(c)
            c.hide()
        row = col = 0
        for c in self.cards:
            if c.title in hidden:
                continue
            if col + c.span > 6:
                row, col = row + 1, 0
            self.grid.addWidget(c, row, col, 1, c.span)
            c.show()
            col += c.span
 
    def configure(self):
        dlg = QDialog(self)
        dlg.setWindowTitle("Dashboard Configuration")
        dlg.setMinimumWidth(340)
        lay = QVBoxLayout(dlg)
        lay.addWidget(QLabel("Choose which widgets are shown on the Dashboard:"))
        hidden = set(self.cfg.data.get("hidden_cards", []))
        boxes = []
        for c in self.cards:
            cb = QCheckBox(c.title)
            cb.setChecked(c.title not in hidden)
            lay.addWidget(cb)
            boxes.append((c.title, cb))
        row = QHBoxLayout()
        reset = QPushButton("Reset")
        reset.clicked.connect(lambda: [cb.setChecked(True) for _t, cb in boxes])
        save = QPushButton("Save")
        save.clicked.connect(dlg.accept)
        row.addWidget(reset)
        row.addStretch()
        row.addWidget(save)
        lay.addLayout(row)
        if dlg.exec() == QDialog.Accepted:
            self.cfg.data["hidden_cards"] = [t for t, cb in boxes if not cb.isChecked()]
            self.cfg.save()
            self.layout_cards()
 
    def refresh(self):
        s = self.stats
        self.percore.set_values(s.cpu)
        self.donut.set_value(s.cpu_avg)
        self.recent.set_series([s.cpu_hist])
        up = int(time.time() - psutil.boot_time())
        self.uptime.setText(f"{up // 86400} days {up % 86400 // 3600} hours {up % 3600 // 60} minutes")
        m = s.mem
        cached = getattr(m, "cached", 0) + getattr(m, "buffers", 0)
        used = max(m.total - m.available - cached, 0)
        g = lambda x: f"{x / 2**30:.1f}"
        self.memlabel.setText(
            f"<span style='font-size:34px'>{g(m.total)}</span> GiB<br>total memory<br><br>"
            f"<span style='color:{BLUE}'>●</span> Free: {g(m.available - cached if m.available > cached else m.available)} GiB<br>"
            f"<span style='color:{MAGENTA}'>●</span> Cache: {g(cached)} GiB<br>"
            f"<span style='color:{ORANGE}'>●</span> Services: {g(used)} GiB")
        self.memdonut.set_segments([(m.available, BLUE), (cached, MAGENTA), (used, ORANGE)])
        self.netchart.set_series([s.rx, s.tx])
        self.netlabel.setText(f"<span style='color:{BLUE}'>●</span> In: {s.rx[-1]:.2f} Mb/s    "
                              f"<span style='color:{ORANGE}'>●</span> Out: {s.tx[-1]:.2f} Mb/s")
        self.netlabel.setTextFormat(Qt.RichText)
        if self._n % 10 == 0:
            self._storage()
        self._n += 1
 
    def _storage(self):
        total = used = free = disks = 0
        seen = set()
        for part in psutil.disk_partitions():
            if part.device in seen:
                continue
            seen.add(part.device)
            try:
                u = psutil.disk_usage(part.mountpoint)
            except OSError:
                continue
            total, used, free, disks = total + u.total, used + u.used, free + u.free, disks + 1
        ok = f"<span style='color:#4caf50'>●</span>"
        pct = used / total * 100 if total else 0
        self.stlabel.setText(
            f"{ok} Pool Status: ONLINE<br>{ok} Used Space: {pct:.2f}%<br>"
            f"{ok} Free Space: {fmt_size(free)}<br>{ok} Total Capacity: {fmt_size(total)}<br>"
            f"{ok} Volumes: {disks}<br>{ok} Data root: {self.cfg.data['data_root']}")
 
 
class StoragePage(QWidget):
    def __init__(self):
        super().__init__()
        lay = QVBoxLayout(self)
        lay.setContentsMargins(16, 16, 16, 16)
        btn = QPushButton("Refresh")
        btn.clicked.connect(self.refresh)
        page_title("Storage", lay, btn)
        self.table = make_table(["Device", "Mount", "Type", "Total", "Used", "Free", "Used %"])
        lay.addWidget(self.table)
 
    def refresh(self):
        rows = []
        for p in psutil.disk_partitions():
            try:
                u = psutil.disk_usage(p.mountpoint)
            except OSError:
                continue
            rows.append([p.device, p.mountpoint, p.fstype, fmt_size(u.total), fmt_size(u.used),
                         fmt_size(u.free), f"{u.percent:.1f}%"])
        fill_table(self.table, rows)
 
 
class DatasetsPage(QWidget):
    def __init__(self, cfg):
        super().__init__()
        self.cfg = cfg
        lay = QVBoxLayout(self)
        lay.setContentsMargins(16, 16, 16, 16)
        add = QPushButton("＋ Add Dataset")
        add.clicked.connect(self.add)
        page_title("Datasets", lay, add)
        lay.addWidget(QLabel(f"Data root: {cfg.data['data_root']}"))
        self.table = make_table(["Name", "Path", "Size"])
        lay.addWidget(self.table)
        row = QHBoxLayout()
        for text, fn in (("Open folder", self.open), ("Delete", self.delete), ("Refresh", self.refresh)):
            b = QPushButton(text)
            b.clicked.connect(fn)
            row.addWidget(b)
        row.addStretch()
        lay.addLayout(row)
 
    def _sel(self):
        r = self.table.currentRow()
        return self.table.item(r, 1).text() if r >= 0 else None
 
    def refresh(self):
        rows = []
        root = self.cfg.data["data_root"]
        for e in sorted(os.scandir(root), key=lambda e: e.name.lower()):
            if e.is_dir():
                rows.append([e.name, e.path, fmt_size(folder_size(e.path))])
        fill_table(self.table, rows)
 
    def add(self):
        name, ok = QInputDialog.getText(self, "Add Dataset", "Dataset name:")
        name = name.strip()
        if not ok or not name:
            return
        if any(c in name for c in '/\\:*?"<>|') or name in (".", ".."):
            return QMessageBox.warning(self, "PyNAS", "Invalid name.")
        try:
            os.makedirs(os.path.join(self.cfg.data["data_root"], name), exist_ok=False)
        except OSError as e:
            return QMessageBox.warning(self, "PyNAS", str(e))
        self.refresh()
 
    def open(self):
        if p := self._sel():
            QDesktopServices.openUrl(QUrl.fromLocalFile(p))
 
    def delete(self):
        p = self._sel()
        if p and QMessageBox.question(self, "Delete", f"Permanently delete {p} and all its contents?") == QMessageBox.Yes:
            if any(s["path"] == p for s in self.cfg.data["shares"]):
                return QMessageBox.warning(self, "PyNAS", "This dataset is shared - remove the share first.")
            shutil.rmtree(p, ignore_errors=True)
            self.refresh()
 
 
class ShareDialog(QDialog):
    def __init__(self, root, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Add Share")
        self.setMinimumWidth(460)
        lay = QVBoxLayout(self)
        self.name = QLineEdit()
        self.name.setPlaceholderText("Share name (e.g. documents)")
        self.path = QLineEdit(root)
        browse = QPushButton("Browse…")
        browse.clicked.connect(lambda: self.path.setText(
            QFileDialog.getExistingDirectory(self, "Folder", self.path.text()) or self.path.text()))
        self.ro = QCheckBox("Read-only")
        lay.addWidget(QLabel("Name"))
        lay.addWidget(self.name)
        lay.addWidget(QLabel("Path"))
        r = QHBoxLayout()
        r.addWidget(self.path, 1)
        r.addWidget(browse)
        lay.addLayout(r)
        lay.addWidget(self.ro)
        ok = QPushButton("Save")
        ok.clicked.connect(self.accept)
        lay.addWidget(ok)
 
 
class SharesPage(QWidget):
    def __init__(self, cfg, svc, on_state):
        super().__init__()
        self.cfg, self.svc, self.on_state = cfg, svc, on_state
        lay = QVBoxLayout(self)
        lay.setContentsMargins(16, 16, 16, 16)
        add = QPushButton("＋ Add Share")
        add.clicked.connect(self.add)
        page_title("Shares", lay, add)
        top = QHBoxLayout()
        self.status = QLabel()
        self.toggle = QPushButton()
        self.toggle.clicked.connect(self.toggle_service)
        top.addWidget(self.status, 1)
        self.browse = QPushButton("Open in browser")
        self.browse.clicked.connect(lambda: QDesktopServices.openUrl(
            QUrl(f"http://localhost:{self.cfg.data['port']}")))
        top.addWidget(self.browse)
        top.addWidget(self.toggle)
        lay.addLayout(top)
        self.urls = QLabel()
        self.urls.setTextInteractionFlags(Qt.TextSelectableByMouse)
        lay.addWidget(self.urls)
        self.table = make_table(["Name", "Path", "Access"])
        lay.addWidget(self.table)
        row = QHBoxLayout()
        for text, fn in (("Toggle Read-only", self.toggle_ro), ("Remove", self.remove)):
            b = QPushButton(text)
            b.clicked.connect(fn)
            row.addWidget(b)
        row.addStretch()
        lay.addLayout(row)
 
    def refresh(self):
        shares = self.cfg.data["shares"]
        fill_table(self.table, [[s["name"], s["path"], "Read-only" if s["readonly"] else "Read/Write"] for s in shares])
        run = self.svc.running
        self.status.setText(f"<b>Sharing service:</b> <span style='color:{'#4caf50' if run else '#e55'}'>"
                            f"{'RUNNING' if run else 'STOPPED'}</span> (port {self.cfg.data['port']})")
        self.status.setTextFormat(Qt.RichText)
        self.toggle.setText("Stop" if run else "Start")
        port = self.cfg.data["port"]
        self.urls.setText("Addresses: " + "   ".join(f"http://{a}:{port}" for _n, a in local_ips()) if run else "")
        self.on_state()
 
    def toggle_service(self):
        try:
            self.svc.stop() if self.svc.running else self.svc.start()
        except Exception as e:  # noqa
            QMessageBox.warning(self, "PyNAS", str(e))
        self.refresh()
 
    def add(self):
        d = ShareDialog(self.cfg.data["data_root"], self)
        if d.exec() != QDialog.Accepted:
            return
        name, path = d.name.text().strip(), d.path.text().strip()
        if not name or "/" in name or not os.path.isdir(path):
            return QMessageBox.warning(self, "PyNAS", "Enter a name (without /) and an existing folder.")
        if any(s["name"] == name for s in self.cfg.data["shares"]):
            return QMessageBox.warning(self, "PyNAS", "A share with that name already exists.")
        self.cfg.data["shares"].append({"name": name, "path": path, "readonly": d.ro.isChecked()})
        self.cfg.save()
        self.refresh()
 
    def _idx(self):
        r = self.table.currentRow()
        return r if r >= 0 else None
 
    def toggle_ro(self):
        if (i := self._idx()) is not None:
            s = self.cfg.data["shares"][i]
            s["readonly"] = not s["readonly"]
            self.cfg.save()
            self.refresh()
 
    def remove(self):
        if (i := self._idx()) is not None:
            del self.cfg.data["shares"][i]
            self.cfg.save()
            self.refresh()
 
 
class CredentialsPage(QWidget):
    def __init__(self, cfg):
        super().__init__()
        self.cfg = cfg
        lay = QVBoxLayout(self)
        lay.setContentsMargins(16, 16, 16, 16)
        add = QPushButton("＋ Add User")
        add.clicked.connect(self.add)
        page_title("Credentials  ›  Local Users", lay, add)
        self.table = make_table(["Username"])
        lay.addWidget(self.table)
        row = QHBoxLayout()
        for text, fn in (("Change password", self.change), ("Delete", self.delete)):
            b = QPushButton(text)
            b.clicked.connect(fn)
            row.addWidget(b)
        row.addStretch()
        lay.addLayout(row)
 
    def refresh(self):
        fill_table(self.table, [[u] for u in sorted(self.cfg.data["users"])])
 
    def _pw(self):
        pw, ok = QInputDialog.getText(self, "Password", "Password (min 6 characters):", QLineEdit.Password)
        if ok and len(pw) >= 6:
            return pw
        if ok:
            QMessageBox.warning(self, "PyNAS", "The password is too short.")
 
    def add(self):
        name, ok = QInputDialog.getText(self, "Add User", "Username:")
        name = name.strip()
        if not ok or not name or ":" in name:
            return
        if (pw := self._pw()):
            self.cfg.set_password(name, pw)
            self.refresh()
 
    def change(self):
        r = self.table.currentRow()
        if r >= 0 and (pw := self._pw()):
            self.cfg.set_password(self.table.item(r, 0).text(), pw)
 
    def delete(self):
        r = self.table.currentRow()
        if r >= 0:
            self.cfg.delete_user(self.table.item(r, 0).text())
            self.refresh()
 
 
class ReportingPage(QWidget):
    def __init__(self, stats):
        super().__init__()
        self.stats = stats
        lay = QVBoxLayout(self)
        lay.setContentsMargins(16, 16, 16, 16)
        page_title("Reporting", lay)
        self.cpu = LineChart((BLUE,), fixed_max=100, unit="%")
        self.mem = LineChart((MAGENTA,), fixed_max=100, unit="%")
        self.net = LineChart((BLUE, ORANGE), unit=" Mb/s")
        for title, w in (("CPU", self.cpu), ("Memory", self.mem), ("Network (in/out)", self.net)):
            lay.addWidget(Card(title, w), 1)
 
    def refresh(self):
        s = self.stats
        self.cpu.set_series([s.cpu_hist])
        self.mem.set_series([s.mem_hist])
        self.net.set_series([s.rx, s.tx])
 
 
class SystemPage(QWidget):
    def __init__(self, cfg, svc):
        super().__init__()
        self.cfg, self.svc = cfg, svc
        lay = QVBoxLayout(self)
        lay.setContentsMargins(16, 16, 16, 16)
        page_title("System  ›  General", lay)
        row = QHBoxLayout()
        row.addWidget(QLabel("Server port:"))
        self.port = QSpinBox()
        self.port.setRange(1, 65535)
        self.port.setValue(int(cfg.data["port"]))
        row.addWidget(self.port)
        save = QPushButton("Save")
        save.clicked.connect(self.save)
        row.addWidget(save)
        row.addStretch()
        lay.addLayout(row)
        lay.addWidget(QLabel("Service log"))
        self.log = QPlainTextEdit()
        self.log.setReadOnly(True)
        lay.addWidget(self.log, 1)
 
    def save(self):
        self.cfg.data["port"] = self.port.value()
        self.cfg.save()
        QMessageBox.information(self, "PyNAS", "Saved. Restart the sharing service to apply the new port.")
 
    def refresh(self):
        text = "\n".join(self.svc.logs)
        if text != self.log.toPlainText():
            self.log.setPlainText(text)
            self.log.verticalScrollBar().setValue(self.log.verticalScrollBar().maximum())
 
 
class WinButton(QPushButton):
    """Window-style button (minimize or close) drawn by hand, no font dependency."""
 
    def __init__(self, kind, parent=None):
        super().__init__(parent)
        self.kind = kind
        self.setFixedSize(46, 32)
        self.setToolTip("Minimize (F11 = windowed)" if kind == "min" else "Exit PyNAS (Ctrl+Q)")
        self.setCursor(Qt.PointingHandCursor)
 
    def paintEvent(self, _):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        if self.underMouse():
            p.fillRect(self.rect(), QColor("#e81123" if self.kind == "close" else "#3a3a3a"))
        p.setPen(QPen(QColor("white"), 1.6))
        cx, cy, r = self.width() / 2, self.height() / 2, 6
        if self.kind == "close":
            p.drawLine(int(cx - r), int(cy - r), int(cx + r), int(cy + r))
            p.drawLine(int(cx - r), int(cy + r), int(cx + r), int(cy - r))
        else:
            p.drawLine(int(cx - r), int(cy + 3), int(cx + r), int(cy + 3))
 
 
# ------------------------------------------------------------------ main
STYLE = f"""
* {{ color:#e0e0e0; font-size:13px; }}
QMainWindow, QWidget#root {{ background:#1a1a1a; }}
QWidget {{ background:transparent; }}
QFrame#topbar {{ background:#0f0f0f; }}
QListWidget {{ background:#202020; border:0; outline:0; }}
QListWidget::item {{ padding:15px 18px; border-bottom:1px solid #2a2a2a; }}
QListWidget::item:selected {{ background:#2a2a2a; color:{BLUE}; border-left:3px solid {BLUE}; }}
QFrame#card {{ background:{CARD_BG}; border:1px solid #333; }}
QLabel#cardTitle {{ font-size:17px; font-weight:bold; background:transparent; }}
QLabel#pageTitle {{ font-size:26px; font-weight:bold; padding:6px 0; }}
QPushButton {{ background:{BLUE}; color:white; border:0; padding:8px 18px; font-weight:bold; }}
QPushButton:hover {{ background:#2aa9e0; }}
QLineEdit, QSpinBox, QPlainTextEdit {{ background:#111; border:1px solid #333; padding:6px; }}
QTableWidget {{ background:{CARD_BG}; border:1px solid #333; gridline-color:#333; }}
QTableWidget::item:selected {{ background:#0b3a52; }}
QHeaderView::section {{ background:#2d2d2d; border:0; padding:8px; font-weight:bold; }}
QDialog, QMessageBox, QInputDialog {{ background:#222; }}
QScrollBar:vertical {{ background:#1a1a1a; width:10px; }}
QScrollBar::handle:vertical {{ background:#444; }}
"""
 
 
class MainWindow(QMainWindow):
    NAV = ["▦   Dashboard", "☰   Storage", "⌸   Datasets", "⇄   Shares",
           "⚿   Credentials", "▥   Reporting", "⚙   System"]
 
    def __init__(self):
        super().__init__()
        self.setWindowTitle("PyNAS")
        self.resize(1400, 860)
        self.cfg, self.stats = Config(), Stats()
        self.svc = NasService(self.cfg)
 
        root = QWidget()
        root.setObjectName("root")
        self.setCentralWidget(root)
        v = QVBoxLayout(root)
        v.setContentsMargins(0, 0, 0, 0)
        v.setSpacing(0)
 
        top = QFrame()
        top.setObjectName("topbar")
        top.setFixedHeight(48)
        tl = QHBoxLayout(top)
        logo = QLabel("◆ PyNAS")
        logo.setStyleSheet(f"color:{BLUE};font-size:24px;font-weight:600")
        tl.addWidget(logo)
        tl.addSpacing(40)
        search = QLineEdit()
        search.setPlaceholderText("Search UI")
        search.setFixedWidth(450)
        tl.addWidget(search)
        tl.addStretch()
        self.state = QLabel()
        self.state.setTextFormat(Qt.RichText)
        tl.addWidget(self.state)
        tl.addSpacing(16)
        tl.addWidget(QLabel(f"👤 {socket.gethostname()}"))
        tl.addSpacing(8)
        self.min_btn = WinButton("min")
        self.min_btn.clicked.connect(self.showMinimized)
        tl.addWidget(self.min_btn)
        self.quit_btn = WinButton("close")
        self.quit_btn.clicked.connect(self.close)
        tl.addWidget(self.quit_btn)
        self.min_btn.hide()   # custom buttons only appear in true fullscreen (F11);
        self.quit_btn.hide()  # otherwise the normal window title bar is used
        v.addWidget(top)
 
        body = QHBoxLayout()
        body.setSpacing(0)
        self.nav = QListWidget()
        self.nav.setFixedWidth(233)
        for n in self.NAV:
            self.nav.addItem(n)
        side = QVBoxLayout()
        side.setSpacing(0)
        side.addWidget(self.nav, 1)
        foot = QLabel("COMMUNITY\n© PyNAS")
        foot.setAlignment(Qt.AlignCenter)
        foot.setStyleSheet("background:#202020;color:#888;padding:14px;font-size:11px")
        side.addWidget(foot)
        body.addLayout(side)
 
        self.stack = QStackedWidget()
        self.dash = Dashboard(self.stats, self.cfg, self.nav.setCurrentRow)
        self.pages = [self.dash, StoragePage(), DatasetsPage(self.cfg),
                      SharesPage(self.cfg, self.svc, self.update_state),
                      CredentialsPage(self.cfg), ReportingPage(self.stats), SystemPage(self.cfg, self.svc)]
        for p in self.pages:
            self.stack.addWidget(p)
        body.addWidget(self.stack, 1)
        v.addLayout(body, 1)
 
        QShortcut(QKeySequence("F11"), self, self.toggle_fullscreen)
        QShortcut(QKeySequence("Ctrl+Q"), self, self.close)
        self.nav.currentRowChanged.connect(self.change_page)
        self.nav.setCurrentRow(0)
        self.timer = QTimer(self)
        self.timer.timeout.connect(self.tick)
        self.timer.start(1000)
        self.tick()
        self.update_state()
 
    def toggle_fullscreen(self):
        self.showMaximized() if self.isFullScreen() else self.showFullScreen()
 
    def changeEvent(self, e):
        if e.type() == QEvent.WindowStateChange and hasattr(self, "min_btn"):
            fs = self.isFullScreen()
            self.min_btn.setVisible(fs)
            self.quit_btn.setVisible(fs)
        super().changeEvent(e)
 
    def update_state(self):
        run = self.svc.running
        self.state.setText(f"<span style='color:{'#4caf50' if run else '#e55'}'>●</span> Sharing: "
                           f"{'Running' if run else 'Stopped'}")
 
    def change_page(self, i):
        self.stack.setCurrentIndex(i)
        if hasattr(self.pages[i], "refresh"):
            self.pages[i].refresh()
 
    def tick(self):
        self.stats.tick()
        cur = self.pages[self.stack.currentIndex()]
        if isinstance(cur, (Dashboard, ReportingPage, SystemPage)):
            cur.refresh()
 
    def closeEvent(self, e):
        self.svc.stop()
        super().closeEvent(e)
 
 
def main():
    if sys.platform == "win32":
        # Own taskbar identity, so Windows shows the PyNAS icon instead of the Python one
        try:
            import ctypes
            ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID("PyNAS.App.1")
        except Exception:
            pass
    app = QApplication(sys.argv)
    app.setStyleSheet(STYLE)
    here = os.path.dirname(os.path.abspath(__file__))
    for fname in ("pynas.ico", "pynas_icon_256.png"):
        path = os.path.join(here, fname)
        if os.path.exists(path):
            app.setWindowIcon(QIcon(path))
            break
    w = MainWindow()
    if not app.windowIcon().isNull():
        w.setWindowIcon(app.windowIcon())
    w.showMaximized()
    sys.exit(app.exec())
 
 
if __name__ == "__main__":
    main()
 