"""Trilha de auditoria do FPCH.

Cada invocação de backend vira uma linha JSONL. Serve a três propósitos:

1. Operacional — descobrir qual pool está sendo drenado e por quê.
2. Acadêmico — é o dado empírico que o TCC não tem (lacuna nº 5 do inventário).
   Sem isto, qualquer afirmação sobre eficácia do roteamento é opinião.
3. Segurança — reconstruir o que um agente fez, depois do fato.

Append-only. Nunca reescrever histórico: um log que o agente pode editar não é
trilha de auditoria.
"""

from __future__ import annotations

import json
import os
import time
import uuid
from dataclasses import asdict, dataclass, field
from pathlib import Path


def _default_log_path() -> Path:
    override = os.environ.get("FPCH_AUDIT_LOG")
    if override:
        return Path(override)
    return Path.home() / ".fpch" / "audit.jsonl"


@dataclass
class Record:
    """Uma invocação. Campos None = não aplicável ou não capturado."""

    task_class: str
    backend: str
    model: str
    pool: str
    prompt_chars: int
    exit_code: int | None = None
    latency_s: float | None = None
    output_chars: int | None = None
    ok: bool | None = None
    error: str | None = None
    escalated_from: str | None = None
    label: str | None = None
    run_id: str = field(default_factory=lambda: uuid.uuid4().hex[:12])
    ts: str = field(default_factory=lambda: time.strftime("%Y-%m-%dT%H:%M:%S%z"))


def write(rec: Record, path: Path | None = None) -> None:
    """Grava um registro. Falha de log nunca derruba a chamada que a originou."""
    target = path or _default_log_path()
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        with target.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(asdict(rec), ensure_ascii=False) + "\n")
    except OSError:
        pass


def read_all(path: Path | None = None) -> list[dict]:
    target = path or _default_log_path()
    if not target.exists():
        return []
    out: list[dict] = []
    with target.open(encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                out.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    return out


def summary(path: Path | None = None) -> dict:
    """Agrega por pool. Responde: onde a cota está indo e o que está falhando."""
    rows = read_all(path)
    by_pool: dict[str, dict] = {}
    for r in rows:
        p = r.get("pool", "?")
        agg = by_pool.setdefault(p, {"calls": 0, "fail": 0, "latency_total": 0.0, "chars_out": 0})
        agg["calls"] += 1
        if r.get("ok") is False:
            agg["fail"] += 1
        agg["latency_total"] += r.get("latency_s") or 0.0
        agg["chars_out"] += r.get("output_chars") or 0
    for agg in by_pool.values():
        agg["latency_avg"] = round(agg["latency_total"] / agg["calls"], 2) if agg["calls"] else 0.0
        del agg["latency_total"]
    return {"total_calls": len(rows), "by_pool": by_pool}
