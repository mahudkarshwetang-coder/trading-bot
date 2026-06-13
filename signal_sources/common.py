from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone


def utc_now_iso():
    return datetime.now(timezone.utc).isoformat()


def clean_ticker(value):
    ticker = str(value or "").strip().upper()
    return "".join(char for char in ticker if char.isalnum() or char in {".", "-"})


@dataclass
class SignalSourceCandidate:
    ticker: str
    source: str
    score: float = 0.0
    action: str = "WATCH"
    reason: str = ""
    category: str = ""
    theme: str = ""
    metadata: dict = field(default_factory=dict)
    observed_at: str = field(default_factory=utc_now_iso)

    def to_dict(self):
        payload = asdict(self)
        payload["ticker"] = clean_ticker(payload.get("ticker"))
        return payload


@dataclass
class SourceRunResult:
    source: str
    candidates: list = field(default_factory=list)
    warnings: list = field(default_factory=list)
    metadata: dict = field(default_factory=dict)
    started_at: str = field(default_factory=utc_now_iso)
    finished_at: str = ""

    def finish(self):
        self.finished_at = utc_now_iso()
        return self

    def to_dict(self):
        payload = asdict(self)
        payload["candidates"] = [
            candidate.to_dict() if hasattr(candidate, "to_dict") else candidate
            for candidate in self.candidates
        ]
        return payload
