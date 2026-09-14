"""Testes da trilha de auditoria.

Primeira cobertura do módulo. O que se testa aqui não é "o dataclass tem os
campos certos" — é a única coisa que a trilha promete e que, se não for verdade,
invalida o dado empírico do TCC: **adulteração é detectável e perda é visível.**

Nenhum teste toca o `~/.fpch` real: todos escrevem em `tmp_path`.
"""

from __future__ import annotations

import json
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

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


def test_dois_writers_concorrentes_nao_reutilizam_prev_hash(
    trilha, monkeypatch
):
    """O segundo writer só calcula a ponta depois do append durável do primeiro."""
    # Evita que o write controlado abaixo intercepte a inicialização de um byte
    # do sidecar antes de chegar ao append JSON que interessa ao teste.
    trilha.with_name(trilha.name + ".lock").write_bytes(b"0")
    real_write = audit.os.write
    first_inside_append = threading.Event()
    release_first = threading.Event()
    selection_guard = threading.Lock()
    selected = False

    def controlled_write(descriptor, data):
        nonlocal selected
        should_pause = False
        if bytes(data).startswith(b"{"):
            with selection_guard:
                if not selected:
                    selected = True
                    should_pause = True
        if should_pause:
            first_inside_append.set()
            assert release_first.wait(5), "teste não liberou o primeiro writer"
        return real_write(descriptor, data)

    monkeypatch.setattr(audit.os, "write", controlled_write)
    with ThreadPoolExecutor(max_workers=2) as executor:
        first = executor.submit(audit.write, _evento(0, tid="concorrente"), trilha)
        assert first_inside_append.wait(5), "primeiro writer não chegou ao append"
        second = executor.submit(audit.write, _evento(1, tid="concorrente"), trilha)
        release_first.set()
        assert first.result(timeout=5) is True
        assert second.result(timeout=5) is True

    linhas = _linhas(trilha)
    assert len(linhas) == 2
    assert "prev_hash" not in linhas[0]
    assert linhas[1]["prev_hash"] == linhas[0]["hash"]
    assert linhas[1]["hash"] != linhas[0]["hash"]
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


def test_falha_de_fsync_retorna_false_reverte_append_e_preserva_evento(
    trilha, monkeypatch, capsys
):
    event = _evento(0, model="sem-durabilidade")
    before = (event.prev_hash, event.hash)

    def broken_fsync(_descriptor):
        raise OSError("fsync indisponível")

    monkeypatch.setattr(audit.os, "fsync", broken_fsync)

    assert audit.write(event, trilha) is False
    assert trilha.read_bytes() == b""
    assert (event.prev_hash, event.hash) == before
    assert audit.counters()["write_failures"] == 1
    assert "fsync indisponível" in capsys.readouterr().err


def test_trilha_symlink_e_rejeitada_sem_tocar_destino(tmp_path, capsys):
    destination = tmp_path / "destino.jsonl"
    destination.write_text("preservar\n", encoding="utf-8")
    trail = tmp_path / "audit.jsonl"
    try:
        trail.symlink_to(destination)
    except (NotImplementedError, OSError) as exc:
        pytest.skip(f"links simbólicos indisponíveis neste ambiente: {exc}")

    assert audit.write(_evento(0), trail) is False
    assert destination.read_text(encoding="utf-8") == "preservar\n"
    assert "link, junction ou reparse" in capsys.readouterr().err


def test_trilha_marcada_como_junction_e_rejeitada(
    tmp_path, monkeypatch, capsys
):
    trail = tmp_path / "audit.jsonl"
    trail.mkdir()
    real_isjunction = getattr(audit.os.path, "isjunction", lambda _path: False)
    monkeypatch.setattr(
        audit.os.path,
        "isjunction",
        lambda path: Path(path) == trail or real_isjunction(path),
        raising=False,
    )

    assert audit.write(_evento(0), trail) is False
    assert trail.is_dir()
    assert tuple(trail.iterdir()) == ()
    assert "link, junction ou reparse" in capsys.readouterr().err


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
    assert s["by_cause"] == {}
    assert s["by_source"] == {}


# --------------------------------------------------------------------------
# Vocabulário fechado — validação na construção
#
# Metade destes casos existe para provar que o verificador **não acusa o
# inocente**: para cada rejeição há o par que mostra o caso legítimo vizinho
# passando. Uma suíte só de casos positivos não distingue "valida" de "recusa
# tudo".
# --------------------------------------------------------------------------

