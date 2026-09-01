"""`fpch audit --verify` — o furo da §4.2 do contrato da fatia funcional.

`audit.verify_chain()` já existe e já é testado em `test_audit.py`; o que faltava
era a CLI expor a flag. Estes testes prendem só a tradução feita em
`cli._cmd_audit_verify`, não a lógica de encadeamento em si:

1. cadeia íntegra -> código 0, tela diz quantas linhas foram conferidas.
2. cadeia adulterada -> código 1, tela mostra o índice físico correto do
   primeiro defeito.
3. trilha vazia ou inexistente -> código 1, e a tela NUNCA diz "íntegra": a
   ausência de dado não pode ser lida como integridade confirmada (§3 do
   contrato).

Nenhum teste toca `~/.fpch`: toda trilha vive em `tmp_path`, seja via `--trilha`
explícito, seja via `FPCH_AUDIT_LOG` isolado pela fixture abaixo.
"""

from __future__ import annotations

import json

import pytest

from fpch import audit, cli, policy


@pytest.fixture(autouse=True)
def _isolado(tmp_path, monkeypatch):
    """Nenhum teste escreve na trilha real nem lê a política real do autor."""
    audit.reset_state()
    monkeypatch.setenv("FPCH_AUDIT_LOG", str(tmp_path / "audit-default.jsonl"))
    monkeypatch.delenv("FPCH_POLICY", raising=False)
    monkeypatch.setattr(policy, "_cwd", lambda: tmp_path)
    monkeypatch.setattr(policy, "_home", lambda: tmp_path / "home-inexistente")
    yield
    audit.reset_state()


def _evento(seq: int, tid: str = "t1", **kw) -> audit.Event:
    kw.setdefault("event", "attempt")
    return audit.Event(trajectory_id=tid, seq=seq, **kw)


def _linhas(path) -> list[dict]:
    return [json.loads(ln) for ln in path.read_text(encoding="utf-8").splitlines() if ln.strip()]


# --------------------------------------------------------------------------
# 1. Cadeia íntegra
# --------------------------------------------------------------------------

def test_verify_cadeia_integra_devolve_zero_e_conta_linhas(tmp_path, capsys):
    trilha = tmp_path / "audit.jsonl"
    for i in range(4):
        audit.write(_evento(i, model=f"m{i}"), trilha)

    codigo = cli.main(["audit", "--verify", "--trilha", str(trilha)])

    assert codigo == 0
    saida = capsys.readouterr().out
    assert "íntegra" in saida
    assert "4" in saida


def test_verify_sem_trilha_explicita_usa_o_default_da_politica(tmp_path, capsys):
    """Sem `--trilha`, cai no mesmo default que `FPCH_AUDIT_LOG` resolve."""
    default = tmp_path / "audit-default.jsonl"
    audit.write(_evento(0, model="m0"), default)
    audit.write(_evento(1, model="m1"), default)

    codigo = cli.main(["audit", "--verify"])

    assert codigo == 0
    saida = capsys.readouterr().out
    assert "íntegra" in saida
    assert "2" in saida


# --------------------------------------------------------------------------
# 2. Cadeia adulterada
# --------------------------------------------------------------------------

def test_verify_cadeia_adulterada_devolve_um_e_mostra_o_indice(tmp_path, capsys):
    trilha = tmp_path / "audit.jsonl"
    for i in range(5):
        audit.write(_evento(i, model=f"m{i}"), trilha)

    linhas = _linhas(trilha)
    linhas[2]["hash"] = "0" * 64  # hash adulterado de propósito
    trilha.write_text(
        "\n".join(json.dumps(r, sort_keys=True, ensure_ascii=False) for r in linhas) + "\n",
        encoding="utf-8",
    )

    codigo = cli.main(["audit", "--verify", "--trilha", str(trilha)])

    assert codigo == 1
    err = capsys.readouterr().err
    assert "QUEBRADA" in err
    assert "2" in err
    assert "íntegra" not in err


# --------------------------------------------------------------------------
# 3. Ausência de dado nunca é "íntegra"
# --------------------------------------------------------------------------

def test_verify_trilha_inexistente_nao_e_lida_como_integra(tmp_path, capsys):
    alvo = tmp_path / "nao-existe.jsonl"
    assert not alvo.exists()

    codigo = cli.main(["audit", "--verify", "--trilha", str(alvo)])

    assert codigo != 0
    saida = capsys.readouterr()
    assert "íntegra" not in saida.out
    assert "íntegra" not in saida.err
    assert "vazia ou inexistente" in saida.err


def test_verify_trilha_vazia_nao_e_lida_como_integra(tmp_path, capsys):
    alvo = tmp_path / "audit.jsonl"
    alvo.write_text("", encoding="utf-8")

    codigo = cli.main(["audit", "--verify", "--trilha", str(alvo)])

    assert codigo != 0
    saida = capsys.readouterr()
    assert "íntegra" not in saida.out
    assert "íntegra" not in saida.err
    assert "vazia ou inexistente" in saida.err


def test_verify_trilha_so_com_linhas_em_branco_nao_e_lida_como_integra(tmp_path, capsys):
    alvo = tmp_path / "audit.jsonl"
    alvo.write_text("\n\n   \n", encoding="utf-8")

    codigo = cli.main(["audit", "--verify", "--trilha", str(alvo)])

    assert codigo != 0
    saida = capsys.readouterr()
    assert "íntegra" not in saida.out
    assert "vazia ou inexistente" in saida.err


# --------------------------------------------------------------------------
# 4. `audit` sem `--verify` continua com o comportamento antigo
# --------------------------------------------------------------------------

def test_audit_sem_verify_continua_o_resumo_por_pool(tmp_path, capsys):
    trilha = tmp_path / "audit.jsonl"
    audit.write(_evento(0, model="m0", pool="local", ok=True, latency_s=1.0, output_chars=10), trilha)

    codigo = cli.main(["audit", "--trilha", str(trilha)])

    assert codigo == 0
    saida = capsys.readouterr().out
    assert "chamadas totais" in saida
    assert "--verify" not in saida
