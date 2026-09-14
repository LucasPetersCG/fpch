"""C17 — o portão persistido de `cannibalize.run()` visto pela CLI.

O que se prende aqui: quando a fronteira do módulo recusa (registro divergente,
aprovação revogada no meio da execução, fila malformada), `fpch canib run` e
`fpch canib runall` terminam com código 1 e mensagem legível — sem traceback e
sem ficha. E o caminho de `accept` segue igual.

Isolamento: `QUEUE_PATH`, `DEFAULT_WORKDIR`, `DEFAULT_FICHE_DIR`, a trilha de
auditoria e a cascata da política apontam para `tmp_path`; `router.route` é
sempre um duplo. Nada aqui toca `~/.fpch/` nem backend real.
"""

from __future__ import annotations

import dataclasses
from pathlib import Path
from types import SimpleNamespace

import pytest

from fpch import cannibalize, cli, policy


@pytest.fixture(autouse=True)
def _isolado(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("FPCH_AUDIT_LOG", str(tmp_path / "audit.jsonl"))
    monkeypatch.delenv("FPCH_POLICY", raising=False)
    monkeypatch.setattr(policy, "_cwd", lambda: tmp_path)
    monkeypatch.setattr(policy, "_home", lambda: tmp_path / "home-inexistente")
    monkeypatch.setattr(cannibalize, "QUEUE_PATH", tmp_path / "fila" / "cannibalize.json")
    monkeypatch.setattr(cli, "DEFAULT_WORKDIR", tmp_path / "work")
    monkeypatch.setattr(cli, "DEFAULT_FICHE_DIR", tmp_path / "fichas")


def _fake_result(text: str) -> SimpleNamespace:
    return SimpleNamespace(
        ok=True, text=text, error=None, model="modelo-falso",
        pool=SimpleNamespace(value="teste"), latency_s=0.0,
    )


def _aprovado() -> cannibalize.Target:
    t = cannibalize.add("https://example.invalid/alvo-cli", note="nota sintética")
    cannibalize.set_status(t.id, cannibalize.STATUS_ACCEPTED)
    return t


def _route_que_rejeita(target_id: str):
    def _route(task, prompt, **kwargs):
        cannibalize.set_status(target_id, cannibalize.STATUS_REJECTED)
        return _fake_result("## O que é\nrelatório falso")
    return _route


def test_accept_continua_igual(capsys: pytest.CaptureFixture[str]) -> None:
    t = cannibalize.add("https://example.invalid/alvo-cli")

    assert cli.main(["canib", "accept", t.id]) == 0

    assert capsys.readouterr().out.strip() == f"{t.id} → {cannibalize.STATUS_ACCEPTED}"


def test_canib_run_com_rejeicao_durante_execucao_sai_1(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    t = _aprovado()
    monkeypatch.setattr(cannibalize.router, "route", _route_que_rejeita(t.id))

    assert cli.main(["canib", "run", t.id]) == 1

    err = capsys.readouterr().err
    assert "erro:" in err
    assert "durante a execução" in err
    assert "Traceback" not in err
    assert not (tmp_path / "fichas").exists()


def test_canib_run_com_copia_divergente_sai_1(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Simula a corrida entre a leitura da CLI e a entrada de `run()`: a CLI
    entrega um objeto que já não coincide com a fila."""
    t = _aprovado()
    divergente = dataclasses.replace(t, status=cannibalize.STATUS_ACCEPTED,
                                     note="VALOR-NAO-PODE-ECOAR")
    monkeypatch.setattr(cannibalize, "listing", lambda status=None: [divergente])

    def _boom(*a, **k):
        raise AssertionError("backend não podia ser invocado")

    monkeypatch.setattr(cannibalize.router, "route", _boom)

    assert cli.main(["canib", "run", t.id]) == 1

    err = capsys.readouterr().err
    assert "erro:" in err and "note" in err
    assert "VALOR-NAO-PODE-ECOAR" not in err
    assert f"fpch canib accept {t.id}" in err


def test_canib_runall_com_rejeicao_durante_execucao_sai_1(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    t = _aprovado()
    monkeypatch.setattr(cannibalize.router, "route", _route_que_rejeita(t.id))

    assert cli.main(["canib", "runall"]) == 1

    err = capsys.readouterr().err
    assert "FALHOU" in err and "Traceback" not in err
    assert not (tmp_path / "fichas").exists()


def test_add_imprime_alvo_e_dica_de_aprovacao(capsys: pytest.CaptureFixture[str]) -> None:
    assert cli.main(["canib", "add", "https://github.com/exemplo/alvo", "--note", "n"]) == 0

    out = capsys.readouterr().out
    [t] = cannibalize.listing()
    assert t.status == cannibalize.STATUS_PENDING and t.kind == "repo"
    assert out.splitlines()[0].startswith(f"{t.id}  [pending]  conceito")
    assert f"aprove com:  fpch canib accept {t.id}" in out


def test_add_repetido_devolve_o_mesmo_alvo(capsys: pytest.CaptureFixture[str]) -> None:
    assert cli.main(["canib", "add", "https://example.invalid/x"]) == 0
    assert cli.main(["canib", "add", "https://example.invalid/x"]) == 0

    assert len(cannibalize.listing()) == 1


def test_reject_grava_status_e_sai_0(capsys: pytest.CaptureFixture[str]) -> None:
    t = _aprovado()

    assert cli.main(["canib", "reject", t.id]) == 0

    assert capsys.readouterr().out.strip() == f"{t.id} → {cannibalize.STATUS_REJECTED}"
    assert cannibalize.listing()[0].status == cannibalize.STATUS_REJECTED


@pytest.mark.parametrize("subcomando", ["accept", "reject"])
def test_accept_reject_id_inexistente_sai_1(
    capsys: pytest.CaptureFixture[str], subcomando: str
) -> None:
    assert cli.main(["canib", subcomando, "00000000"]) == 1

    assert "id não encontrado: 00000000" in capsys.readouterr().err


def test_list_fila_vazia_sai_0(capsys: pytest.CaptureFixture[str]) -> None:
    assert cli.main(["canib", "list"]) == 0

    assert capsys.readouterr().out.strip() == "fila vazia."


def test_list_com_fila_valida_e_filtro_de_status(capsys: pytest.CaptureFixture[str]) -> None:
    pendente = cannibalize.add("https://example.invalid/pendente", note="nota p")
    aprovado = _aprovado()
    capsys.readouterr()

    assert cli.main(["canib", "list"]) == 0
    out = capsys.readouterr().out
    assert f"{pendente.id}  pending" in out and "— nota p" in out
    assert f"{aprovado.id}  accepted" in out

    assert cli.main(["canib", "list", "--status", "accepted"]) == 0
    out = capsys.readouterr().out
    assert aprovado.id in out and pendente.id not in out

    assert cli.main(["canib", "list", "--status", "done"]) == 0
    assert capsys.readouterr().out.strip() == "fila vazia."


@pytest.mark.parametrize("subcomando", [["run", "abc"], ["runall"], ["list"]])
def test_fila_malformada_sai_1_sem_traceback(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], subcomando: list[str]
) -> None:
    cannibalize.QUEUE_PATH.parent.mkdir(parents=True, exist_ok=True)
    cannibalize.QUEUE_PATH.write_text('{"topo": "objeto"}', encoding="utf-8")

    assert cli.main(["canib", *subcomando]) == 1

    err = capsys.readouterr().err
    assert err.startswith("erro: fila malformada")
