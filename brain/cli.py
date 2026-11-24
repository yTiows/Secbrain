"""Command line: `brain serve | ingest | search | maintain | export | keys`."""
from __future__ import annotations
import argparse
import json
import sys
from pathlib import Path

from .app import Brain
from .config import Settings


def main(argv=None):
    ap = argparse.ArgumentParser(prog="brain", description="Secbrain: personal knowledge intelligence")
    ap.add_argument("--data", default=None, help="data directory (default ./data or $BRAIN_DATA_DIR)")
    sub = ap.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("serve"); s.add_argument("--host", default="127.0.0.1"); s.add_argument("--port", type=int, default=8765)
    i = sub.add_parser("ingest"); i.add_argument("paths", nargs="+"); i.add_argument("--project"); i.add_argument("--folders-as-projects", action="store_true")
    q = sub.add_parser("search"); q.add_argument("query"); q.add_argument("-k", type=int, default=8); q.add_argument("--context", action="store_true")
    sub.add_parser("maintain")
    e = sub.add_parser("export"); e.add_argument("dest")
    k = sub.add_parser("keys"); k.add_argument("action", choices=["create", "list"]); k.add_argument("--name", default="agent"); k.add_argument("--scopes", default="read")
    k.add_argument("--projects", default="")
    sub.add_parser("reindex")
    a = ap.parse_args(argv)
    settings = Settings(data_dir=a.data) if a.data else Settings()
    brain = Brain(settings)
    try:
        if a.cmd == "serve":
            import uvicorn
            from .api.app import create_app
            tok = brain.keys.bootstrap_admin(settings.data_dir)
            if tok:
                print(f"\n  Admin API key created and saved to {settings.data_dir / 'admin.key'} (mode 600).\n  Paste it into the UI login.\n")
            else:
                print(f"\n  Admin key: {settings.data_dir / 'admin.key'}\n")
            uvicorn.run(create_app(brain), host=a.host, port=a.port, log_level="info")
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
