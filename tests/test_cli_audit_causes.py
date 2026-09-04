"""`fpch audit --causes` e `fpch audit --process` — CLI para o dado novo da trilha.

`audit.summary()["by_cause"]`, `audit.summary()["by_source"]` e
`audit.failure_process()` já existem e já são testados em `test_audit.py`; o
que faltava era a CLI expor esse dado sem exigir que alguém abra o JSONL na
mão. Estes testes prendem só a tradução feita em `cli._cmd_audit_causes` e
`cli._cmd_audit_process`, não a lógica de agregação em si.

Metade dos testes prova que o comando acusa o que deve (categoria certa,
contagem certa, traço no lugar certo); a outra metade prova que ele NÃO acusa
o caso legítimo (vocabulário conhecido não vira "?", marco zero não vira
traço, trajetória sem remarcação não ganha asterisco).

Nenhum teste toca `~/.fpch`: toda trilha vive em `tmp_path`, via `--trilha`
explícito.
"""

from __future__ import annotations

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


def _verify_evt(seq: int, tid: str, **kw) -> audit.Event:
    kw.setdefault("event", "verify")
    kw.setdefault("component", "hook-teste")
    kw.setdefault("verdict", "fail")
    return audit.Event(trajectory_id=tid, seq=seq, **kw)


def _annotate(seq: int, tid: str, mark: str, target_seq: int) -> audit.Event:
    return audit.Event(event="annotate", trajectory_id=tid, seq=seq, mark=mark,
                        target_seq=target_seq)


# --------------------------------------------------------------------------
# 1. `--causes` — tabelas de causa raiz e de fonte
# --------------------------------------------------------------------------

def test_causes_mostra_categorias_subtipos_e_balde_de_desconhecido(tmp_path, capsys):
    trilha = tmp_path / "audit.jsonl"
    audit.write(_verify_evt(0, "t1", cause_category="epistemica",
                             cause_subtype="premissa_falsa", source="hook",
                             source_ref="pytest"), trilha)
    audit.write(_verify_evt(1, "t2", cause_category="epistemica",
                             cause_subtype="premissa_falsa", source="hook",
                             source_ref="pytest"), trilha)
    audit.write(_verify_evt(2, "t3", cause_category="competencia",
                             cause_subtype="lacuna_de_conhecimento", source="nlah",
                             source_ref="roteador"), trilha)
    # linha de vocabulário fora do vigente: escrita direta em disco, porque
    # `Event.__post_init__` rejeitaria isso na construção (por desenho) e a
    # leitura, ao contrário da escrita, precisa tolerar o que já está no disco.
    import json
    linha_estranha = {
        "event": "verify", "trajectory_id": "t4", "seq": 0, "schema_version": 3,
        "cause_category": "categoria_extinta", "cause_subtype": "lacuna_de_conhecimento",
        "source": "fonte_extinta", "source_ref": "roteador", "hash": "0" * 64,
    }
    with trilha.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(linha_estranha, sort_keys=True, ensure_ascii=False) + "\n")

    codigo = cli.main(["audit", "--causes", "--trilha", str(trilha)])

    assert codigo == 0
    saida = capsys.readouterr().out
    assert "causa raiz" in saida
    assert "epistemica" in saida and "premissa_falsa" in saida
    assert "competencia" in saida and "lacuna_de_conhecimento" in saida
    # duas ocorrências de premissa_falsa (t1, t2) -> total 2 na linha da categoria
    linhas = saida.splitlines()
    linha_epistemica = next(l for l in linhas if l.strip().startswith("epistemica"))
    assert linha_epistemica.split()[-1] == "2"
    # o balde "?" precisa aparecer -- esconder o desconhecido é o que a trilha combate
    assert any(l.strip() == "?" or l.strip().split()[0] == "?" for l in linhas)
    assert "fonte" in saida
    assert "roteador" in saida


def test_causes_vocabulario_conhecido_nao_produz_balde_de_interrogacao(tmp_path, capsys):
    """Vizinho do teste acima: trilha inteiramente dentro do vocabulário não
    deve inventar um balde "?" onde não há nada de desconhecido."""
    trilha = tmp_path / "audit.jsonl"
    audit.write(_verify_evt(0, "t1", cause_category="ambiente",
                             cause_subtype="bloqueio_de_ambiente", source="mcp",
                             source_ref="playwright"), trilha)

    codigo = cli.main(["audit", "--causes", "--trilha", str(trilha)])

    assert codigo == 0
    saida = capsys.readouterr().out
    assert "?" not in saida


