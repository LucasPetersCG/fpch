"""Testes dos hooks determinísticos (`src/fpch/hooks.py`).

Cobre a seção 6 de `docs/decisoes/fatia-funcional-2026-08-31.md`. O teste mais
importante do lote é `test_criterio_nao_reconhecido_devolve_absent_nunca_pass`:
é o ponto onde o princípio da seção 3 do contrato ("promessa que falha jamais é
lida como promessa cumprida") vira código, e é o teste que teria pego a
regressão mais perigosa possível neste módulo — um `absent` virando `pass`.

Herméticos por construção: nenhum comando de terceiro, nenhuma rede. Os
"comandos" executados são o próprio interpretador Python (`sys.executable -c
...`), o que funciona igual em Windows, Linux e mac — nada de `true`/`echo` de
shell POSIX. Toda trilha vai para `tmp_path`, nunca para `~/.fpch` real.
"""

from __future__ import annotations

import json
import sys

import pytest

from fpch import audit, hooks
from fpch.policy import HookEntry


@pytest.fixture(autouse=True)
def _limpa_estado_da_trilha():
    """`audit` guarda contadores e cache de cadeia em estado de módulo."""
    audit.reset_state()
    yield
    audit.reset_state()


@pytest.fixture
def trilha(tmp_path):
    return tmp_path / "audit.jsonl"


def _linhas(path) -> list[dict]:
    if not path.exists():
        return []
    return [json.loads(ln) for ln in path.read_text(encoding="utf-8").splitlines() if ln.strip()]


def _hook(nome="h", cmd=None, quando="sempre", criterio="exit_zero") -> HookEntry:
    return HookEntry(nome=nome, cmd=tuple(cmd or []), quando=quando, criterio=criterio)


def _py(codigo: str) -> list[str]:
    """Comando portável: o próprio interpretador rodando `codigo`."""
    return [sys.executable, "-c", codigo]


# ---------------------------------------------------------------------------
# 1. criterio = "exit_zero"
# ---------------------------------------------------------------------------

def test_exit_zero_passa_com_codigo_zero(trilha):
    h = _hook(cmd=_py("import sys; sys.exit(0)"), criterio="exit_zero")
    r = hooks.run_hook(h, audit_path=trilha)
    assert r.verdict == "pass"
    assert r.ok is True
    assert r.exit_code == 0


def test_exit_zero_reprova_com_codigo_diferente_de_zero(trilha):
    h = _hook(cmd=_py("import sys; sys.exit(7)"), criterio="exit_zero")
    r = hooks.run_hook(h, audit_path=trilha)
    assert r.verdict == "fail"
    assert r.ok is False
    assert r.exit_code == 7
    assert r.fault_side == "infraestrutura"


# ---------------------------------------------------------------------------
# 2. criterio = "saida_vazia"
# ---------------------------------------------------------------------------

def test_saida_vazia_passa_sem_saida(trilha):
    h = _hook(cmd=_py("pass"), criterio="saida_vazia")
    r = hooks.run_hook(h, audit_path=trilha)
    assert r.verdict == "pass"


def test_saida_vazia_reprova_com_saida(trilha):
    h = _hook(cmd=_py("print('oi')"), criterio="saida_vazia")
    r = hooks.run_hook(h, audit_path=trilha)
    assert r.verdict == "fail"


def test_saida_vazia_ignora_codigo_de_saida_na_decisao(trilha):
    """O critério declarado é sobre a SAÍDA, não sobre o exit code — mesmo um
    exit != 0 sem saída nenhuma deve passar em `saida_vazia`."""
    h = _hook(cmd=_py("import sys; sys.exit(3)"), criterio="saida_vazia")
    r = hooks.run_hook(h, audit_path=trilha)
    assert r.verdict == "pass"
    assert r.exit_code == 3


# ---------------------------------------------------------------------------
# 3. Critério não reconhecido -> absent, NUNCA pass. O teste mais importante.
# ---------------------------------------------------------------------------

