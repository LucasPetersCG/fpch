"""Testes da trilha de auditoria.

Primeira cobertura do módulo. O que se testa aqui não é "o dataclass tem os
campos certos" — é a única coisa que a trilha promete e que, se não for verdade,
invalida o dado empírico do TCC: **adulteração é detectável e perda é visível.**

Nenhum teste toca o `~/.fpch` real: todos escrevem em `tmp_path`.
"""

from __future__ import annotations

import json

import pytest

from fpch import audit


@pytest.fixture(autouse=True)
def _limpa_estado():
    """Contadores e cache de cadeia são estado de módulo — zerar entre testes."""
    audit.reset_state()
    yield
    audit.reset_state()


@pytest.fixture
def trilha(tmp_path):
    return tmp_path / "audit.jsonl"


def _evento(seq: int, tid: str = "t1", **kw) -> audit.Event:
    kw.setdefault("event", "attempt")
    return audit.Event(trajectory_id=tid, seq=seq, **kw)


def _linhas(path) -> list[dict]:
    return [json.loads(ln) for ln in path.read_text(encoding="utf-8").splitlines() if ln.strip()]


# --------------------------------------------------------------------------
# Encadeamento por hash
# --------------------------------------------------------------------------

def test_cadeia_integra_em_n_eventos(trilha):
    for i in range(5):
        assert audit.write(_evento(i, model=f"m{i}"), trilha) is True

    ok, quebrado = audit.verify_chain(trilha)
    assert ok is True
    assert quebrado is None

    linhas = _linhas(trilha)
    assert len(linhas) == 5
    # Primeira linha não tem antecessor; as demais apontam para a anterior.
    assert "prev_hash" not in linhas[0]
    for anterior, atual in zip(linhas, linhas[1:]):
        assert atual["prev_hash"] == anterior["hash"]


def test_hash_confere_com_o_dicionario_gravado(trilha):
    audit.write(_evento(0, model="m", error="acentuação é preservada"), trilha)
    row = _linhas(trilha)[0]
    assert audit.compute_hash(row) == row["hash"]


def test_campos_none_sao_omitidos(trilha):
    audit.write(_evento(0, model="m", ok=False), trilha)
    row = _linhas(trilha)[0]
    assert "verdict" not in row and "outcome" not in row
    # `False` não é omitido: só `None` é ausência.
    assert row["ok"] is False


def test_adulteracao_no_meio_e_detectada_no_indice_certo(trilha):
    for i in range(5):
        audit.write(_evento(i, model=f"m{i}"), trilha)

    linhas = _linhas(trilha)
    linhas[2]["model"] = "modelo-trocado-depois-do-fato"
    trilha.write_text(
        "\n".join(json.dumps(r, sort_keys=True, ensure_ascii=False) for r in linhas) + "\n",
        encoding="utf-8",
    )

    ok, quebrado = audit.verify_chain(trilha)
    assert ok is False
    assert quebrado == 2


def test_remocao_de_linha_quebra_a_cadeia(trilha):
    for i in range(4):
        audit.write(_evento(i, model=f"m{i}"), trilha)
    linhas = _linhas(trilha)
    del linhas[1]
    trilha.write_text(
        "\n".join(json.dumps(r, sort_keys=True, ensure_ascii=False) for r in linhas) + "\n",
        encoding="utf-8",
    )
    ok, quebrado = audit.verify_chain(trilha)
    assert ok is False
    # A linha que sobrou na posição 1 aponta para um antecessor que sumiu.
    assert quebrado == 1


def test_arquivo_inexistente_e_cadeia_valida(tmp_path):
    ok, quebrado = audit.verify_chain(tmp_path / "nao-existe.jsonl")
    assert (ok, quebrado) == (True, None)


def test_cadeia_continua_entre_processos(trilha):
    audit.write(_evento(0, model="a"), trilha)
    # Simula processo novo: cache de ponta de cadeia zerado, arquivo já existente.
    audit.reset_state()
    audit.write(_evento(1, model="b"), trilha)

    linhas = _linhas(trilha)
    assert linhas[1]["prev_hash"] == linhas[0]["hash"]
    assert audit.verify_chain(trilha) == (True, None)