@pytest.mark.parametrize("categoria", sorted(audit.CAUSE_TAXONOMY))
def test_toda_categoria_da_taxonomia_e_aceita(categoria):
    ev = _evento(0, event="verify", cause_category=categoria)
    assert ev.cause_category == categoria


@pytest.mark.parametrize("categoria,subtipo", [
    (cat, sub) for cat, subs in audit.CAUSE_TAXONOMY.items() for sub in subs
])
def test_todo_subtipo_e_aceito_na_sua_categoria(categoria, subtipo):
    ev = _evento(0, event="verify", cause_category=categoria, cause_subtype=subtipo)
    assert (ev.cause_category, ev.cause_subtype) == (categoria, subtipo)


def test_categoria_invalida_levanta_com_o_valor_na_mensagem():
    with pytest.raises(ValueError) as exc:
        _evento(0, cause_category="epistêmica")   # acento: erro de digitação plausível
    msg = str(exc.value)
    assert "epistêmica" in msg          # diz o que recebeu
    assert "epistemica" in msg          # e o que aceitaria


def test_subtipo_na_categoria_errada_levanta():
    with pytest.raises(ValueError) as exc:
        _evento(0, cause_category="ambiente", cause_subtype="premissa_falsa")
    msg = str(exc.value)
    assert "premissa_falsa" in msg
    assert "bloqueio_de_ambiente" in msg


def test_subtipo_sem_categoria_levanta():
    with pytest.raises(ValueError) as exc:
        _evento(0, cause_subtype="premissa_falsa")
    assert "cause_category" in str(exc.value)


def test_categoria_sem_subtipo_e_legitima():
    """Os hooks gravam só a categoria quando não há base para o subtipo."""
    ev = _evento(0, event="verify", cause_category="ambiente")
    assert ev.cause_subtype is None


@pytest.mark.parametrize("lado", audit.FAULT_SIDES)
def test_fault_side_valido_e_aceito(lado):
    assert _evento(0, fault_side=lado).fault_side == lado


def test_fault_side_invalido_levanta():
    with pytest.raises(ValueError) as exc:
        _evento(0, fault_side="infra")
    assert "infra" in str(exc.value)


@pytest.mark.parametrize("origem", audit.SOURCES)
def test_source_valido_e_aceito(origem):
    ev = _evento(0, source=origem, source_ref="qualquer/coisa")
    assert (ev.source, ev.source_ref) == (origem, "qualquer/coisa")


def test_source_invalido_levanta():
    with pytest.raises(ValueError) as exc:
        _evento(0, source="hooks")   # plural: o vocabulário é "hook"
    assert "hooks" in str(exc.value)


def test_source_ref_e_texto_livre():
    """`source` é fechado; `source_ref` não — é o identificador concreto."""
    ev = _evento(0, source="mcp", source_ref="servidor-que-ninguem-previu")
    assert ev.source_ref == "servidor-que-ninguem-previu"


@pytest.mark.parametrize("marca", audit.MARKS)
def test_mark_valido_e_aceito(marca):
    ev = audit.Event(event="annotate", trajectory_id="t1", seq=1, mark=marca, target_seq=0)
    assert ev.mark == marca


def test_mark_invalido_levanta():
    with pytest.raises(ValueError) as exc:
        audit.Event(event="annotate", trajectory_id="t1", seq=1, mark="t_fim", target_seq=0)
    assert "t_fim" in str(exc.value)


def test_evento_sem_diagnostico_nenhum_e_valido():
    """O caso mais comum da trilha: nada de causa, nada de fonte, nada de marco."""
    ev = _evento(0, model="m", ok=True)
    assert (ev.cause_category, ev.source, ev.mark, ev.inverse) == (None, None, None, None)


# --------------------------------------------------------------------------
# Invariante do evento `install`
# --------------------------------------------------------------------------

def test_install_sem_inverse_levanta():
    with pytest.raises(ValueError) as exc:
        audit.Event(event="install", trajectory_id="t1", seq=0, evidence="copiou o hook")
    assert "inverse" in str(exc.value)