def test_criterio_nao_reconhecido_devolve_absent_nunca_pass(trilha):
    h = _hook(cmd=_py("import sys; sys.exit(0)"), criterio="sempre_verde")
    r = hooks.run_hook(h, audit_path=trilha)
    assert r.verdict == "absent"
    assert r.verdict != "pass"
    assert r.ok is False


def test_criterio_vazio_tambem_devolve_absent(trilha):
    h = _hook(cmd=_py("import sys; sys.exit(0)"), criterio="")
    r = hooks.run_hook(h, audit_path=trilha)
    assert r.verdict == "absent"


def test_criterio_nao_reconhecido_nao_executa_o_comando(tmp_path, trilha):
    """Critério ausente é 'não verificou', então o processo nem deveria rodar —
    inclusive porque um hook pode ter efeito colateral (escrever em disco)."""
    marcador = tmp_path / "executou.txt"
    h = _hook(
        cmd=_py(f"open(r'{marcador}', 'w').close()"),
        criterio="criterio_inventado",
    )
    r = hooks.run_hook(h, audit_path=trilha)
    assert r.verdict == "absent"
    assert not marcador.exists()


def test_hook_sem_comando_devolve_absent(trilha):
    h = _hook(cmd=[], criterio="exit_zero")
    r = hooks.run_hook(h, audit_path=trilha)
    assert r.verdict == "absent"


# ---------------------------------------------------------------------------
# 4. Comando inexistente / timeout: nunca pode virar pass.
# ---------------------------------------------------------------------------

def test_comando_inexistente_nao_vira_pass(trilha):
    h = _hook(cmd=["fpch-binario-que-nao-existe-8f3a2c"], criterio="exit_zero")
    r = hooks.run_hook(h, audit_path=trilha)
    assert r.verdict != "pass"
    assert r.verdict == "fail"
    assert r.exit_code is None
    assert r.cause_category == "ambiente"


def test_timeout_nao_vira_pass(trilha):
    h = _hook(cmd=_py("import time; time.sleep(5)"), criterio="exit_zero")
    r = hooks.run_hook(h, timeout_s=0.2, audit_path=trilha)
    assert r.verdict != "pass"
    assert r.verdict == "fail"
    assert r.cause_category == "ambiente"
    assert "timeout" in r.evidence.lower()


def test_timeout_tambem_nao_vira_pass_com_saida_vazia(trilha):
    """Mesmo sob o critério cujo veredito de sucesso é 'não produziu saída', um
    processo que nem terminou não pode ser lido como aprovado."""
    h = _hook(cmd=_py("import time; time.sleep(5)"), criterio="saida_vazia")
    r = hooks.run_hook(h, timeout_s=0.2, audit_path=trilha)
    assert r.verdict == "fail"


# ---------------------------------------------------------------------------
# 4b. Vocabulário fechado (C14 onda 2A): subtipo, source/source_ref, e prova de
# que o evento legítimo não é rejeitado por `Event.__post_init__` (audit.py).
# ---------------------------------------------------------------------------

def test_timeout_emite_subtipo_bloqueio_de_ambiente(trilha):
    h = _hook(cmd=_py("import time; time.sleep(5)"), criterio="exit_zero")
    r = hooks.run_hook(h, timeout_s=0.2, audit_path=trilha)
    assert r.cause_category == "ambiente"
    assert r.cause_subtype == "bloqueio_de_ambiente"


def test_comando_inexistente_emite_subtipo_bloqueio_de_ambiente(trilha):
    h = _hook(cmd=["fpch-binario-que-nao-existe-8f3a2c"], criterio="exit_zero")
    r = hooks.run_hook(h, audit_path=trilha)
    assert r.cause_category == "ambiente"
    assert r.cause_subtype == "bloqueio_de_ambiente"