def test_causes_trilha_sem_causa_registrada_mensagem_honesta_e_exit_zero(tmp_path, capsys):
    trilha = tmp_path / "audit.jsonl"
    # eventos existem, mas nenhum carrega cause_category/source
    audit.write(audit.Event(event="attempt", trajectory_id="t1", seq=0, ok=True), trilha)
    audit.write(audit.Event(event="end", trajectory_id="t1", seq=1, outcome="ok"), trilha)

    codigo = cli.main(["audit", "--causes", "--trilha", str(trilha)])

    assert codigo == 0
    saida = capsys.readouterr().out
    assert "nenhuma causa registrada" in saida


def test_causes_trilha_inexistente_mensagem_honesta_e_exit_zero(tmp_path, capsys):
    alvo = tmp_path / "nao-existe.jsonl"
    assert not alvo.exists()

    codigo = cli.main(["audit", "--causes", "--trilha", str(alvo)])

    assert codigo == 0
    saida = capsys.readouterr().out
    assert "nenhuma causa registrada" in saida


# --------------------------------------------------------------------------
# 2. `--process` — marcos do processo de falha
# --------------------------------------------------------------------------

def test_process_marco_ausente_aparece_como_traco_nao_como_zero(tmp_path, capsys):
    trilha = tmp_path / "audit.jsonl"
    audit.write(_annotate(0, "t1", "t_err", 1), trilha)
    # t_lock nunca anotado -> fix_window e t_lock devem ser "—"
    audit.write(_annotate(1, "t1", "t_obs", 3), trilha)

    codigo = cli.main(["audit", "--process", "--trilha", str(trilha)])

    assert codigo == 0
    saida = capsys.readouterr().out
    linha_t1 = next(l for l in saida.splitlines() if l.startswith("t1"))
    campos = linha_t1.split()
    # trajetória, t_err=1, t_lock=—, t_obs=3, fix_window=—, obs_lag=—
    assert campos[1] == "1"
    assert campos[2] == "—"
    assert campos[3] == "3"
    assert campos[4] == "—"
    assert campos[5] == "—"
    assert "0" not in (campos[2], campos[4], campos[5])


def test_process_marco_no_seq_zero_aparece_como_zero_nao_como_traco(tmp_path, capsys):
    """Vizinho do teste acima: seq=0 é uma medida legítima e não pode virar
    traço só porque o valor numérico é falsy em Python."""
    trilha = tmp_path / "audit.jsonl"
    audit.write(_annotate(0, "t1", "t_err", 0), trilha)
    audit.write(_annotate(1, "t1", "t_lock", 0), trilha)
    audit.write(_annotate(2, "t1", "t_obs", 0), trilha)

    codigo = cli.main(["audit", "--process", "--trilha", str(trilha)])

    assert codigo == 0
    saida = capsys.readouterr().out
    linha_t1 = next(l for l in saida.splitlines() if l.startswith("t1"))
    campos = linha_t1.split()
    assert campos[1:6] == ["0", "0", "0", "0", "0"]
    assert "—" not in linha_t1


def test_process_observability_lag_negativo_mantem_sinal(tmp_path, capsys):
    """observability_lag = t_obs - t_lock pode ser negativo quando o sinal do
    erro era observável antes do ponto de irrecuperabilidade -- achado
    legítimo, não deve ser mostrado com abs()."""
    trilha = tmp_path / "audit.jsonl"
    audit.write(_annotate(0, "t1", "t_err", 1), trilha)
    audit.write(_annotate(1, "t1", "t_lock", 5), trilha)
    audit.write(_annotate(2, "t1", "t_obs", 2), trilha)  # antes do lock

    esperado = audit.failure_process(trilha)["t1"]
    assert esperado["observability_lag"] == -3
    assert esperado["fix_window"] == 4

    codigo = cli.main(["audit", "--process", "--trilha", str(trilha)])

    assert codigo == 0
    saida = capsys.readouterr().out
    linha_t1 = next(l for l in saida.splitlines() if l.startswith("t1"))
    campos = linha_t1.split()
    assert campos[4] == "4"    # fix_window
    assert campos[5] == "-3"   # observability_lag, com sinal
    assert "3" == campos[5].lstrip("-")
    assert "negativo" in saida.lower()


def test_process_valores_batem_exatamente_com_failure_process(tmp_path, capsys):
    trilha = tmp_path / "audit.jsonl"
    audit.write(_annotate(0, "t1", "t_err", 1), trilha)
    audit.write(_annotate(1, "t1", "t_lock", 4), trilha)
    audit.write(_annotate(2, "t1", "t_obs", 6), trilha)
    audit.write(_annotate(3, "t2", "t_err", 0), trilha)
    audit.write(_annotate(4, "t2", "t_lock", 2), trilha)

    esperado = audit.failure_process(trilha)

    codigo = cli.main(["audit", "--process", "--trilha", str(trilha)])
    assert codigo == 0
    saida = capsys.readouterr().out

    for tid, m in esperado.items():
        linha = next(l for l in saida.splitlines() if l.startswith(tid))
        campos = linha.split()
        assert campos[1] == ("—" if m["t_err"] is None else str(m["t_err"]))
        assert campos[2] == ("—" if m["t_lock"] is None else str(m["t_lock"]))
        assert campos[3] == ("—" if m["t_obs"] is None else str(m["t_obs"]))
        assert campos[4] == ("—" if m["fix_window"] is None else str(m["fix_window"]))
        assert campos[5].rstrip("*") == (
            "—" if m["observability_lag"] is None else str(m["observability_lag"])
        )