def test_install_com_inverse_grava(trilha):
    ok = audit.write(
        audit.Event(
            event="install",
            trajectory_id="t1",
            seq=0,
            source="hook",
            source_ref="pytest",
            inverse="rm -f .git/hooks/pre-commit",
        ),
        trilha,
    )
    assert ok is True
    row = _linhas(trilha)[0]
    assert row["inverse"] == "rm -f .git/hooks/pre-commit"
    assert audit.verify_chain(trilha) == (True, None)


def test_inverse_em_evento_que_nao_e_install_nao_e_exigido_nem_proibido(trilha):
    """A invariante é sobre `install`, não sobre o campo: não acusar o inocente."""
    assert audit.write(_evento(0, model="m", inverse="git checkout -- ."), trilha) is True
    assert _linhas(trilha)[0]["inverse"] == "git checkout -- ."


# --------------------------------------------------------------------------
# `write()` mantém o contrato antigo com os campos novos
# --------------------------------------------------------------------------

def test_write_com_campos_novos_nao_levanta_em_falha_de_io(tmp_path, capsys):
    alvo = tmp_path / "audit.jsonl"
    alvo.mkdir()
    ev = audit.Event(
        event="install", trajectory_id="t1", seq=0,
        source="skill", source_ref="fpch-init", inverse="fpch uninstall",
    )
    assert audit.write(ev, alvo) is False        # devolve, não levanta
    assert audit.counters()["write_failures"] == 1
    assert "AVISO" in capsys.readouterr().err


def test_cadeia_integra_com_os_campos_novos(trilha):
    audit.write(_evento(0, event="start"), trilha)
    audit.write(
        _evento(1, event="verify", component="pytest", verdict="fail",
                cause_category="competencia", cause_subtype="lacuna_de_conhecimento",
                fault_side="modelo", source="hook", source_ref="pytest"),
        trilha,
    )
    audit.write(
        audit.Event(event="annotate", trajectory_id="t1", seq=2,
                    mark="t_err", target_seq=1, evidence="o erro decisivo foi aqui"),
        trilha,
    )
    assert audit.verify_chain(trilha) == (True, None)
    linhas = _linhas(trilha)
    for anterior, atual in zip(linhas, linhas[1:]):
        assert atual["prev_hash"] == anterior["hash"]


def test_adulteracao_de_annotate_e_detectada(trilha):
    """Remarcar escondido é o ataque que o encadeamento tem que pegar."""
    audit.write(_evento(0, event="start"), trilha)
    audit.write(
        audit.Event(event="annotate", trajectory_id="t1", seq=1,
                    mark="t_err", target_seq=0, evidence="justificativa"),
        trilha,
    )
    audit.write(_evento(2, model="m"), trilha)

    linhas = _linhas(trilha)
    linhas[1]["target_seq"] = 2          # move o marco sem gravar novo evento
    trilha.write_text(
        "\n".join(json.dumps(r, sort_keys=True, ensure_ascii=False) for r in linhas) + "\n",
        encoding="utf-8",
    )

    ok, quebrado = audit.verify_chain(trilha)
    assert ok is False
    assert quebrado == 1


# --------------------------------------------------------------------------
# Compatibilidade de leitura: esquemas 1, 2 e 3
# --------------------------------------------------------------------------

def _linha_v2(**kw) -> str:
    base = {
        "event": "attempt",
        "trajectory_id": "t-antiga",
        "seq": 0,
        "ts": "2026-08-01T10:00:00.000000-03:00",
        "schema_version": 2,
        "task_class": "standard",
        "backend": "agy",
        "model": "m",
        "pool": "agy:google",
        "prompt_chars": 10,
        "output_chars": 20,
        "latency_s": 1.0,
        "ok": True,
    }
    base.update(kw)
    base["hash"] = audit.compute_hash(base)
    return json.dumps(base, sort_keys=True, ensure_ascii=False, separators=(",", ":"))


def _linha_v3(**kw) -> str:
    """Evento mínimo do esquema anterior, para provar leitura retrocompatível."""
    return _linha_v2(schema_version=3, trajectory_id="t-esquema-3", **kw)


