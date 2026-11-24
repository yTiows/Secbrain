"""Phone access helpers: one-tap login link, tunnel detection (Tailscale preferred, Cloudflare quick tunnel fallback), QR codes."""
from __future__ import annotations
import json
import re
import shutil
import socket
import subprocess
import time
from pathlib import Path

from .app import Brain


def lan_ip() -> str | None:
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("192.0.2.1", 9))                  # no packet is sent; selects the outbound interface
        ip = s.getsockname()[0]
        s.close()
        return ip if not ip.startswith("127.") else None
    except OSError:
        return None


def phone_key(brain: Brain) -> str:
    """A dedicated, revocable key for the phone (kept in data/phone.key, mode 600). The UI needs admin scope."""
    p = brain.settings.data_dir / "phone.key"
    if p.exists():
        tok = p.read_text().strip()
        if brain.keys.verify(tok):
            return tok
    import os
    k = brain.keys.create("phone", ["read", "write", "admin"])
    fd = os.open(p, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        f.write(k["token"] + "\n")
    return k["token"]


def login_link(base_url: str, token: str) -> str:
    return f"{base_url.rstrip('/')}/#key={token}"          # fragment: never sent to servers/proxies, stripped by the UI after login


def tailscale_url(port: int) -> str | None:
    ts = shutil.which("tailscale")
    if not ts:
        return None
    try:
        st = json.loads(subprocess.run([ts, "status", "--json"], capture_output=True, text=True, timeout=10).stdout or "{}")
        if st.get("BackendState") != "Running":
            return None
        name = (st.get("Self", {}).get("DNSName") or "").rstrip(".")
        if not name:
            return None
        for cmd in ([ts, "serve", "--bg", "--https=443", f"http://127.0.0.1:{port}"], [ts, "serve", "--bg", str(port)]):
            if subprocess.run(cmd, capture_output=True, text=True, timeout=30).returncode == 0:
                return f"https://{name}"
    except Exception:
        return None
    return None


def cloudflare_quick_tunnel(port: int, timeout: float = 40.0):
    cf = shutil.which("cloudflared")
    if not cf:
        return None, None
    proc = subprocess.Popen([cf, "tunnel", "--no-autoupdate", "--url", f"http://127.0.0.1:{port}"], stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True)
    t0 = time.time()
    while time.time() - t0 < timeout and proc.poll() is None:
        line = proc.stderr.readline()
        m = re.search(r"https://[a-z0-9-]+\.trycloudflare\.com", line or "")
        if m:
            return m.group(0), proc
    proc.terminate()
    return None, None


def qr_ascii(text: str) -> str | None:
    try:
        import qrcode
        q = qrcode.QRCode(border=1)
        q.add_data(text)
        q.make(fit=True)
        rows = q.get_matrix()
        out = []
        for y in range(0, len(rows), 2):                 # two pixel rows per text row using half blocks
            top, bot = rows[y], rows[y + 1] if y + 1 < len(rows) else [False] * len(rows[0])
            out.append("".join({(0, 0): " ", (1, 0): "▀", (0, 1): "▄", (1, 1): "█"}[(int(a), int(b))] for a, b in zip(top, bot)))
        return "\n".join(out)
    except Exception:
        return None


def save_qr_png(text: str, path: Path) -> bool:
    try:
        import qrcode
        img = qrcode.make(text)
        img.save(path)
        path.chmod(0o600)
        return True
    except Exception:
        return False