def test_recusa_generica_do_so_e_ambiente_com_subtipo_residual(trilha, monkeypatch):
    """O terceiro caso de `_executa` (PermissionError/NotADirectoryError/OSError
    genérico) tem categoria determinada e subtipo residual.

    O processo não chegou a rodar, então a falha nasceu fora do que o hook
    deveria julgar — que é a definição de `"ambiente"`. O que não se sabe é o
    subtipo, e para isso a taxonomia adotada tem o slot `"outro"`. Deixar a
    categoria vazia aqui faria a falha sumir do agregado por causa, que é o
    sumiço silencioso que o vocabulário fechado existe para impedir."""
    def _recusa(*a, **k):
        raise PermissionError("acesso negado, simulado")

    monkeypatch.setattr(hooks.subprocess, "run", _recusa)
    h = _hook(cmd=_py("import sys; sys.exit(0)"), criterio="exit_zero")
    r = hooks.run_hook(h, audit_path=trilha)

    assert r.verdict == "fail"
    assert r.cause_category == "ambiente"
    assert r.cause_subtype == "outro"
    assert len(_linhas(trilha)) == 1
    assert _linhas(trilha)[-1]["cause_subtype"] == "outro"


def test_reprovacao_de_hook_que_rodou_continua_sem_categoria(trilha):
    """O par que delimita o teste acima: categoria indeterminada segue vazia.

    Um hook que **executou** e reprovou não diz, por si, se a causa foi
    epistêmica ou de competência. Aqui a categoria é desconhecida, não residual,
    e preenchê-la seria o rótulo chutado que o módulo recusa — a distinção que
    `hooks.py` sustenta é entre categoria determinada com subtipo residual e
    categoria indeterminada, não entre casos conhecidos e o resto."""
    h = _hook(cmd=_py("import sys; sys.exit(1)"), criterio="exit_zero")
    r = hooks.run_hook(h, audit_path=trilha)

    assert r.verdict == "fail"
    assert r.cause_category is None
    assert r.cause_subtype is None
    assert r.fault_side == "infraestrutura"


def test_source_e_source_ref_chegam_a_trilha_com_o_nome_real_do_hook(trilha):
    h = _hook(nome="meu-hook-xyz", cmd=_py("import sys; sys.exit(0)"), criterio="exit_zero")
    hooks.run_hook(h, audit_path=trilha)

    registro = _linhas(trilha)[-1]
    assert registro["source"] == "hook"
    assert registro["source_ref"] == "meu-hook-xyz"


def test_source_e_source_ref_chegam_a_trilha_tambem_numa_reprovacao(trilha):
    """Par positivo: `source`/`source_ref` não dependem do veredito ser `pass` —
    uma reprovação também sabe de que hook concreto ela veio."""
    h = _hook(nome="outro-hook", cmd=_py("import sys; sys.exit(1)"), criterio="exit_zero")
    hooks.run_hook(h, audit_path=trilha)

    registro = _linhas(trilha)[-1]
    assert registro["verdict"] == "fail"
    assert registro["source"] == "hook"
    assert registro["source_ref"] == "outro-hook"


def test_evento_de_timeout_e_gravado_na_trilha_sem_ser_rejeitado(trilha):
    """Par positivo de `test_timeout_emite_subtipo_bloqueio_de_ambiente`: o par
    `cause_category='ambiente'` + `cause_subtype='bloqueio_de_ambiente'` que
    `run_hook` monta é vocabulário legítimo — `Event.__post_init__` (audit.py)
    aceita e a linha chega ao disco intacta, não é descartada em silêncio."""
    h = _hook(cmd=_py("import time; time.sleep(5)"), criterio="exit_zero")
    hooks.run_hook(h, timeout_s=0.2, audit_path=trilha)

    registro = _linhas(trilha)[-1]
    assert registro["cause_category"] == "ambiente"
    assert registro["cause_subtype"] == "bloqueio_de_ambiente"
    assert registro["fault_side"] == "infraestrutura"