def test_linha_de_esquema_2_continua_legivel_e_encadeavel(trilha):
    trilha.write_text(_linha_v2() + "\n", encoding="utf-8")

    rows = audit.read_all(trilha)
    assert len(rows) == 1
    assert rows[0]["schema_version"] == 2
    assert audit.verify_chain(trilha) == (True, None)
    assert audit.counters()["unknown_vocab"] == 0

    # E o esquema atual continua a cadeia a partir dela, sem reescrever nada.
    audit.write(_evento(1, tid="t-antiga", model="novo"), trilha)
    linhas = _linhas(trilha)
    assert linhas[0]["schema_version"] == 2
    assert linhas[1]["schema_version"] == audit.SCHEMA_VERSION == 4
    assert linhas[1]["prev_hash"] == linhas[0]["hash"]
    assert audit.verify_chain(trilha) == (True, None)


def test_esquemas_1_2_3_e_4_convivem_na_mesma_trilha(trilha):
    linha_v2 = _linha_v2()
    linha_v3 = _linha_v3(prev_hash=json.loads(linha_v2)["hash"])
    trilha.write_text(
        _linha_v1(label="antigo") + "\n" + linha_v2 + "\n" + linha_v3 + "\n",
        encoding="utf-8",
    )
    audit.write(_evento(0, tid="nova", model="m"), trilha)

    rows = audit.read_all(trilha)
    assert {r["schema_version"] for r in rows} == {1, 2, 3, 4}
    assert audit.verify_chain(trilha) == (True, None)
    assert audit.summary(trilha)["total_calls"] == 4


# --------------------------------------------------------------------------
# Vocabulário desconhecido vindo do disco
# --------------------------------------------------------------------------

def test_vocabulario_desconhecido_do_disco_cai_em_interrogacao(trilha, capsys):
    trilha.write_text(
        _linha_v2(cause_category="ambiental", cause_subtype="sei_la", source="plugin") + "\n",
        encoding="utf-8",
    )

    rows = audit.read_all(trilha)          # não levanta: a linha já está no disco
    assert len(rows) == 1
    # três campos fora do vocabulário na mesma linha: categoria, subtipo e fonte
    assert audit.counters()["unknown_vocab"] == 3
    assert "AVISO" in capsys.readouterr().err

    s = audit.summary(trilha)
    assert s["by_cause"]["?"]["total"] == 1
    assert s["by_cause"]["?"]["subtypes"] == {"?": 1}
    assert s["by_source"]["?"]["total"] == 1


def test_vocabulario_vigente_do_disco_nao_conta_como_desconhecido(trilha, capsys):
    """O par que prova que o contador não acusa o inocente."""
    trilha.write_text(
        _linha_v2(cause_category="ambiente", cause_subtype="bloqueio_de_ambiente",
                  fault_side="infraestrutura", source="hook", source_ref="pytest") + "\n",
        encoding="utf-8",
    )

    audit.read_all(trilha)
    assert audit.counters()["unknown_vocab"] == 0
    assert capsys.readouterr().err == ""

    s = audit.summary(trilha)
    assert "?" not in s["by_cause"]
    assert s["by_cause"]["ambiente"]["subtypes"] == {"bloqueio_de_ambiente": 1}


def test_subtipo_desconhecido_sob_categoria_conhecida_vira_interrogacao(trilha):
    trilha.write_text(
        _linha_v2(cause_category="ambiente", cause_subtype="bloqueio_de_rede") + "\n",
        encoding="utf-8",
    )
    s = audit.summary(trilha)
    # A categoria é boa e é preservada; só o subtipo cai no balde do desconhecido.
    assert s["by_cause"]["ambiente"]["total"] == 1
    assert s["by_cause"]["ambiente"]["subtypes"] == {"?": 1}
    assert audit.counters()["unknown_vocab"] == 1


# --------------------------------------------------------------------------
# Processo de falha
# --------------------------------------------------------------------------

def _anota(path, tid, seq, marca, alvo):
    audit.write(
        audit.Event(event="annotate", trajectory_id=tid, seq=seq,
                    mark=marca, target_seq=alvo, evidence=f"marco {marca}"),
        path,
    )


def test_failure_process_calcula_as_duas_derivadas(trilha):
    for i in range(7):
        audit.write(_evento(i, tid="t1", model=f"m{i}"), trilha)
    _anota(trilha, "t1", 7, "t_err", 1)
    _anota(trilha, "t1", 8, "t_lock", 4)
    _anota(trilha, "t1", 9, "t_obs", 6)

    fp = audit.failure_process(trilha)["t1"]
    assert (fp["t_err"], fp["t_lock"], fp["t_obs"]) == (1, 4, 6)
    assert fp["fix_window"] == 3
    assert fp["observability_lag"] == 2
    assert fp["remarcado"] is False
    # Anotar não reescreveu nada: a cadeia segue íntegra e as anotações estão nela.
    assert audit.verify_chain(trilha) == (True, None)


