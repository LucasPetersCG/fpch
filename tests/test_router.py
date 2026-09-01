"""Testes do roteador — a instrumentação da trilha, não a qualidade da escolha.

O que o roteador promete à trilha é estreito e verificável: **toda trajetória tem
começo e fim registrados, e as tentativas entre os dois são contáveis.** É disso
que sai o "quantas tentativas custou" do texto; sem `end` em todos os caminhos, a
diferença entre "fracassou" e "o processo morreu no meio" some da trilha.

`backends.invoke` e `backends.available` são substituídos em todos os testes:
nenhum processo externo é iniciado aqui.
"""

from __future__ import annotations

import pytest

from fpch import audit, backends, router
from fpch.models import BY_ID, Pool, TaskClass

MODELO_FIXO = "Gemini 3.5 Flash (Low)"


@pytest.fixture(autouse=True)
def trilha(tmp_path, monkeypatch):
    """Isola a trilha em tmp_path. O `~/.fpch` do autor nunca é tocado."""
    alvo = tmp_path / "audit.jsonl"
    monkeypatch.setenv("FPCH_AUDIT_LOG", str(alvo))
    audit.reset_state()
    yield alvo
    audit.reset_state()


@pytest.fixture(autouse=True)
def _tudo_instalado(monkeypatch):
    monkeypatch.setattr(backends, "available", lambda _b: True)


def _resultado(model, *, ok: bool, texto: str = "saida") -> backends.Result:
    return backends.Result(
        ok=ok,
        text=texto if ok else "",
        exit_code=0 if ok else 1,
        latency_s=1.5,
        model=model.id,
        backend=model.backend,
        pool=model.pool,
        error=None if ok else "falhou",
    )


def _instala_invoke(monkeypatch, comportamento):
    """Substitui `backends.invoke` e registra as chamadas recebidas."""
    chamadas: list[str] = []

    def fake(model, prompt, **kw):
        chamadas.append(model.id)
        return comportamento(model, prompt, len(chamadas))

    monkeypatch.setattr(backends, "invoke", fake)
    return chamadas


def _eventos(trilha) -> list[dict]:
    return audit.read_all(trilha)


# --------------------------------------------------------------------------
# Os três caminhos de saída
# --------------------------------------------------------------------------

def test_sucesso_emite_start_attempt_end(trilha, tmp_path, monkeypatch):
    _instala_invoke(monkeypatch, lambda m, p, n: _resultado(m, ok=True))

    res = router.route(TaskClass.STANDARD, "prompt", workdir=tmp_path, label="lbl")
    assert res.ok is True

    evs = _eventos(trilha)
    assert [e["event"] for e in evs] == ["start", "attempt", "end"]
    assert [e["seq"] for e in evs] == [0, 1, 2]
    assert len({e["trajectory_id"] for e in evs}) == 1
    assert evs[-1]["outcome"] == "ok"
    assert evs[-1]["attempts"] == 1
    assert all(e["task_class"] == "standard" and e["label"] == "lbl" for e in evs)


def test_tres_tentativas_produzem_start_3_attempt_e_end(trilha, tmp_path, monkeypatch):
    chamadas = _instala_invoke(monkeypatch, lambda m, p, n: _resultado(m, ok=False))

    res = router.route(TaskClass.STANDARD, "p", workdir=tmp_path, max_escalations=2)
    assert res.ok is False
    assert len(chamadas) == 3

    evs = _eventos(trilha)
    assert [e["event"] for e in evs] == ["start", "attempt", "attempt", "attempt", "end"]
    assert [e["seq"] for e in evs] == [0, 1, 2, 3, 4]
    assert len({e["trajectory_id"] for e in evs}) == 1
    assert evs[-1]["outcome"] == "failed"
    assert evs[-1]["attempts"] == 3
    # Escalada é rastreável: cada tentativa sabe de quem herdou a vez.
    tentativas = [e for e in evs if e["event"] == "attempt"]
    assert "escalated_from" not in tentativas[0]
    assert tentativas[1]["escalated_from"] == tentativas[0]["model"]
    assert tentativas[2]["escalated_from"] == tentativas[1]["model"]


def test_backend_indisponivel_em_toda_a_cadeia_e_abandoned(trilha, tmp_path, monkeypatch):
    def indisponivel(model, prompt, n):
        raise backends.BackendUnavailable(f"{model.backend} não instalado")

    _instala_invoke(monkeypatch, indisponivel)

    with pytest.raises(router.NoBackendAvailable):
        router.route(TaskClass.STANDARD, "p", workdir=tmp_path, max_escalations=1)

    evs = _eventos(trilha)
    assert evs[-1]["event"] == "end"
    # Nenhum backend chegou a rodar: ausência de resultado, não resultado ruim.
    assert evs[-1]["outcome"] == "abandoned"
    assert evs[-1]["attempts"] == 2
    assert all(e["ok"] is False for e in evs if e["event"] == "attempt")