def test_evento_de_binario_ausente_e_gravado_na_trilha_sem_ser_rejeitado(trilha):
    """Par positivo de `test_comando_inexistente_emite_subtipo_bloqueio_de_ambiente`."""
    h = _hook(cmd=["fpch-binario-que-nao-existe-8f3a2c"], criterio="exit_zero")
    hooks.run_hook(h, audit_path=trilha)

    registro = _linhas(trilha)[-1]
    assert registro["cause_category"] == "ambiente"
    assert registro["cause_subtype"] == "bloqueio_de_ambiente"


# ---------------------------------------------------------------------------
# 5. evidence truncada em EVIDENCE_MAX (500) caracteres.
# ---------------------------------------------------------------------------

def test_evidence_truncada_no_limite(trilha):
    h = _hook(cmd=_py("print('x' * 1000)"), criterio="saida_vazia")
    r = hooks.run_hook(h, audit_path=trilha)
    assert r.verdict == "fail"
    assert len(r.evidence) <= hooks.EVIDENCE_MAX
    assert r.evidence.endswith("...")


def test_evidence_curta_nao_e_alterada(trilha):
    h = _hook(cmd=_py("print('curto')"), criterio="saida_vazia")
    r = hooks.run_hook(h, audit_path=trilha)
    assert not r.evidence.endswith("...") or len(r.evidence) <= 20
    assert len(r.evidence) <= hooks.EVIDENCE_MAX


def test_truncate_helper_respeita_o_teto_exato():
    texto = "a" * 800
    resultado = hooks._truncate(texto, limite=hooks.EVIDENCE_MAX)
    assert len(resultado) == hooks.EVIDENCE_MAX
    assert resultado.endswith("...")


# ---------------------------------------------------------------------------
# 6. applicable() — filtragem por `quando`.
# ---------------------------------------------------------------------------

def test_applicable_sem_quando_devolve_todos():
    entradas = (
        _hook(nome="a", quando="sempre"),
        _hook(nome="b", quando="pre_commit"),
        _hook(nome="c", quando="post_merge"),
    )
    assert hooks.applicable(entradas, None) == entradas


def test_applicable_filtra_por_momento_exato_mais_curinga():
    entradas = (
        _hook(nome="a", quando="sempre"),
        _hook(nome="b", quando="pre_commit"),
        _hook(nome="c", quando="post_merge"),
    )
    selecionados = hooks.applicable(entradas, "pre_commit")
    assert [h.nome for h in selecionados] == ["a", "b"]


def test_applicable_momento_sem_hook_correspondente_so_pega_o_curinga():
    entradas = (
        _hook(nome="a", quando="sempre"),
        _hook(nome="b", quando="pre_commit"),
    )
    selecionados = hooks.applicable(entradas, "post_merge")
    assert [h.nome for h in selecionados] == ["a"]


def test_applicable_nao_normaliza_sinonimos():
    """Momento que não casa exatamente (nem é 'sempre') é momento que não roda —
    sem adivinhação de sinônimo."""
    entradas = (_hook(nome="a", quando="Pre_Commit"),)
    assert hooks.applicable(entradas, "pre_commit") == ()


def test_applicable_conjunto_vazio():
    assert hooks.applicable((), "pre_commit") == ()


# ---------------------------------------------------------------------------
# 7. overall() — agregação dos vereditos.
# ---------------------------------------------------------------------------

def _resultado(verdict: str) -> hooks.HookResult:
    return hooks.HookResult(nome="x", verdict=verdict, evidence="", criterio="exit_zero")


def test_overall_conjunto_vazio_e_absent_nao_pass():
    assert hooks.overall(()) == "absent"
    assert hooks.overall([]) == "absent"


def test_overall_todos_pass_e_pass():
    assert hooks.overall([_resultado("pass"), _resultado("pass")]) == "pass"


def test_overall_qualquer_fail_prevalece_sobre_absent():
    assert hooks.overall([_resultado("pass"), _resultado("fail"), _resultado("absent")]) == "fail"


def test_overall_um_unico_fail_basta():
    assert hooks.overall([_resultado("fail")]) == "fail"


def test_overall_absent_sem_fail_e_absent():
    assert hooks.overall([_resultado("pass"), _resultado("absent")]) == "absent"


