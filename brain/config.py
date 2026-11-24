from __future__ import annotations
import os
from dataclasses import dataclass, field
from pathlib import Path


@dataclass
class Settings:
    data_dir: Path = field(default_factory=lambda: Path(os.environ.get("BRAIN_DATA_DIR", "data")))
    max_upload_bytes: int = 25 * 1024 * 1024
    max_text_chars: int = 2_000_000
    embedding_dim: int = 1024
    # relationship discovery
    rel_min_confidence: float = 0.45       # below this a candidate edge is rejected (not persisted)
    rel_max_per_object: int = 8
    # dedupe
    dup_jaccard: float = 0.82
    dup_cosine: float = 0.94
    # maintenance autonomy: levels "safe" auto-applied; "conditional" only if auto_conditional and confidence>=conditional_min
    auto_conditional: bool = False
    conditional_min_confidence: float = 0.9
    stale_days: int = 180
    # LLM (optional)
    anthropic_model: str = os.environ.get("BRAIN_ANTHROPIC_MODEL", "claude-sonnet-5-5")

    def __post_init__(self):
        self.data_dir = Path(self.data_dir)

    @property
    def db_path(self) -> Path:
        return self.data_dir / "brain.sqlite"

    @property
    def sources_dir(self) -> Path:
        return self.data_dir / "sources"

    @property
    def backups_dir(self) -> Path:
        return self.data_dir / "backups"

    def ensure(self) -> None:
        for d in (self.data_dir, self.sources_dir, self.backups_dir):
            d.mkdir(parents=True, exist_ok=True)