def test_outcome_distingue_os_tres_casos(trilha, tmp_path, monkeypatch):
    """`ok`, `failed` e `abandoned` na mesma trilha, sem se confundirem."""
    estado = {"modo": "ok"}

    def comporta(model, prompt, n):
        if estado["modo"] == "abandoned":
            raise backends.BackendUnavailable("sem binário")
        return _resultado(model, ok=estado["modo"] == "ok")

    _instala_invoke(monkeypatch, comporta)

    router.route(TaskClass.STANDARD, "p", workdir=tmp_path, max_escalations=0)
    estado["modo"] = "failed"
    router.route(TaskClass.STANDARD, "p", workdir=tmp_path, max_escalations=0)
    estado["modo"] = "abandoned"
    with pytest.raises(router.NoBackendAvailable):
        router.route(TaskClass.STANDARD, "p", workdir=tmp_path, max_escalations=0)

    fins = [e for e in _eventos(trilha) if e["event"] == "end"]
    assert sorted(e["outcome"] for e in fins) == ["abandoned", "failed", "ok"]
    assert len({e["trajectory_id"] for e in fins}) == 3


# --------------------------------------------------------------------------
# Identidade da trajetória
# --------------------------------------------------------------------------

def test_model_id_fixo_nao_escala_e_ainda_assim_registra(trilha, tmp_path, monkeypatch):
    chamadas = _instala_invoke(monkeypatch, lambda m, p, n: _resultado(m, ok=False))

    router.route(TaskClass.STANDARD, "p", workdir=tmp_path, model_id=MODELO_FIXO)

    assert chamadas == [MODELO_FIXO]
    evs = _eventos(trilha)
    assert [e["event"] for e in evs] == ["start", "attempt", "end"]
    # O `start` marca que o roteamento foi fixado à mão — é o que separa,
    # na análise, escalada real de comparação controlada.
    assert evs[0]["model"] == MODELO_FIXO
    assert evs[-1]["outcome"] == "failed"


def test_modelo_desconhecido_nao_abre_trajetoria(trilha, tmp_path, monkeypatch):
    _instala_invoke(monkeypatch, lambda m, p, n: _resultado(m, ok=True))
    with pytest.raises(router.NoBackendAvailable):
        router.route(TaskClass.STANDARD, "p", workdir=tmp_path, model_id="não existe")
    assert _eventos(trilha) == []


def test_trajectory_id_do_chamador_amarra_dois_estagios(trilha, tmp_path, monkeypatch):
    _instala_invoke(monkeypatch, lambda m, p, n: _resultado(m, ok=True))

    router.route(TaskClass.STANDARD, "extrai", workdir=tmp_path, trajectory_id="canib-42")
    router.route(TaskClass.STANDARD, "valida", workdir=tmp_path, trajectory_id="canib-42")

    grupos = audit.trajectories(trilha)
    assert list(grupos) == ["canib-42"]
    # Dois estágios na mesma trajetória: 2 × (start, attempt, end).
    assert [e["event"] for e in grupos["canib-42"]] == [
        "start", "attempt", "end", "start", "attempt", "end",
    ]
    # E o `seq` continua de onde parou — se recomeçasse do zero, a ordenação por
    # (trajectory_id, seq) intercalaria os dois estágios.
    assert [e["seq"] for e in grupos["canib-42"]] == [0, 1, 2, 3, 4, 5]


def test_trajetorias_distintas_por_padrao(trilha, tmp_path, monkeypatch):
    _instala_invoke(monkeypatch, lambda m, p, n: _resultado(m, ok=True))
    router.route(TaskClass.STANDARD, "a", workdir=tmp_path)
    router.route(TaskClass.STANDARD, "b", workdir=tmp_path)
    assert len(audit.trajectories(trilha)) == 2


# --------------------------------------------------------------------------
# Integridade e agregação de ponta a ponta
# --------------------------------------------------------------------------

def test_trilha_produzida_pelo_route_tem_cadeia_integra(trilha, tmp_path, monkeypatch):
    _instala_invoke(monkeypatch, lambda m, p, n: _resultado(m, ok=n >= 2))

    router.route(TaskClass.STANDARD, "p", workdir=tmp_path, max_escalations=2)
    router.route(TaskClass.MECHANICAL, "q", workdir=tmp_path)

    assert audit.verify_chain(trilha) == (True, None)

    s = audit.summary(trilha)
    assert s["trajectories"]["total"] == 2
    assert s["trajectories"]["by_outcome"]["ok"] == 2
    assert s["trajectories"]["success_rate"] == 1.0
    assert s["total_calls"] == s["trajectories"]["attempts_total"]


def test_proxies_de_tokens_sao_registrados(trilha, tmp_path, monkeypatch):
    _instala_invoke(monkeypatch, lambda m, p, n: _resultado(m, ok=True, texto="12345"))

    prompt = "x" * 321
    router.route(TaskClass.STANDARD, prompt, workdir=tmp_path)

    tentativa = [e for e in _eventos(trilha) if e["event"] == "attempt"][0]
    assert tentativa["prompt_chars"] == 321
    assert tentativa["output_chars"] == 5
    assert tentativa["latency_s"] == 1.5
    assert tentativa["exit_code"] == 0
    assert tentativa["pool"] in {p.value for p in Pool}
    assert tentativa["backend"] == BY_ID[tentativa["model"]].backend


def test_falha_de_trilha_nao_derruba_o_roteamento(tmp_path, monkeypatch):
    """A trilha existe para a tarefa, não a tarefa para a trilha."""
    bloqueado = tmp_path / "trilha-bloqueada.jsonl"
    bloqueado.mkdir()
    monkeypatch.setenv("FPCH_AUDIT_LOG", str(bloqueado))
    audit.reset_state()
    _instala_invoke(monkeypatch, lambda m, p, n: _resultado(m, ok=True))

    res = router.route(TaskClass.STANDARD, "p", workdir=tmp_path)

    assert res.ok is True
    assert audit.counters()["write_failures"] == 3  # start + attempt + end