# ---------------------------------------------------------------------------
# Integração: run_all / check / CheckReport — trilha, seq, exit_code.
# ---------------------------------------------------------------------------

def test_run_all_agrega_e_registra_todos_na_mesma_trajetoria(trilha):
    entradas = (
        _hook(nome="ok", cmd=_py("import sys; sys.exit(0)"), criterio="exit_zero"),
        _hook(nome="quebra", cmd=_py("import sys; sys.exit(1)"), criterio="exit_zero"),
    )
    report = hooks.run_all(entradas, audit_path=trilha)

    assert report.verdict == "fail"
    assert report.exit_code == 1
    assert len(report.results) == 2

    eventos = _linhas(trilha)
    assert len(eventos) == 2
    ids = {e["trajectory_id"] for e in eventos}
    assert ids == {report.trajectory_id}
    assert sorted(e["seq"] for e in eventos) == [0, 1]


def test_run_all_continua_apos_primeiro_fail(trilha):
    """Todos os hooks rodam, mesmo depois de um reprovar — não para no primeiro erro."""
    entradas = (
        _hook(nome="quebra", cmd=_py("import sys; sys.exit(1)"), criterio="exit_zero"),
        _hook(nome="ok", cmd=_py("import sys; sys.exit(0)"), criterio="exit_zero"),
    )
    report = hooks.run_all(entradas, audit_path=trilha)
    assert [r.nome for r in report.results] == ["quebra", "ok"]
    assert [r.verdict for r in report.results] == ["fail", "pass"]


def test_run_all_sem_hook_aplicavel_e_absent_com_nota(trilha):
    entradas = (_hook(nome="a", quando="pre_commit"),)
    report = hooks.run_all(entradas, quando="post_merge", audit_path=trilha)
    assert report.verdict == "absent"
    assert report.results == ()
    assert "AUSENTE" in report.nota
    assert report.exit_code == 1


def test_check_report_exit_code_reflete_apenas_pass():
    assert hooks.CheckReport(verdict="pass").exit_code == 0
    assert hooks.CheckReport(verdict="fail").exit_code == 1
    assert hooks.CheckReport(verdict="absent").exit_code == 1


def test_check_usa_hooks_da_policy(trilha):
    import dataclasses

    from fpch import policy

    pol = dataclasses.replace(
        policy._default(),
        hooks=(_hook(nome="ok", cmd=_py("import sys; sys.exit(0)"), criterio="exit_zero"),),
    )
    report = hooks.check(pol, audit_path=trilha)
    assert report.verdict == "pass"
    assert report.results[0].nome == "ok"


def test_emitir_false_nao_escreve_na_trilha(trilha):
    h = _hook(cmd=_py("import sys; sys.exit(0)"), criterio="exit_zero")
    r = hooks.run_hook(h, audit_path=trilha, emitir=False)
    assert r.verdict == "pass"
    assert _linhas(trilha) == []


def test_trilha_ok_propaga_falha_de_escrita(monkeypatch, trilha):
    monkeypatch.setattr(audit, "write", lambda *a, **kw: False)
    h = _hook(cmd=_py("import sys; sys.exit(0)"), criterio="exit_zero")
    r = hooks.run_hook(h, audit_path=trilha)
    assert r.verdict == "pass"
    assert r.trilha_ok is False


def test_seq_encadeia_quando_trajectory_id_e_compartilhado(trilha):
    tid = "traj-compartilhada"
    h1 = _hook(nome="a", cmd=_py("import sys; sys.exit(0)"), criterio="exit_zero")
    h2 = _hook(nome="b", cmd=_py("import sys; sys.exit(0)"), criterio="exit_zero")
    hooks.run_hook(h1, trajectory_id=tid, audit_path=trilha)
    hooks.run_hook(h2, trajectory_id=tid, audit_path=trilha)
    eventos = _linhas(trilha)
    assert [e["trajectory_id"] for e in eventos] == [tid, tid]
    assert [e["seq"] for e in eventos] == [0, 1]