# --------------------------------------------------------------------------
# Escrita nunca silenciosa
# --------------------------------------------------------------------------

def test_falha_de_escrita_e_observavel_e_nao_levanta(tmp_path, capsys):
    # Um diretório onde deveria haver um arquivo: `open("a")` falha com OSError
    # em qualquer plataforma, sem precisar adulterar o pathlib.
    alvo = tmp_path / "audit.jsonl"
    alvo.mkdir()

    assert audit.write(_evento(0), alvo) is False
    assert audit.counters()["write_failures"] == 1
    assert "AVISO" in capsys.readouterr().err

    # Segunda falha conta, mas não repete o aviso: ruído por chamada faria o
    # operador filtrar o stderr, que é o oposto de tornar a perda visível.
    assert audit.write(_evento(1), alvo) is False
    assert audit.counters()["write_failures"] == 2
    assert capsys.readouterr().err == ""


# --------------------------------------------------------------------------
# Leitura
# --------------------------------------------------------------------------

def test_linha_corrompida_e_contada_nao_silenciada(trilha, capsys):
    audit.write(_evento(0, model="bom"), trilha)
    with trilha.open("a", encoding="utf-8") as fh:
        fh.write("{isto não é json}\n")
    audit.write(_evento(1, model="tambem-bom"), trilha)

    rows = audit.read_all(trilha)
    assert len(rows) == 2
    assert audit.counters()["corrupt_lines"] == 1
    assert "ilegível" in capsys.readouterr().err

    # E a corrupção não é apenas contada: quebra a verificação, no índice certo.
    ok, quebrado = audit.verify_chain(trilha)
    assert ok is False
    assert quebrado == 1


def test_read_all_ordena_por_trajetoria_e_seq(trilha):
    audit.write(_evento(0, tid="b"), trilha)
    audit.write(_evento(1, tid="a"), trilha)
    audit.write(_evento(0, tid="a"), trilha)

    chaves = [(r["trajectory_id"], r["seq"]) for r in audit.read_all(trilha)]
    assert chaves == [("a", 0), ("a", 1), ("b", 0)]


def test_trajectories_agrupa_e_ordena(trilha):
    audit.write(_evento(0, tid="x", event="start"), trilha)
    audit.write(_evento(0, tid="y", event="start"), trilha)
    audit.write(_evento(1, tid="x"), trilha)

    grupos = audit.trajectories(trilha)
    assert set(grupos) == {"x", "y"}
    assert [e["seq"] for e in grupos["x"]] == [0, 1]


# --------------------------------------------------------------------------
# Compatibilidade com o esquema 1
# --------------------------------------------------------------------------

def _linha_v1(**kw) -> str:
    base = {
        "task_class": "standard",
        "backend": "agy",
        "model": "Gemini 3.5 Flash (Medium)",
        "pool": "agy:google",
        "prompt_chars": 100,
        "exit_code": 0,
        "latency_s": 2.0,
        "output_chars": 50,
        "ok": True,
        "run_id": "abc123",
        "ts": "2026-07-01T10:00:00+0000",
    }
    base.update(kw)
    return json.dumps(base, ensure_ascii=False)


def test_linha_de_esquema_1_e_lida_sem_quebrar(trilha):
    trilha.write_text(_linha_v1(label="fichar-artigo") + "\n", encoding="utf-8")

    rows = audit.read_all(trilha)
    assert len(rows) == 1
    assert rows[0]["schema_version"] == 1
    assert rows[0]["event"] == "attempt"
    assert rows[0]["seq"] == 0
    assert rows[0]["trajectory_id"] == "v1-fichar-artigo"
    assert audit.counters()["corrupt_lines"] == 0


def test_linha_v1_sem_label_cai_no_run_id(trilha):
    trilha.write_text(_linha_v1() + "\n", encoding="utf-8")
    assert audit.read_all(trilha)[0]["trajectory_id"] == "v1-abc123"


