from __future__ import annotations

import hashlib
import json
import threading
from bisect import bisect_right
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from agentlab.domain import Stage, Visibility
from agentlab.security import redact


def _canonical(value: Any) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")


class AuditStore:
    """Append-only JSONL audit log with a hash chain and content-addressed artifacts."""

    def __init__(self, run_dir: Path, run_id: str):
        self.run_dir = run_dir
        self.run_id = run_id
        self.artifact_dir = run_dir / "artifacts"
        self.events_path = run_dir / "events.jsonl"
        self.run_dir.mkdir(parents=True, exist_ok=True)
        self.artifact_dir.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._seq = 0
        self._prev_hash = "0" * 64
        self._read_signature = None
        self._read_rows = []
        self._read_sequences = []
        self._verify_result = (True, None)

    def append(
        self,
        stage: Stage | str,
        visibility: Visibility | str,
        payload: dict[str, Any],
        *,
        step: int | None = None,
    ) -> dict[str, Any]:
        with self._lock:
            prior_signature = self._signature()
            self._seq += 1
            event = {
                "run_id": self.run_id,
                "seq": self._seq,
                "timestamp": datetime.now(UTC).isoformat(),
                "step": step,
                "stage": str(stage),
                "visibility": str(visibility),
                "payload": redact(payload),
                "prev_hash": self._prev_hash,
            }
            event_hash = hashlib.sha256(_canonical(event)).hexdigest()
            event["event_hash"] = event_hash
            line = (json.dumps(event, ensure_ascii=False) + "\n").encode('utf-8')
            offset = self.events_path.stat().st_size if self.events_path.exists() else 0
            with self.events_path.open("ab") as handle:
                handle.write(line)
            self._prev_hash = event_hash
            if (self._read_signature == prior_signature and self._verify_result[0]
                    and len(self._read_sequences) == self._seq - 1):
                self._read_rows.append((self._seq, offset, len(line)))
                self._read_sequences.append(self._seq)
                self._read_signature = self._signature()
            else:
                self._read_signature = None
            return event

    def resume_verified(self, expected: dict) -> None:
        """Stream the chain; never truncate an action written after a checkpoint."""
        if self.events_path.stat().st_size != expected['size']:
            raise ValueError('审计在检查点之后发生变化；需要核对最后动作')
        previous, seq = '0' * 64, 0
        with self.events_path.open(encoding='utf-8') as stream:
            for line in stream:
                event = json.loads(line)
                digest = event.pop('event_hash')
                if (event['run_id'] != self.run_id or event['seq'] != seq + 1
                        or event['prev_hash'] != previous
                        or hashlib.sha256(_canonical(event)).hexdigest() != digest):
                    raise ValueError('审计链校验失败')
                previous, seq = digest, event['seq']
        if (seq, previous) != (expected['seq'], expected['hash']):
            raise ValueError('审计尾部不匹配')
        self._seq, self._prev_hash = seq, previous

    def save_artifact(self, content: bytes, media_type: str, suffix: str = ".bin") -> dict[str, Any]:
        digest = hashlib.sha256(content).hexdigest()
        path = self.artifact_dir / f"{digest}{suffix}"
        if not path.exists():
            path.write_bytes(content)
        return {"sha256": digest, "media_type": media_type, "size": len(content), "file": path.name}

    def save_json_artifact(self, value: Any) -> dict[str, Any]:
        return self.save_artifact(_canonical(redact(value)), "application/json", ".json")

    def events(self, after_seq: int = 0) -> list[dict[str, Any]]:
        return self.events_page(after_seq, limit=None, max_bytes=None)['events']

    def _signature(self):
        if not self.events_path.exists():
            return ()
        s = self.events_path.stat()
        return (s.st_dev, s.st_ino, s.st_size, s.st_mtime_ns, s.st_ctime_ns)

    def _ensure_read_index(self):
        """Stream once per changed file; retain offsets, never image payloads."""
        signature = self._signature()
        if signature == self._read_signature:
            return
        rows, sequences, previous, problem = [], [], '0' * 64, None
        if signature:
            with self.events_path.open('rb') as stream:
                while True:
                    offset = stream.tell()
                    line = stream.readline()
                    if not line:
                        break
                    event = json.loads(line)
                    recorded = event.pop('event_hash')
                    seq = event['seq']
                    if problem is None:
                        if seq != len(rows) + 1 or event.get('run_id') != self.run_id:
                            problem = f'broken identity/sequence at seq {seq}'
                        elif event.get('prev_hash') != previous:
                            problem = f'broken prev_hash at seq {seq}'
                        elif hashlib.sha256(_canonical(event)).hexdigest() != recorded:
                            problem = f'broken event_hash at seq {seq}'
                    rows.append((seq, offset, len(line)))
                    sequences.append(seq)
                    previous = recorded
        if signature != self._signature():
            raise RuntimeError('audit changed during verification; retry the read')
        self._read_rows, self._read_sequences = rows, sequences
        self._verify_result = (problem is None, problem)
        self._read_signature = signature

    def events_page(self, after_seq=0, *, limit=128, max_bytes=8*1024*1024):
        with self._lock:
            self._ensure_read_index()
            start = bisect_right(self._read_sequences, after_seq)
            rows = self._read_rows[start:]
            result, used = [], 0
            if rows:
                with self.events_path.open('rb') as stream:
                    for seq, offset, size in rows:
                        if result and ((limit is not None and len(result) >= limit)
                                       or (max_bytes is not None and used + size > max_bytes)):
                            break
                        stream.seek(offset)
                        result.append(json.loads(stream.read(size)))
                        used += size
            return dict(events=result, has_more=start+len(result)<len(self._read_rows),
                        next_after=result[-1]['seq'] if result else after_seq,
                        hash_chain_valid=self._verify_result[0], hash_chain_problem=self._verify_result[1])

    def verify(self) -> tuple[bool, str | None]:
        with self._lock:
            self._ensure_read_index()
            return self._verify_result