def test_process_trajetoria_remarcada_ganha_asterisco_e_legenda(tmp_path, capsys):
    trilha = tmp_path / "audit.jsonl"
    audit.write(_annotate(0, "t1", "t_err", 1), trilha)
    audit.write(_annotate(1, "t1", "t_err", 2), trilha)  # remarca

    codigo = cli.main(["audit", "--process", "--trilha", str(trilha)])

    assert codigo == 0
    saida = capsys.readouterr().out
    linha_t1 = next(l for l in saida.splitlines() if l.startswith("t1"))
    assert "*" in linha_t1
    assert "remarcado" in saida


def test_process_sem_remarcacao_nao_mostra_asterisco_nem_legenda(tmp_path, capsys):
    """Vizinho do teste acima: trajetória marcada uma única vez não deve
    carregar o sinal nem a legenda de uma remarcação que não aconteceu."""
    trilha = tmp_path / "audit.jsonl"
    audit.write(_annotate(0, "t1", "t_err", 1), trilha)
    audit.write(_annotate(1, "t1", "t_lock", 2), trilha)

    codigo = cli.main(["audit", "--process", "--trilha", str(trilha)])

    assert codigo == 0
    saida = capsys.readouterr().out
    linha_t1 = next(l for l in saida.splitlines() if l.startswith("t1"))
    assert "*" not in linha_t1
    assert "remarcado" not in saida


def test_process_nota_de_granularidade_aparece_na_saida(tmp_path, capsys):
    trilha = tmp_path / "audit.jsonl"
    audit.write(_annotate(0, "t1", "t_err", 1), trilha)

    codigo = cli.main(["audit", "--process", "--trilha", str(trilha)])

    assert codigo == 0
    saida = capsys.readouterr().out
    assert "eventos da trilha" in saida.lower()
    assert "Zhao et al" in saida
    assert "erro de leitura" in saida


def test_process_trilha_sem_marco_anotado_mensagem_honesta_e_exit_zero(tmp_path, capsys):
    trilha = tmp_path / "audit.jsonl"
    audit.write(audit.Event(event="attempt", trajectory_id="t1", seq=0, ok=True), trilha)

    codigo = cli.main(["audit", "--process", "--trilha", str(trilha)])

    assert codigo == 0
    saida = capsys.readouterr().out
    assert "nenhuma trajetória" in saida


# --------------------------------------------------------------------------
# 3. Precedência entre `--verify`, `--causes` e `--process`
# --------------------------------------------------------------------------

def test_verify_e_causes_juntas_verify_prevalece_e_avisa(tmp_path, capsys):
    trilha = tmp_path / "audit.jsonl"
    audit.write(_verify_evt(0, "t1", cause_category="epistemica",
                             cause_subtype="premissa_falsa"), trilha)

    codigo = cli.main(["audit", "--verify", "--causes", "--trilha", str(trilha)])

    assert codigo == 0
    saida = capsys.readouterr()
    assert "íntegra" in saida.out
    assert "causa raiz" not in saida.out
    assert "--verify prevalece" in saida.err


def test_causes_e_process_juntas_causes_prevalece_e_avisa(tmp_path, capsys):
    trilha = tmp_path / "audit.jsonl"
    audit.write(_verify_evt(0, "t1", cause_category="epistemica",
                             cause_subtype="premissa_falsa"), trilha)

    codigo = cli.main(["audit", "--causes", "--process", "--trilha", str(trilha)])

    assert codigo == 0
    saida = capsys.readouterr()
    assert "causa raiz" in saida.out
    assert "--causes prevalece" in saida.err


# --------------------------------------------------------------------------
# 4. `fpch audit` sem flag continua exatamente como hoje
# --------------------------------------------------------------------------

def test_audit_sem_flag_saida_inalterada(tmp_path, capsys):
    trilha = tmp_path / "audit.jsonl"
    audit.write(audit.Event(event="attempt", trajectory_id="t1", seq=0, model="m0",
                             pool="local", ok=True, latency_s=1.0, output_chars=10), trilha)

    codigo = cli.main(["audit", "--trilha", str(trilha)])

    assert codigo == 0
    saida = capsys.readouterr().out
    assert "chamadas totais: 1" in saida
    assert "pool" in saida and "chamadas" in saida and "falhas" in saida
    assert "causa raiz" not in saida
    assert "trajetória" not in saida
    assert "íntegra" not in saida
