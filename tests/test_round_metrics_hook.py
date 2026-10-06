"""Teste do hook Stop .claude/hooks/fpch_round_metrics.py (fixtures sinteticas).

Cobre: dedup por message.id (streaming), subagente dentro da janela da rodada,
subagente fora da janela ignorado, custo pela tabela de precos e modelo
desconhecido => cost_usd null.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

HOOK = Path(__file__).resolve().parent.parent / ".claude" / "hooks" / "fpch_round_metrics.py"


def _msg(ts, mid, model, out, inp=10, cc=0, cr=0):
    return {"type": "assistant", "timestamp": ts, "uuid": mid + ts,
            "message": {"id": mid, "model": model, "content": [],
                        "usage": {"input_tokens": inp, "output_tokens": out,
                                  "cache_creation_input_tokens": cc,
                                  "cache_read_input_tokens": cr}}}


def _grava(path, linhas):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(json.dumps(x) for x in linhas) + "\n", encoding="utf-8")


def test_hook_soma_subagente_e_deduplica_streaming(tmp_path):
    sid = "sess-1"
    tp = tmp_path / f"{sid}.jsonl"
    _grava(tp, [
        {"type": "user", "timestamp": "2026-10-05T10:00:00Z",
         "message": {"content": "oi"}},
        # mesma mensagem em streaming: output parcial (5) depois final (100)
        _msg("2026-10-05T10:00:01Z", "m1", "claude-opus-5-5", 5, cc=1000),
        _msg("2026-10-05T10:00:02Z", "m1", "claude-opus-5-5", 100, cc=1000),
    ])
    sub = tmp_path / sid / "subagents"
    _grava(sub / "agent-aaa.jsonl", [
        _msg("2026-10-05T09:00:00Z", "antigo", "claude-opus-5-5", 999),  # fora da janela
        _msg("2026-10-05T10:00:03Z", "s1", "claude-sonnet-5-5", 7, inp=1),
        _msg("2026-10-05T10:00:04Z", "s1", "claude-sonnet-5-5", 50, inp=1),
        _msg("2026-10-05T10:00:05Z", "s2", "modelo-inexistente", 10, inp=0),
    ])
    (sub / "agent-aaa.meta.json").write_text(
        json.dumps({"description": "teste", "agentType": "x"}), encoding="utf-8")
    out = tmp_path / "saida.jsonl"
    env = dict(os.environ, FPCH_METRICS_OUT=str(out))
    entrada = json.dumps({"transcript_path": str(tp), "session_id": sid, "cwd": str(tmp_path)})
    r = subprocess.run([sys.executable, str(HOOK)], input=entrada, text=True, env=env)
    assert r.returncode == 0
    row = json.loads(out.read_text(encoding="utf-8").splitlines()[-1])
    # principal: dedup fica com output 100 (nao 105)
    assert row["api_calls"] == 1
    assert row["output_tokens"] == 100
    assert row["total_tokens"] == 10 + 100 + 1000
    # custo opus-5-5: (10*4 + 100*20 + 1000*5)/1e6
    assert abs(row["cost_usd"] - (10 * 4 + 100 * 20 + 1000 * 5) / 1e6) < 1e-9
    # subagente: s1 (dedup, 1+50) + s2 (0+10); mensagem antiga ignorada
    assert len(row["subagents"]) == 1
    s = row["subagents"][0]
    assert s["agent_id"] == "aaa" and s["description"] == "teste"
    assert s["output_tokens"] == 60 and s["input_tokens"] == 1
    assert s["cost_usd"] is None  # s2 usa modelo fora da tabela
    assert row["subagents_total_tokens"] == 61
    assert row["grand_total_tokens"] == 1110 + 61
    assert row["grand_cost_parcial"] is True
