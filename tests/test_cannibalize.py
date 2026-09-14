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

Segunda camada (C17): o portão confere o REGISTRO PERSISTIDO na fila, não o
objeto em memória. Um `Target(status="accepted")` forjado, uma cópia feita com
`dataclasses.replace`, uma fonte trocada depois da aprovação ou uma rejeição no
meio da execução não podem produzir ficha.

Nenhum teste aqui invoca CLI de terceiro ou rede — `router.route` é sempre
substituído por monkeypatch antes de `run()` ser chamada — e nenhum toca a fila
real do autor: `QUEUE_PATH` aponta para `tmp_path` em todos eles (fixture
automática abaixo).

Rodar: `uv run --with pytest pytest tests/ -q`
"""

from __future__ import annotations

import dataclasses
import json
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from fpch import cannibalize  # noqa: E402


@pytest.fixture(autouse=True)
def _fila_isolada(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Nenhum teste desta suíte lê ou escreve `~/.fpch/cannibalize.json`."""
    fila = tmp_path / "fila" / "cannibalize.json"
    monkeypatch.setattr(cannibalize, "QUEUE_PATH", fila)
    return fila


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


def _target(status: str, **kwargs) -> cannibalize.Target:
    campos = dict(
        url="https://example.invalid/alvo-de-teste",
        kind="repo",
        note="alvo sintético; nada aqui toca a rede",
        status=status,
    )
    campos.update(kwargs)
    return cannibalize.Target(**campos)


def _persisted(status: str, **kwargs) -> cannibalize.Target:
    """Cria um alvo e o grava na fila isolada, como `add` + `accept` fariam."""
    target = _target(status, **kwargs)
    cannibalize._save([*cannibalize._load(), target])
    return target


def _stored(target_id: str) -> cannibalize.Target:
    [t] = [t for t in cannibalize._load() if t.id == target_id]
    return t


def _fake_result(text: str) -> SimpleNamespace:
    """Resultado mínimo que `run()` e `_render` consomem — nada de backend real."""
    return SimpleNamespace(
        ok=True,
        text=text,
        error=None,
        model="modelo-falso",
        pool=SimpleNamespace(value="teste"),
        latency_s=0.0,
    )


def _run(target: cannibalize.Target, tmp_path: Path) -> Path:
    return cannibalize.run(
        target,
        workdir=tmp_path / "work",
        fiche_dir=tmp_path / "fichas",
    )


# --- (a) alvo pendente é recusado antes de qualquer backend ------------------