def test_failure_process_devolve_none_para_marco_ausente(trilha):
    audit.write(_evento(0, tid="t1", model="m"), trilha)
    _anota(trilha, "t1", 1, "t_err", 0)

    fp = audit.failure_process(trilha)["t1"]
    assert fp["t_err"] == 0
    assert fp["t_lock"] is None and fp["t_obs"] is None
    # Ausência de marco não é janela zero: `None` é a resposta honesta.
    assert fp["fix_window"] is None
    assert fp["observability_lag"] is None


def test_failure_process_reporta_remarcacao_e_vale_o_ultimo(trilha):
    for i in range(5):
        audit.write(_evento(i, tid="t1", model=f"m{i}"), trilha)
    _anota(trilha, "t1", 5, "t_err", 1)
    _anota(trilha, "t1", 6, "t_lock", 4)
    _anota(trilha, "t1", 7, "t_err", 3)      # mudou de opinião, e isso aparece

    fp = audit.failure_process(trilha)["t1"]
    assert fp["t_err"] == 3
    assert fp["fix_window"] == 1
    assert fp["remarcado"] is True


def test_failure_process_ignora_trajetoria_sem_anotacao(trilha):
    _trajetoria(trilha, "sem_marco", tentativas=2, outcome="failed")
    audit.write(_evento(0, tid="com_marco", event="start"), trilha)
    _anota(trilha, "com_marco", 1, "t_err", 0)

    fp = audit.failure_process(trilha)
    assert set(fp) == {"com_marco"}


def test_failure_process_de_trilha_vazia(tmp_path):
    assert audit.failure_process(tmp_path / "vazio.jsonl") == {}


def test_failure_process_separa_trajetorias(trilha):
    for tid, err, lock in (("t1", 0, 2), ("t2", 1, 5)):
        audit.write(_evento(0, tid=tid, event="start"), trilha)
        _anota(trilha, tid, 10, "t_err", err)
        _anota(trilha, tid, 11, "t_lock", lock)

    fp = audit.failure_process(trilha)
    assert fp["t1"]["fix_window"] == 2
    assert fp["t2"]["fix_window"] == 4


# --------------------------------------------------------------------------
# Agregação por causa e por fonte
# --------------------------------------------------------------------------

def test_summary_agrega_por_causa_incluindo_o_balde_desconhecido(trilha):
    audit.write(_evento(0, event="verify", cause_category="epistemica",
                        cause_subtype="premissa_falsa"), trilha)
    audit.write(_evento(1, event="verify", cause_category="epistemica",
                        cause_subtype="premissa_falsa"), trilha)
    audit.write(_evento(2, event="verify", cause_category="epistemica"), trilha)
    audit.write(_evento(3, event="verify", cause_category="ambiente",
                        cause_subtype="bloqueio_de_ambiente"), trilha)
    with trilha.open("a", encoding="utf-8") as fh:
        fh.write(_linha_v2(cause_category="chute_do_passado") + "\n")

    by_cause = audit.summary(trilha)["by_cause"]
    assert by_cause["epistemica"]["total"] == 3
    assert by_cause["epistemica"]["subtypes"] == {"premissa_falsa": 2}
    assert by_cause["ambiente"] == {"total": 1, "subtypes": {"bloqueio_de_ambiente": 1}}
    assert by_cause["?"]["total"] == 1


def test_summary_agrega_por_fonte_com_referencia_concreta(trilha):
    audit.write(_evento(0, event="verify", source="hook", source_ref="pytest"), trilha)
    audit.write(_evento(1, event="verify", source="hook", source_ref="pytest"), trilha)
    audit.write(_evento(2, event="verify", source="hook", source_ref="ruff"), trilha)
    audit.write(_evento(3, event="verify", source="mcp"), trilha)

    by_source = audit.summary(trilha)["by_source"]
    assert by_source["hook"]["total"] == 3
    assert by_source["hook"]["refs"] == {"pytest": 2, "ruff": 1}
    # Fonte sem referência conta na fonte e não inventa uma referência.
    assert by_source["mcp"] == {"total": 1, "refs": {}}


