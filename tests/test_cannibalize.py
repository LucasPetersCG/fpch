"""Testes do portão de aprovação na fronteira do módulo de canibalização.

Por que este arquivo existe: o portão de aprovação humana nasceu na CLI, e ali
protegia apenas quem entrava pela CLI. Qualquer chamador programático — um teste,
um script, um agente com `--allow-write` — chegava a `cannibalize.run()` sem
passar por barreira alguma. Uma invariante de segurança que reside na *interface*
não é invariante: é convenção, e convenção só vale para quem a conhece.

O que estes testes prendem é justamente o que a convenção não prendia: que a
recusa acontece na ENTRADA de `run()`, **antes** de qualquer invocação de backend.
Testar só que "levanta exceção" seria fraco — uma implementação que roteia o
prompt e depois reclama passaria no teste e teria já entregado conteúdo hostil a
um modelo. Por isso o duplo do roteador não devolve resposta falsa: ele *falha o
teste* se for tocado.

Nenhum teste aqui invoca CLI de terceiro ou rede — `router.route` é sempre
substituído por monkeypatch antes de `run()` ser chamada.

Rodar: `uv run --with pytest pytest tests/ -q`
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from fpch import cannibalize  # noqa: E402


class BackendWasInvoked(AssertionError):
    """Sinaliza que o roteador foi chamado quando não devia ter sido."""


def _forbidden_route(monkeypatch: pytest.MonkeyPatch) -> None:
    """Substitui `router.route` por um duplo que reprova o teste se for chamado.

    É a prova de que a recusa é *anterior* ao backend, e não um arrependimento
    depois de o prompt já ter viajado.
    """

    def _boom(*args, **kwargs):
        raise BackendWasInvoked(
            "router.route foi invocado para um alvo não aprovado — "
            "o portão não está na entrada de run()"
        )

    monkeypatch.setattr(cannibalize.router, "route", _boom)


def _target(status: str) -> cannibalize.Target:
    return cannibalize.Target(
        url="https://example.invalid/alvo-de-teste",
        kind="repo",
        note="alvo sintético; nada aqui toca a rede",
        status=status,
    )


# --- (a) alvo pendente é recusado antes de qualquer backend ------------------


def test_alvo_pendente_e_recusado_sem_tocar_backend(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _forbidden_route(monkeypatch)
    target = _target(cannibalize.STATUS_PENDING)

    with pytest.raises(cannibalize.TargetNotApprovedError) as exc:
        cannibalize.run(
            target,
            workdir=tmp_path / "work",
            fiche_dir=tmp_path / "fichas",
        )

    assert exc.value.target_id == target.id
    assert exc.value.status == cannibalize.STATUS_PENDING


def test_recusa_de_pendente_nao_escreve_ficha(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A recusa não deixa efeito colateral: nenhum diretório, nenhuma ficha."""
    _forbidden_route(monkeypatch)
    fiche_dir = tmp_path / "fichas"

    with pytest.raises(cannibalize.TargetNotApprovedError):
        cannibalize.run(
            _target(cannibalize.STATUS_PENDING),
            workdir=tmp_path / "work",
            fiche_dir=fiche_dir,
        )

    assert not fiche_dir.exists()


def test_mensagem_diz_id_status_e_como_aprovar() -> None:
    """A mensagem tem de ser acionável — id, status atual e o comando de aprovação."""
    target = _target(cannibalize.STATUS_PENDING)
    msg = str(cannibalize.TargetNotApprovedError(target.id, target.status))

    assert target.id in msg
    assert cannibalize.STATUS_PENDING in msg
    assert f"fpch canib accept {target.id}" in msg


def test_excecao_e_subclasse_de_runtimeerror() -> None:
    """A CLI já captura `RuntimeError` em `canib run`; o portão degrada para
    mensagem legível sem que `cli.py` precise mudar."""
    assert issubclass(cannibalize.TargetNotApprovedError, RuntimeError)


# --- (b) alvo rejeitado é recusado ------------------------------------------


def test_alvo_rejeitado_e_recusado(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _forbidden_route(monkeypatch)
    target = _target(cannibalize.STATUS_REJECTED)

    with pytest.raises(cannibalize.TargetNotApprovedError) as exc:
        cannibalize.run(
            target,
            workdir=tmp_path / "work",
            fiche_dir=tmp_path / "fichas",
        )

    assert exc.value.status == cannibalize.STATUS_REJECTED


def test_alvo_ja_concluido_e_recusado(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`done` também não é `accepted`. O portão confere aprovação, não
    "qualquer status que não seja pendente" — reexecutar um alvo concluído
    exige nova aprovação explícita."""
    _forbidden_route(monkeypatch)

    with pytest.raises(cannibalize.TargetNotApprovedError):
        cannibalize.run(
            _target(cannibalize.STATUS_DONE),
            workdir=tmp_path / "work",
            fiche_dir=tmp_path / "fichas",
        )


# --- (c) alvo aprovado passa do portão --------------------------------------


class _GateOpened(Exception):
    """Sentinela: só é levantada se a execução chegou ao roteador."""


def test_alvo_aprovado_passa_do_portao(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Prova o outro lado do portão: com `accepted`, `run()` segue adiante e
    chega ao roteador.

    O duplo levanta uma sentinela em vez de devolver um `Result` falso — o que
    está sob teste aqui é o portão, não o pipeline de duas etapas. Deixar a
    execução seguir exigiria simular extração, proposta, validação de citações e
    escrita da ficha, e um teste que simula tudo isso não falha mais por causa do
    portão.
    """
    calls: list[tuple] = []

    def _sentinel(task, prompt, **kwargs):
        calls.append((task, kwargs.get("label")))
        raise _GateOpened

    monkeypatch.setattr(cannibalize.router, "route", _sentinel)
    target = _target(cannibalize.STATUS_ACCEPTED)

    with pytest.raises(_GateOpened):
        cannibalize.run(
            target,
            workdir=tmp_path / "work",
            fiche_dir=tmp_path / "fichas",
        )

    assert len(calls) == 1, "o portão devia ter deixado passar exatamente uma rota"
    assert calls[0][1] == f"cannibalize:extract:{target.id}"


def test_status_accepted_e_o_unico_que_passa(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Varre todos os status declarados no módulo: só `accepted` passa."""
    todos = [
        cannibalize.STATUS_PENDING,
        cannibalize.STATUS_ACCEPTED,
        cannibalize.STATUS_REJECTED,
        cannibalize.STATUS_DONE,
    ]

    def _sentinel(*args, **kwargs):
        raise _GateOpened

    monkeypatch.setattr(cannibalize.router, "route", _sentinel)

    passaram = []
    for status in todos:
        try:
            cannibalize.run(
                _target(status),
                workdir=tmp_path / "work",
                fiche_dir=tmp_path / "fichas",
            )
        except _GateOpened:
            passaram.append(status)
        except cannibalize.TargetNotApprovedError:
            pass

    assert passaram == [cannibalize.STATUS_ACCEPTED]
