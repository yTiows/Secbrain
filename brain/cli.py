"""Command line: `brain serve | ingest | search | maintain | export | keys`."""
from __future__ import annotations
import argparse
import json
import os
import sys
import threading
import time
from pathlib import Path

from .app import Brain
from .config import Settings


def _up(brain, settings, port: int, lan: bool):
    import shutil
    import uvicorn
    from .api.app import create_app
    from . import phone
    brain.keys.bootstrap_admin(settings.data_dir)
    tok = phone.phone_key(brain)
    have_tunnel = not lan and bool(shutil.which("tailscale") or shutil.which("cloudflared"))
    host = "127.0.0.1" if have_tunnel else "0.0.0.0"          # only listen on the network when there is no tunnel to front it
    server = uvicorn.Server(uvicorn.Config(create_app(brain), host=host, port=port, log_level="warning"))
    t = threading.Thread(target=server.run, daemon=True)
    t.start()
    while not server.started and t.is_alive():
        time.sleep(0.1)
    url = how = proc = None
    if have_tunnel:
        url = phone.tailscale_url(port)
        how = "Tailscale (private to your tailnet, HTTPS)" if url else None
        if not url:
            url, proc = phone.cloudflare_quick_tunnel(port)
            how = "Cloudflare quick tunnel (public URL, protected by your key)" if url else None
        if not url:
            print("Tunnel tools found but none could start (is Tailscale logged in?). Restart with --lan to use the local network.")
            server.should_exit = True
            return
    if not url:
        ip = phone.lan_ip()
        url = f"http://{ip or '127.0.0.1'}:{port}"
        how = "local network, plain HTTP: phone must be on the same Wi-Fi. For private HTTPS install Tailscale on this machine and the phone"
    link = phone.login_link(url, tok)
    lf = settings.data_dir / "phone-link.txt"
    lf.write_text(link + "\n"); lf.chmod(0o600)
    png = settings.data_dir / "phone-link.png"
    print(f"\n  Secbrain is running via {how}\n  Scan with your phone camera (logs in automatically):\n")
    qr = phone.qr_ascii(link)
    print(qr if qr else "  (pip install qrcode to show a QR here)")
    print(f"\n  {link}\n  Saved to {lf}" + (f" and {png}" if phone.save_qr_png(link, png) else ""))
    print("  The link contains your key: treat it like a password. Revoke it any time in Vault > API keys (key named 'phone').")
    print(f"  Drop files into {settings.data_dir / 'inbox'} to have them ingested automatically.  Ctrl+C to stop.\n", flush=True)
    try:
        while t.is_alive():
            time.sleep(1)
    except KeyboardInterrupt:
        pass
    finally:
        server.should_exit = True
        if proc:
            proc.terminate()


def main(argv=None):
    ap = argparse.ArgumentParser(prog="brain", description="Secbrain: personal knowledge intelligence")
    ap.add_argument("--data", default=None, help="data directory (default ./data or $BRAIN_DATA_DIR)")
    sub = ap.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("serve"); s.add_argument("--host", default=os.environ.get("BRAIN_HOST", "127.0.0.1")); s.add_argument("--port", type=int, default=8765)
    i = sub.add_parser("ingest"); i.add_argument("paths", nargs="+"); i.add_argument("--project"); i.add_argument("--folders-as-projects", action="store_true")
    q = sub.add_parser("search"); q.add_argument("query"); q.add_argument("-k", type=int, default=8); q.add_argument("--context", action="store_true")
    sub.add_parser("maintain")
    e = sub.add_parser("export"); e.add_argument("dest")
    k = sub.add_parser("keys"); k.add_argument("action", choices=["create", "list"]); k.add_argument("--name", default="agent"); k.add_argument("--scopes", default="read")
    k.add_argument("--projects", default="")
    sub.add_parser("reindex")
    u = sub.add_parser("up", help="one command: start the server, open a private HTTPS tunnel if possible, print a QR code that logs your phone in")
    u.add_argument("--port", type=int, default=8765); u.add_argument("--lan", action="store_true", help="skip tunnels; serve on the local network (plain HTTP)")
    a = ap.parse_args(argv)
    settings = Settings(data_dir=a.data) if a.data else Settings()
    brain = Brain(settings)
    try:
        if a.cmd == "serve":
            import uvicorn
            from .api.app import create_app
            tok = brain.keys.bootstrap_admin(settings.data_dir)
            pub = os.environ.get("BRAIN_PUBLIC_URL")
            if pub:
                from .phone import phone_key, login_link
                print(f"\n  Phone login link (open once on your phone, then Add to Home Screen):\n  {login_link(pub, phone_key(brain))}\n", flush=True)
            if tok:
                print(f"\n  Admin API key created and saved to {settings.data_dir / 'admin.key'} (mode 600).\n  Paste it into the UI login.\n")
            else:
                print(f"\n  Admin key: {settings.data_dir / 'admin.key'}\n")
            uvicorn.run(create_app(brain), host=a.host, port=a.port, log_level="info")
        elif a.cmd == "up":
            _up(brain, settings, a.port, a.lan)
        elif a.cmd == "ingest":
            for p in map(Path, a.paths):
                if p.is_dir():
                    print(json.dumps(brain.vault.import_directory(p, folders_as_projects=a.folders_as_projects)))
                else:
                    j = brain.ingest.ingest_bytes(p.name, p.read_bytes(), project=a.project)
                    print(p.name, j["status"], j["result"].get("summary", j.get("error")))
        elif a.cmd == "search":
            if a.context:
                r = brain.retrieval.context(a.query, k=a.k); r.pop("_full_tokens", None)
                print(json.dumps(r, indent=1, ensure_ascii=False))
            else:
                for x in brain.retrieval.search(a.query, k=a.k)["results"]:
                    print(f"{x['score']:.3f}  [{x['type']}] {x['title']}  ({', '.join(s['name'] for s in x['sources'][:1])})\n        {x['snippet'][:150]}")
        elif a.cmd == "maintain":
            print(json.dumps(brain.maintenance.run(), indent=1))
        elif a.cmd == "export":
            print(brain.vault.export_to_dir(Path(a.dest)))
        elif a.cmd == "keys":
            if a.action == "create":
                print(json.dumps(brain.keys.create(a.name, a.scopes.split(","), [x for x in a.projects.split(",") if x]), indent=1))
            else:
                print(json.dumps(brain.keys.list(), indent=1))
        elif a.cmd == "reindex":
            print(brain.store.reindex())
    finally:
        brain.close()


if __name__ == "__main__":
    main()