def test_alvo_pendente_e_recusado_sem_tocar_backend(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _forbidden_route(monkeypatch)
    target = _persisted(cannibalize.STATUS_PENDING)

    with pytest.raises(cannibalize.TargetNotApprovedError) as exc:
        _run(target, tmp_path)

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
            _persisted(cannibalize.STATUS_PENDING),
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
    assert issubclass(
        cannibalize.TargetApprovalMismatchError, cannibalize.TargetNotApprovedError
    )


# --- (b) alvo rejeitado é recusado ------------------------------------------


def test_alvo_rejeitado_e_recusado(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _forbidden_route(monkeypatch)
    target = _persisted(cannibalize.STATUS_REJECTED)

    with pytest.raises(cannibalize.TargetNotApprovedError) as exc:
        _run(target, tmp_path)

    assert exc.value.status == cannibalize.STATUS_REJECTED


def test_alvo_ja_concluido_e_recusado(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`done` também não é `accepted`. O portão confere aprovação, não
    "qualquer status que não seja pendente" — reexecutar um alvo concluído
    exige nova aprovação explícita."""
    _forbidden_route(monkeypatch)

    with pytest.raises(cannibalize.TargetNotApprovedError):
        _run(_persisted(cannibalize.STATUS_DONE), tmp_path)


# --- (c) alvo aprovado passa do portão --------------------------------------


class _GateOpened(Exception):
    """Sentinela: só é levantada se a execução chegou ao roteador."""


def test_alvo_aprovado_passa_do_portao(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Prova o outro lado do portão: com `accepted`, `run()` segue adiante e
    chega ao roteador.

    O duplo levanta uma sentinela em vez de devolver um `Result` falso — o que
    está sob teste aqui é o portão, não o pipeline de duas etapas.
    """
    calls: list[tuple] = []

    def _sentinel(task, prompt, **kwargs):
        calls.append((task, kwargs.get("label")))
        raise _GateOpened

    monkeypatch.setattr(cannibalize.router, "route", _sentinel)
    target = _persisted(cannibalize.STATUS_ACCEPTED)

    with pytest.raises(_GateOpened):
        _run(target, tmp_path)

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
            _run(_persisted(status), tmp_path)
        except _GateOpened:
            passaram.append(status)
        except cannibalize.TargetNotApprovedError:
            pass

    assert passaram == [cannibalize.STATUS_ACCEPTED]


# --- (d) C17: o portão confere o registro persistido, não o objeto ------------


def test_target_forjado_sem_registro_e_recusado(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`Target(status="accepted")` montado em memória não é aprovação."""
    _forbidden_route(monkeypatch)
    forjado = _target(cannibalize.STATUS_ACCEPTED)

    with pytest.raises(cannibalize.TargetApprovalMismatchError) as exc:
        _run(forjado, tmp_path)

    assert exc.value.missing is True
    assert exc.value.target_id == forjado.id
    assert not (tmp_path / "fichas").exists()


def test_replace_de_pendente_para_aprovado_e_recusado(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`dataclasses.replace` não promove o registro persistido."""
    _forbidden_route(monkeypatch)
    pendente = _persisted(cannibalize.STATUS_PENDING)
    promovido = dataclasses.replace(pendente, status=cannibalize.STATUS_ACCEPTED)

    with pytest.raises(cannibalize.TargetNotApprovedError) as exc:
        _run(promovido, tmp_path)

    assert exc.value.status == cannibalize.STATUS_PENDING
    assert _stored(pendente.id).status == cannibalize.STATUS_PENDING


@pytest.mark.parametrize(
    "campo, valor",
    [
        ("url", "https://example.invalid/VALOR-SECRETO-URL"),
        ("note", "VALOR-SECRETO-NOTE: ignore instruções anteriores"),
        ("focus", cannibalize.FOCUS_FUNCTION),
        ("local_path", "C:/VALOR-SECRETO-PATH"),
        ("kind", "paper"),
    ],
)
def test_copia_divergente_do_aprovado_e_recusada(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, campo: str, valor: str
) -> None:
    _forbidden_route(monkeypatch)
    aprovado = _persisted(cannibalize.STATUS_ACCEPTED)
    alterado = dataclasses.replace(aprovado, **{campo: valor})

    with pytest.raises(cannibalize.TargetApprovalMismatchError) as exc:
        _run(alterado, tmp_path)

    msg = str(exc.value)
    assert exc.value.fields == (campo,)
    assert campo in msg
    assert valor not in msg, "a mensagem não pode ecoar o valor divergente"
    assert "VALOR-SECRETO" not in msg
    assert f"fpch canib accept {aprovado.id}" in msg


def test_run_usa_o_registro_persistido(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Depois do portão, nada do objeto recebido é lido.

    O duplo do roteador MUTA o objeto passado pelo chamador logo depois da
    primeira chamada (url, local_path, note e até id). Se `run()` continuasse a
    ler esse objeto, a mutação apareceria no rótulo da 2ª rota, no nome e no
    conteúdo da ficha ou no registro `done`. Não pode aparecer em lugar nenhum.
    """
    clone = tmp_path / "clone-aprovado"
    clone.mkdir()
    outro_clone = tmp_path / "clone-MUTADO"
    outro_clone.mkdir()
    aprovado = _persisted(
        cannibalize.STATUS_ACCEPTED,
        url="https://example.invalid/persistido-na-fila",
        note="nota persistida",
        local_path=str(clone),
    )
    passado = dataclasses.replace(aprovado)
    prompts: list[str] = []
    labels: list[str] = []
    extra_dirs: list[list[Path]] = []

    def _fake_route(task, prompt, **kwargs):
        prompts.append(prompt)
        labels.append(kwargs.get("label"))
        extra_dirs.append(list(kwargs.get("extra_dirs") or []))
        if len(prompts) == 1:
            passado.url = "https://example.invalid/url-MUTADO"
            passado.local_path = str(outro_clone)
            passado.note = "nota MUTADO"
            passado.id = "mutado00"
        return _fake_result("## O que é\nrelatório falso")

    monkeypatch.setattr(cannibalize.router, "route", _fake_route)

    path = _run(passado, tmp_path)

    assert len(prompts) == 2
    assert "https://example.invalid/persistido-na-fila" in prompts[0]
    assert str(clone) in prompts[0] and "nota persistida" in prompts[0]
    assert extra_dirs[0] == [clone]
    assert "MUTADO" not in prompts[1]
    assert labels == [
        f"cannibalize:extract:{aprovado.id}",
        f"cannibalize:propose:{aprovado.id}",
    ]
    assert path.name == "CANIB-conceito-persistido-na-fila.md"
    ficha = path.read_text(encoding="utf-8")
    assert "https://example.invalid/persistido-na-fila" in ficha
    assert str(clone) in ficha and "nota persistida" in ficha
    assert "MUTADO" not in ficha and "mutado00" not in ficha
    registro = _stored(aprovado.id)
    assert registro.status == cannibalize.STATUS_DONE
    assert registro.fiche_path == str(path)
    assert (registro.url, registro.note, registro.local_path) == (
        aprovado.url, aprovado.note, aprovado.local_path,
    )
    assert [t.id for t in cannibalize._load()] == [aprovado.id]


# --- (e) C17: mudar a fonte depois da aprovação revoga a aprovação ----------


def test_set_local_path_novo_valor_revoga_aprovacao() -> None:
    aprovado = _persisted(cannibalize.STATUS_ACCEPTED)

    t = cannibalize.set_local_path(aprovado.id, "C:/clone/novo")

    assert t is not None and t.status == cannibalize.STATUS_PENDING
    registro = _stored(aprovado.id)
    assert registro.status == cannibalize.STATUS_PENDING
    assert registro.local_path == "C:/clone/novo"
    assert f"fpch canib accept {aprovado.id}" in cannibalize.reapproval_hint(aprovado.id)


def test_set_local_path_mesmo_valor_mantem_aprovacao() -> None:
    aprovado = _persisted(cannibalize.STATUS_ACCEPTED, local_path="C:/clone/igual")

    t = cannibalize.set_local_path(aprovado.id, "C:/clone/igual")

    assert t is not None and t.status == cannibalize.STATUS_ACCEPTED
    assert _stored(aprovado.id).status == cannibalize.STATUS_ACCEPTED


def test_add_preenchendo_local_path_revoga_aprovacao() -> None:
    aprovado = _persisted(cannibalize.STATUS_ACCEPTED)

    t = cannibalize.add(aprovado.url, focus=aprovado.focus, local_path="C:/clone/depois")

    assert t.id == aprovado.id
    assert t.status == cannibalize.STATUS_PENDING
    assert _stored(aprovado.id).status == cannibalize.STATUS_PENDING


def test_local_path_trocado_bloqueia_run_ate_nova_aprovacao(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _forbidden_route(monkeypatch)
    aprovado = _persisted(cannibalize.STATUS_ACCEPTED)
    t = cannibalize.set_local_path(aprovado.id, "C:/clone/trocado")

    with pytest.raises(cannibalize.TargetNotApprovedError) as exc:
        _run(t, tmp_path)

    assert exc.value.status == cannibalize.STATUS_PENDING


# --- (f) C17: revogação durante a execução (TOCTOU) --------------------------


@pytest.mark.parametrize("mudanca", ["rejeitar", "trocar_fonte"])
def test_mudanca_durante_execucao_nao_gera_ficha_nem_done(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, mudanca: str
) -> None:
    aprovado = _persisted(cannibalize.STATUS_ACCEPTED)
    chamadas: list[str] = []

    def _route_que_muda_a_fila(task, prompt, **kwargs):
        chamadas.append(kwargs.get("label", ""))
        if len(chamadas) == 1:
            if mudanca == "rejeitar":
                cannibalize.set_status(aprovado.id, cannibalize.STATUS_REJECTED)
            else:
                cannibalize.set_local_path(aprovado.id, "C:/clone/no-meio")
        return _fake_result("## O que é\nrelatório falso")

    monkeypatch.setattr(cannibalize.router, "route", _route_que_muda_a_fila)

    with pytest.raises(cannibalize.TargetApprovalMismatchError) as exc:
        _run(aprovado, tmp_path)

    assert "durante a execução" in str(exc.value)
    assert not (tmp_path / "fichas").exists(), "nenhuma ficha pode ser escrita"
    registro = _stored(aprovado.id)
    assert registro.status != cannibalize.STATUS_DONE
    assert registro.fiche_path is None


# --- (g) C17: fila malformada falha fechado ----------------------------------


@pytest.mark.parametrize(
    "conteudo, motivo",
    [
        ("{não é json", "ilegível"),
        (json.dumps({"url": "https://example.invalid"}), "o topo deve ser uma lista"),
        (json.dumps(["texto solto"]), "esperado objeto"),
        (json.dumps([{"url": "u", "kind": "repo", "id": "abc", "chave_estranha": 1}]),
         "chaves desconhecidas: chave_estranha"),
        (json.dumps([{"url": "u", "kind": "repo"}]), "chaves obrigatórias ausentes: id"),
        (json.dumps([{"url": "u", "kind": "repo", "id": "abc", "status": ["accepted"]}]),
         "campo 'status'"),
        (json.dumps([{"url": "u", "kind": "repo", "id": 123, "status": "accepted"}]),
         "campo 'id'"),
    ],
    ids=["json-invalido", "topo-objeto", "item-nao-objeto", "chave-desconhecida",
         "sem-id", "status-lista", "id-numerico"],
)
def test_fila_malformada_falha_fechado(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    _fila_isolada: Path,
    conteudo: str,
    motivo: str,
) -> None:
    _forbidden_route(monkeypatch)
    _fila_isolada.parent.mkdir(parents=True, exist_ok=True)
    _fila_isolada.write_text(conteudo, encoding="utf-8")

    with pytest.raises(cannibalize.QueueFormatError) as exc:
        _run(_target(cannibalize.STATUS_ACCEPTED, id="abc"), tmp_path)

    assert str(cannibalize.QUEUE_PATH) in str(exc.value)
    assert motivo in str(exc.value)
    assert not (tmp_path / "fichas").exists()


@pytest.mark.parametrize("ordem", ["aprovado-primeiro", "pendente-primeiro"])
def test_id_duplicado_na_fila_e_recusado(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, ordem: str
) -> None:
    """Dois registros com o mesmo id: ambíguo, logo não aprovado — em qualquer ordem."""
    _forbidden_route(monkeypatch)
    aprovado = _target(cannibalize.STATUS_ACCEPTED)
    pendente = dataclasses.replace(aprovado, status=cannibalize.STATUS_PENDING)
    fila = [aprovado, pendente] if ordem == "aprovado-primeiro" else [pendente, aprovado]
    cannibalize._save(fila)

    with pytest.raises(cannibalize.TargetApprovalMismatchError) as exc:
        _run(aprovado, tmp_path)

    assert "mais de uma vez" in str(exc.value)
    assert exc.value.missing is False
    assert not (tmp_path / "fichas").exists()


# --- (h) fonte local aprovada ausente falha fechado --------------------------


@pytest.mark.parametrize("situacao", ["inexistente", "arquivo"])
def test_local_path_aprovado_ausente_recusa_sem_backend(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, situacao: str
) -> None:
    """Sem o diretório aprovado, não há recuo silencioso para a URL."""
    _forbidden_route(monkeypatch)
    caminho = tmp_path / "clone-sumiu"
    if situacao == "arquivo":
        caminho.write_text("não sou diretório", encoding="utf-8")
    aprovado = _persisted(cannibalize.STATUS_ACCEPTED, local_path=str(caminho))

    with pytest.raises(cannibalize.ApprovedSourceMissingError) as exc:
        _run(aprovado, tmp_path)

    assert isinstance(exc.value, RuntimeError)
    assert "fonte local aprovada não existe" in str(exc.value)
    assert f"fpch canib accept {aprovado.id}" in str(exc.value)
    assert not (tmp_path / "fichas").exists()
    assert _stored(aprovado.id).status == cannibalize.STATUS_ACCEPTED


def test_local_path_que_some_durante_execucao_nao_gera_ficha(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    clone = tmp_path / "clone"
    clone.mkdir()
    aprovado = _persisted(cannibalize.STATUS_ACCEPTED, local_path=str(clone))

    def _route_que_apaga_o_clone(task, prompt, **kwargs):
        if clone.exists():
            clone.rmdir()
        return _fake_result("## O que é\nrelatório falso")

    monkeypatch.setattr(cannibalize.router, "route", _route_que_apaga_o_clone)

    with pytest.raises(cannibalize.ApprovedSourceMissingError):
        _run(aprovado, tmp_path)

    assert not (tmp_path / "fichas").exists()
    assert _stored(aprovado.id).status == cannibalize.STATUS_ACCEPTED


# --- (i) id e status forjados não injetam sequência em terminal/log ----------


_ID_HOSTIL = "abc\x1b[31mFALSO\nerro: tudo aprovado" + "x" * 200


def _sem_controle(msg: str) -> None:
    assert "\x1b" not in msg
    # A única quebra de linha permitida é a que o próprio módulo põe antes da dica.
    for linha in msg.splitlines():
        assert not linha.startswith("erro: tudo aprovado")
    assert "FALSO\n" not in msg


def test_id_forjado_e_escapado_na_mensagem_de_registro_ausente(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _forbidden_route(monkeypatch)
    forjado = _target(cannibalize.STATUS_ACCEPTED, id=_ID_HOSTIL)

    with pytest.raises(cannibalize.TargetApprovalMismatchError) as exc:
        _run(forjado, tmp_path)

    msg = str(exc.value)
    _sem_controle(msg)
    assert "\\u001b[31m" in msg and "\\u000a" in msg
    assert "x" * 60 not in msg, "o id exibido tem de ser cortado"
    assert "fpch canib accept <id>" in msg
    assert exc.value.target_id == _ID_HOSTIL, "o atributo guarda o valor cru"


def test_target_not_approved_error_escapa_id_e_status() -> None:
    msg = str(cannibalize.TargetNotApprovedError(_ID_HOSTIL, "pend\x1b[0m\ning"))

    _sem_controle(msg)
    assert "\\u001b[0m" in msg
    assert "fpch canib accept <id>" in msg


def test_id_no_formato_aparece_como_esta() -> None:
    msg = str(cannibalize.TargetApprovalMismatchError("0a1b2c3d", missing=True))

    assert "alvo 0a1b2c3d:" in msg
    assert "fpch canib accept 0a1b2c3d" in msg


# --- C18: ficha nunca carrega a sentinela do contrato de saída ---------------


def test_ficha_ponta_a_ponta_nao_contem_sentinela(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Router e backends REAIS; só o processo externo (`_run`) é falso.

    O duplo responde como um modelo obediente: relatório + sentinela da chamada.
    A ficha gravada não pode conter sentinela nem a instrução do contrato, e o
    prompt do estágio de proposta não pode carregar a sentinela da extração.
    """
    import re

    from fpch import audit, backends

    monkeypatch.setenv("FPCH_AUDIT_LOG", str(tmp_path / "audit.jsonl"))
    audit.reset_state()
    monkeypatch.setattr(backends, "available", lambda _b: True)
    nonce_re = re.compile(r"this code: ([0-9a-f]{16})")
    prompts: list[str] = []
    sentinelas: list[str] = []

    def fake_run(argv, timeout_s, cwd=None):
        prompt = argv[argv.index("-p") + 1]
        prompts.append(prompt)
        [nonce] = set(nonce_re.findall(prompt))
        sentinela = f"FPCH-FIM-{nonce}"
        sentinelas.append(sentinela)
        corpo = (
            f"## Estágio {len(prompts)}\n"
            f"O teste cita `{LITERAL_DO_REPO}` e a variante `{sentinela}0` (17 hex).\n"
            "conteúdo gerado longo o bastante para ser trabalho real"
        )
        return 0, f"{corpo}\r\n\r\n**{sentinela}**\r\n"

    monkeypatch.setattr(backends, "_run", fake_run)

    aprovado = _persisted(cannibalize.STATUS_ACCEPTED)
    path = _run(aprovado, tmp_path)
    audit.reset_state()

    ficha = path.read_text(encoding="utf-8")
    assert len(prompts) == 2
    assert "## Estágio 1" in ficha and "## Estágio 2" in ficha
    assert "OUTPUT CONTRACT" not in ficha
    for sentinela in sentinelas:
        # A sentinela real some; a variante de 17 hex é conteúdo e fica.
        assert re.search(rf"{sentinela}(?![0-9a-f])", ficha) is None
        assert f"{sentinela}0" in ficha
    # Literal com formato de sentinela citado do próprio repositório: preservado.
    assert ficha.count(LITERAL_DO_REPO) == 2
    # A extração alimenta a proposta sem levar a sentinela do 1º estágio.
    assert re.search(rf"{sentinelas[0]}(?![0-9a-f])", prompts[1]) is None


LITERAL_DO_REPO = "FPCH-FIM-0123456789abcdef"


def test_render_nao_apaga_texto_com_formato_de_sentinela() -> None:
    """Regra menos destrutiva: `_render` não limpa nada.

    Só `backends.invoke` conhece o nonce, e só ele remove sentinela. Um relatório
    sobre o próprio FPCH cita `FPCH-FIM-0123456789abcdef` dos testes; apagar isso
    na ficha a faria mentir sobre a fonte.
    """
    texto = f"relatório cita {LITERAL_DO_REPO} e {LITERAL_DO_REPO}0 (17 hex)"
    conteudo = cannibalize._render(
        _target(cannibalize.STATUS_ACCEPTED), _fake_result(texto), _fake_result(texto)
    )
    assert conteudo.count(f"relatório cita {LITERAL_DO_REPO} e {LITERAL_DO_REPO}0") == 2