def test_summary_conta_causa_fora_de_attempt(trilha):
    """Quem diagnostica é o hook (`verify`); contar só `attempt` zeraria a coluna."""
    _trajetoria(trilha, "t1", tentativas=1, outcome="failed")
    audit.write(_evento(9, tid="t1", event="verify", component="pytest", verdict="fail",
                        cause_category="ambiente", cause_subtype="bloqueio_de_ambiente",
                        source="hook", source_ref="pytest"), trilha)

    s = audit.summary(trilha)
    assert s["total_calls"] == 1                      # nada mudou no contrato antigo
    assert s["by_cause"]["ambiente"]["total"] == 1
    assert s["by_source"]["hook"]["refs"] == {"pytest": 1}


def test_summary_sem_causa_nem_fonte_nao_inventa_baldes(trilha):
    _trajetoria(trilha, "t1", tentativas=2, outcome="ok")
    s = audit.summary(trilha)
    assert s["by_cause"] == {}
    assert s["by_source"] == {}


# --------------------------------------------------------------------------
# Contrato de saída (C18) — campo opcional, sem subir o esquema
# --------------------------------------------------------------------------

def test_vocabulario_de_contrato_espelha_backends():
    from fpch import backends

    assert audit.CONTRACTS == backends.FPCH_CONTRATOS


def test_contrato_fora_do_vocabulario_e_rejeitado_na_construcao():
    with pytest.raises(ValueError, match="contract"):
        _evento(0, contract="envelope")


def test_attempt_sem_contrato_anterior_a_c18_continua_legivel_e_integro(trilha):
    """Linha antiga (sem `contract`) e nova (com) convivem na mesma cadeia."""
    audit.write(_evento(0, model="m", ok=True), trilha)
    audit.write(_evento(1, model="m", ok=True, contract="sentinela"), trilha)

    antiga, nova = _linhas(trilha)
    assert "contract" not in antiga
    assert nova["contract"] == "sentinela"
    assert antiga["schema_version"] == nova["schema_version"] == audit.SCHEMA_VERSION
    assert audit.verify_chain(trilha) == (True, None)
    assert audit.counters()["unknown_vocab"] == 0


#: Evento `attempt` anterior a C18, CONGELADO. Construído com `Event` e
#: `compute_hash` de `git show HEAD:src/fpch/audit.py` (commit 3e2e0b2, esquema
#: 4), serializado como `write()` grava. Não regenerar: se este literal deixar de
#: conferir, alguma mudança tornou ilegível a trilha que já existe em disco.
LINHA_PRE_C18 = (
    '{"backend":"agy","event":"attempt","exit_code":0,'
    '"hash":"a76cc2b4bf4cd33508b8d4817370523932ad47140cf7601e1786b7ab8ca87a79",'
    '"label":"congelada","latency_s":1.5,"model":"Gemini 3.5 Flash (Low)","ok":true,'
    '"output_chars":87,"pool":"local","prompt_chars":321,"schema_version":4,"seq":1,'
    '"task_class":"standard","trajectory_id":"pre-c18-congelada",'
    '"ts":"2026-09-10T10:00:00.000000-03:00"}'
)
HASH_PRE_C18 = "a76cc2b4bf4cd33508b8d4817370523932ad47140cf7601e1786b7ab8ca87a79"


def test_linha_congelada_pre_c18_confere_e_encadeia_com_evento_novo(trilha):
    trilha.write_text(LINHA_PRE_C18 + "\n", encoding="utf-8")
    row = json.loads(LINHA_PRE_C18)

    assert audit.compute_hash(row) == HASH_PRE_C18 == row["hash"]
    assert audit.verify_chain(trilha) == (True, None)

    # Evento de C18 (com os campos novos) acrescentado depois da linha antiga.
    assert audit.write(
        _evento(2, tid="pre-c18-congelada", model="m", ok=False,
                contract="sentinela", failure_signature="quota exceeded"),
        trilha,
    ) is True
    antiga, nova = _linhas(trilha)
    assert antiga == row, "a linha antiga não pode ser reescrita"
    assert nova["prev_hash"] == HASH_PRE_C18
    assert nova["failure_signature"] == "quota exceeded"
    assert audit.verify_chain(trilha) == (True, None)
    assert audit.counters()["unknown_vocab"] == 0
