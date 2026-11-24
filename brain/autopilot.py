"""Autopilot: zero-touch background chores. Runs inside `brain serve`.

* Inbox: any file dropped in <data>/inbox (e.g. synced from a phone by Syncthing/iCloud/Dropbox/Nextcloud, or AirDropped to a
  synced folder) is ingested automatically. Top-level sub-folders become projects. Handled files move to inbox/processed/<date>/,
  failures to inbox/failed/ with a .error.txt next to them. Nothing is ever deleted.
* Maintenance: full inspection + concept/consolidation discovery on an interval (SAFE actions auto-apply; the rest waits in the Intel view).
* Backups: daily SQLite backup, newest 7 kept.
"""
from __future__ import annotations
import shutil
import threading
import time
from pathlib import Path

from .app import Brain
from .ingest.extractors import SUPPORTED


class Autopilot:
    def __init__(self, brain: Brain, *, inbox_interval: float = 5.0, maintenance_interval: float = 3600.0, backup_interval: float = 86400.0, keep_backups: int = 7):
        self.b = brain
        self.inbox = brain.settings.data_dir / "inbox"
        self.iv = dict(inbox=inbox_interval, maint=maintenance_interval, backup=backup_interval)
        self.keep = keep_backups
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self.last = {"inbox": 0.0, "maint": time.time(), "backup": 0.0}
        self.log: list[str] = []
        for d in (self.inbox, self.inbox / "processed", self.inbox / "failed"):
            d.mkdir(parents=True, exist_ok=True)

    def start(self):
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, name="autopilot", daemon=True)
        self._thread.start()

    def stop(self):
        self._stop.set()

    def _loop(self):
        while not self._stop.wait(1.0):
            now = time.time()
            for name, fn in (("inbox", self.scan_inbox), ("maint", self.maintain), ("backup", self.backup)):
                if now - self.last[name] >= self.iv[name]:
                    self.last[name] = now
                    try:
                        fn()
                    except Exception as e:                       # a failing chore must never kill the loop
                        self._note(f"{name} error: {type(e).__name__}: {e}")

    def _note(self, msg: str):
        self.log = (self.log + [f"{time.strftime('%H:%M:%S')} {msg}"])[-50:]

    # ---------------------------------------------------------------- inbox
    def scan_inbox(self) -> int:
        n = 0
        for p in sorted(self.inbox.rglob("*")):
            rel = p.relative_to(self.inbox)
            if not p.is_file() or rel.parts[0] in ("processed", "failed") or p.name.startswith(".") or p.suffix == ".part":
                continue
            if p.suffix.lower() not in SUPPORTED or time.time() - p.stat().st_mtime < 2.0:
                continue                                         # still being written / unsupported type stays untouched
            project = rel.parts[0] if len(rel.parts) > 1 else None
            try:
                if p.suffix.lower() in (".md", ".markdown"):
                    r = self.b.vault.import_markdown(rel.as_posix(), p.read_bytes(), project=project)
                    err = r.get("error")
                else:
                    j = self.b.ingest.ingest_bytes(p.name, p.read_bytes(), uri=rel.as_posix(), project=project, origin="import", actor="inbox")
                    err = j.get("error") if j["status"] == "failed" else None
            except Exception as e:
                err = f"{type(e).__name__}: {e}"
            if err:
                dest = self.inbox / "failed" / rel
                dest.parent.mkdir(parents=True, exist_ok=True)
                shutil.move(str(p), self._free(dest))
                Path(str(dest) + ".error.txt").write_text(err)
                self._note(f"inbox FAILED {rel}: {err}")
            else:
                dest = self.inbox / "processed" / time.strftime("%Y%m%d") / rel
                dest.parent.mkdir(parents=True, exist_ok=True)
                shutil.move(str(p), self._free(dest))
                self._note(f"inbox ingested {rel}")
                n += 1
        return n

    @staticmethod
    def _free(dest: Path) -> str:
        if not dest.exists():
            return str(dest)
        i = 1
        while True:
            c = dest.with_name(f"{dest.stem}-{i}{dest.suffix}")
            if not c.exists():
                return str(c)
            i += 1

    # ---------------------------------------------------------- maintenance
    def maintain(self):
        r = self.b.maintenance.run()
        self.b.evolution.discover_concepts()
        self.b.evolution.find_consolidations()
        self._note(f"maintenance done in {r.get('elapsed_s')}s")

    def backup(self):
        path = self.b.vault.backup(self.b.settings)
        files = sorted(self.b.settings.backups_dir.glob("brain-*.sqlite"))
        for old in files[: -self.keep]:
            old.unlink()
        self._note(f"backup {Path(path).name}")