def test_v1_nao_quebra_a_cadeia_nem_impede_a_continuacao(trilha):
    trilha.write_text(_linha_v1() + "\n", encoding="utf-8")
    audit.write(_evento(0, model="novo"), trilha)
    audit.write(_evento(1, model="novo2"), trilha)

    # A cadeia começa no primeiro evento de esquema 2; a linha antiga é pulada.
    assert audit.verify_chain(trilha) == (True, None)
    linhas = _linhas(trilha)
    assert "prev_hash" not in linhas[1]
    assert linhas[2]["prev_hash"] == linhas[1]["hash"]


# --------------------------------------------------------------------------
# Agregação
# --------------------------------------------------------------------------

def _trajetoria(path, tid, *, tentativas, outcome, pool="agy:google", latencia=1.0):
    seq = 0
    audit.write(audit.Event(event="start", trajectory_id=tid, seq=seq, task_class="standard"), path)
    seq += 1
    for i in range(tentativas):
        audit.write(
            audit.Event(
                event="attempt",
                trajectory_id=tid,
                seq=seq,
                task_class="standard",
                backend="agy",
                model=f"m{i}",
                pool=pool,
                prompt_chars=10,
                output_chars=20,
                latency_s=latencia,
                ok=(outcome == "ok" and i == tentativas - 1),
            ),
            path,
        )
        seq += 1
    audit.write(
        audit.Event(
            event="end",
            trajectory_id=tid,
            seq=seq,
            task_class="standard",
            outcome=outcome,
            attempts=tentativas,
            duration_s=tentativas * latencia,
        ),
        path,
    )


def test_summary_agrupa_por_trajetoria(trilha):
    _trajetoria(trilha, "t1", tentativas=3, outcome="ok")
    _trajetoria(trilha, "t2", tentativas=2, outcome="failed")

    s = audit.summary(trilha)

    assert s["total_calls"] == 5          # invocações, não eventos
    assert s["total_events"] == 5 + 2 * 2  # 5 attempts + 2 start + 2 end

    t = s["trajectories"]
    assert t["total"] == 2
    assert t["by_outcome"] == {"ok": 1, "failed": 1, "abandoned": 0, "sem_fim": 0}
    assert t["success_rate"] == 0.5
    assert t["attempts_total"] == 5
    assert t["attempts_avg"] == 2.5
    assert t["detail"]["t1"]["attempts"] == 3
    assert t["detail"]["t1"]["outcome"] == "ok"
    assert t["detail"]["t2"]["latency_s"] == pytest.approx(2.0)


def test_summary_preserva_agregacao_por_pool(trilha):
    """A CLI (`fpch audit`) consome estas chaves. Mudar o formato quebraria ela."""
    _trajetoria(trilha, "t1", tentativas=2, outcome="failed", pool="copilot")

    agg = audit.summary(trilha)["by_pool"]["copilot"]
    assert set(agg) == {"calls", "fail", "latency_avg", "chars_out"}
    assert agg["calls"] == 2
    assert agg["fail"] == 2
    assert agg["chars_out"] == 40
    assert agg["latency_avg"] == 1.0


def test_summary_nao_conta_trajetoria_sem_fim_como_falha(trilha):
    audit.write(_evento(0, tid="incompleta", event="start"), trilha)
    audit.write(_evento(1, tid="incompleta", pool="claude", ok=True), trilha)

    t = audit.summary(trilha)["trajectories"]
    assert t["by_outcome"]["sem_fim"] == 1
    assert t["by_outcome"]["failed"] == 0
    assert t["success_rate"] == 0.0  # nenhuma concluída: não há taxa a afirmar


def test_next_seq_e_por_trajetoria(trilha):
    assert [audit.next_seq("a") for _ in range(3)] == [0, 1, 2]
    assert audit.next_seq("b") == 0        # trajetórias não compartilham contador
    assert audit.next_seq("a") == 3        # e a de "a" continua de onde parou


def test_summary_de_trilha_vazia(tmp_path):
    s = audit.summary(tmp_path / "vazio.jsonl")
    assert s["total_calls"] == 0
    assert s["trajectories"]["total"] == 0
    assert s["by_pool"] == {}
