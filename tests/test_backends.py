"""Testes da invocação de backends.

Primeira cobertura deste módulo — até 31/08/2026 tinha zero, e era exatamente
onde moravam três defeitos de contenção que nenhum teste prendia: `allow_write`
inerte no `agy`, o backend `claude` rodando sem contenção nenhuma, e `extra_dirs`
honrado por um só adaptador.

O que estes testes prendem não é a forma do `argv`. É a única promessa que o
módulo faz e que, se for falsa, contamina todo o resto: **nenhum adaptador
executa concedendo mais do que se pediu.** Ou a contenção aparece no `argv`, ou a
chamada é recusada — e a recusa é `BackendUnavailable`, para que o router escale.

NENHUM teste invoca processo externo: `_run` e `shutil.which` são monkeypatched.

Rodar: `uv run --with pytest pytest tests/ -q`
"""

from __future__ import annotations

import subprocess

import pytest

from fpch import backends
from fpch.models import Model, Pool, TaskClass


# ---------------------------------------------------------------------------
# Instrumentação — nada aqui sai para o sistema operacional
# ---------------------------------------------------------------------------

BACKENDS = sorted(backends.ADAPTERS)


def _model(backend: str) -> Model:
    return Model(
        id="modelo-de-teste",
        backend=backend,
        pool=Pool.LOCAL,
        power=3,
        cost=1,
        good_for=(TaskClass.STANDARD,),
    )


@pytest.fixture
def capturado(monkeypatch):
    """Substitui `_run` por captura. Devolve a lista de chamadas feitas."""
    chamadas: list[dict] = []

    def fake_run(argv, timeout_s, cwd=None):
        chamadas.append({"argv": argv, "timeout_s": timeout_s, "cwd": cwd})
        return 0, "resposta longa o bastante para nao disparar o limiar de 40 chars"

    monkeypatch.setattr(backends, "_run", fake_run)
    monkeypatch.setattr(backends.shutil, "which", lambda b: f"/fake/bin/{b}")
    return chamadas


def _argv(capturado) -> list[str]:
    assert len(capturado) == 1, "esperava exatamente uma invocação"
    return capturado[0]["argv"]


def _pares(argv: list[str], flag: str) -> list[str]:
    """Valores que seguem cada ocorrência de `flag` no argv."""
    return [argv[i + 1] for i, tok in enumerate(argv) if tok == flag and i + 1 < len(argv)]


# ---------------------------------------------------------------------------
# Contrato de argv — um por adaptador, caso simples
# ---------------------------------------------------------------------------


def test_argv_agy_caso_simples(tmp_path, capturado):
    backends.invoke(_model("agy"), "oi", workdir=tmp_path)
    argv = _argv(capturado)
    assert argv[0] == "agy"
    assert argv[:5] == ["agy", "--model", "modelo-de-teste", "-p", "oi"]
    assert "--sandbox" in argv


def test_argv_copilot_caso_simples(tmp_path, capturado):
    backends.invoke(_model("copilot"), "oi", workdir=tmp_path)
    argv = _argv(capturado)
    assert argv[0] == "copilot"
    assert _pares(argv, "-C") == [str(tmp_path)]
    assert argv[-2:] == ["-p", "oi"]


def test_argv_claude_caso_simples(tmp_path, capturado):
    backends.invoke(_model("claude"), "oi", workdir=tmp_path)
    argv = _argv(capturado)
    assert argv[0] == "claude"
    # O prompt do `claude` é POSICIONAL depois de `-p`, e as opções variádicas
    # (`--disallowedTools`, `--add-dir`) engolem valor solto que venha depois.
    # Por isso `-p <prompt>` tem de ser o final do argv.
    assert argv[-2:] == ["-p", "oi"]


@pytest.mark.parametrize("backend", BACKENDS)
def test_argv_e_sempre_lista_sem_shell(tmp_path, capturado, backend):
    """`argv` é lista de strings — nunca string interpolada em shell."""
    backends.invoke(_model(backend), "oi", workdir=tmp_path)
    argv = _argv(capturado)
    assert isinstance(argv, list)
    assert all(isinstance(tok, str) for tok in argv)


@pytest.mark.parametrize("backend", BACKENDS)
def test_workdir_e_honrado_em_todo_adaptador(tmp_path, capturado, backend):
    """Defeito 2: o `claude` rodava sem diretório de trabalho nenhum.

    Quem não tem flag de diretório honra por `cwd` do processo; quem tem, honra
    pelos dois. O que não se admite mais é rodar no diretório de quem chamou.
    """
    wd = tmp_path / "area"
    backends.invoke(_model(backend), "oi", workdir=wd)
    chamada = capturado[0]
    cap = backends.ADAPTERS[backend].regime.workdir
    assert cap.honrado, f"{backend} declara workdir ausente"
    assert chamada["cwd"] == wd
    if cap.suporte is backends.Suporte.FLAG:
        assert str(wd) in chamada["argv"]


# ---------------------------------------------------------------------------
# Contenção — bloqueio de escrita
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("backend", BACKENDS)
def test_bloqueio_de_escrita_aparece_ou_a_chamada_e_recusada(tmp_path, capturado, backend):
    """Defeito 1: `allow_write=False` era inerte no `agy`.

    Regra: pedindo `allow_write=False`, ou o adaptador emite a contenção que
    declarou, ou recusa. Nunca executa concedendo escrita em silêncio.
    """
    adapter = backends.ADAPTERS[backend]
    cap = adapter.regime.bloqueio_escrita
    try:
        backends.invoke(_model(backend), "oi", workdir=tmp_path, allow_write=False)
    except backends.ContainmentUnsupported:
        assert not cap.honrado or "exige" in cap.mecanismo
        return

    argv = _argv(capturado)
    # Jamais a flag que afrouxa tudo, quando se pediu escrita bloqueada.
    assert "--dangerously-skip-permissions" not in argv
    assert "--permission-mode" not in argv or "acceptEdits" not in argv
    if cap.suporte is backends.Suporte.FLAG:
        primeira_flag = cap.mecanismo.split()[0]
        assert primeira_flag in argv, f"{backend}: declarou {primeira_flag} e não emitiu"


def test_agy_sem_sandbox_e_sem_escrita_e_recusado(tmp_path, capturado):
    """O único mecanismo de bloqueio de escrita do `agy` depende do `--sandbox`.

    Sem sandbox não sobra nada — e a resposta certa é recusar, não rodar solto.
    """
    with pytest.raises(backends.ContainmentUnsupported):
        backends.invoke(
            _model("agy"), "oi", workdir=tmp_path, sandbox=False, allow_write=False
        )
    assert capturado == [], "não pode ter chegado a invocar o CLI"


def test_agy_concede_escrita_com_flag_real(tmp_path, capturado):
    """Pedindo escrita SEM contenção, a concessão vira flag de verdade.

    Antes era inerte: `allow_write` era aceito e nunca virava flag nenhuma.
    """
    backends.invoke(
        _model("agy"), "oi", workdir=tmp_path, sandbox=False, allow_write=True
    )
    argv = _argv(capturado)
    assert "--dangerously-skip-permissions" in argv
    assert "--sandbox" not in argv


def test_agy_recusa_contencao_com_escrita(tmp_path, capturado):
    """Contido E podendo escrever não é estado que o `agy` saiba ocupar.

    A flag de concessão desliga o portão de permissão inteiro, e o `--sandbox`
    do agy não é fronteira de segurança (issue #36). Anunciar os dois juntos
    prometeria contenção que não existe — e prometer o que não se entrega é a
    mesma falha do `exit 0` que este módulo documenta.
    """
    with pytest.raises(backends.ContainmentUnsupported):
        backends.invoke(
            _model("agy"), "oi", workdir=tmp_path, sandbox=True, allow_write=True
        )
    assert capturado == [], "não pode ter chegado a invocar o CLI"


def test_copilot_nega_ferramentas_de_escrita(tmp_path, capturado):
    backends.invoke(_model("copilot"), "oi", workdir=tmp_path, allow_write=False)
    negadas = _pares(_argv(capturado), "--deny-tool")
    assert "write" in negadas and "shell" in negadas


def test_claude_nega_ferramentas_de_escrita(tmp_path, capturado):
    """Defeito 2: o `claude` rodava `claude -p <prompt>` e mais nada."""
    backends.invoke(_model("claude"), "oi", workdir=tmp_path, allow_write=False)
    argv = _argv(capturado)
    assert "--disallowedTools" in argv
    for ferramenta in ("Write", "Edit", "Bash"):
        assert ferramenta in argv


# ---------------------------------------------------------------------------
# Contenção — isolamento
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("backend", BACKENDS)
def test_isolamento_aparece_ou_a_chamada_e_recusada(tmp_path, capturado, backend):
    adapter = backends.ADAPTERS[backend]
    cap = adapter.regime.isolamento
    try:
        backends.invoke(_model(backend), "oi", workdir=tmp_path, sandbox=True)
    except backends.ContainmentUnsupported:
        return
    argv = _argv(capturado)
    assert cap.honrado, f"{backend} executou com isolamento pedido e declarado ausente"
    primeira_flag = cap.mecanismo.split()[0]
    assert primeira_flag in argv, f"{backend}: declarou {primeira_flag} e não emitiu"


def test_claude_recusa_isolamento_junto_com_escrita(tmp_path, capturado):
    """`--restricted` do `claude` só deixa PESSOA aprovar escrita.

    Em headless não há pessoa. Conceder escrita sob isolamento seria prometer o
    que o CLI não entrega — então recusa.
    """
    with pytest.raises(backends.ContainmentUnsupported):
        backends.invoke(
            _model("claude"), "oi", workdir=tmp_path, sandbox=True, allow_write=True
        )
    assert capturado == []


def test_sem_isolamento_nao_emite_flag_de_isolamento(tmp_path, capturado):
    backends.invoke(_model("agy"), "oi", workdir=tmp_path, sandbox=False, allow_write=True)
    assert "--sandbox" not in _argv(capturado)


# ---------------------------------------------------------------------------
# Contenção — diretórios extras
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("backend", BACKENDS)
def test_extra_dirs_e_honrado_ou_recusado(tmp_path, capturado, backend):
    """Defeito 3: `extra_dirs` só era honrado pelo `agy`.

    Isso invalidava a proveniência por clone local num dos modos de extração da
    canibalização: o diretório era pedido, ignorado, e ninguém ficava sabendo.
    """
    extra = tmp_path / "clone"
    extra.mkdir()
    cap = backends.ADAPTERS[backend].regime.extra_dirs
    try:
        backends.invoke(_model(backend), "oi", workdir=tmp_path, extra_dirs=[extra])
    except backends.ContainmentUnsupported:
        assert not cap.honrado
        return
    assert cap.honrado, f"{backend} aceitou extra_dirs sem declarar suporte"
    assert str(extra) in _pares(_argv(capturado), "--add-dir")


# ---------------------------------------------------------------------------
# Recusa por contenção escala no router
# ---------------------------------------------------------------------------


def test_recusa_por_contencao_e_backend_unavailable():
    """O router captura `BackendUnavailable` para escalar.

    `ContainmentUnsupported` herda dela de propósito: a recusa vira troca de
    modelo, sem uma linha de mudança no `router.py`.
    """
    assert issubclass(backends.ContainmentUnsupported, backends.BackendUnavailable)
    assert issubclass(backends.BackendUnavailable, RuntimeError)


def test_recusa_concreta_e_capturavel_como_backend_unavailable(tmp_path, capturado):
    with pytest.raises(backends.BackendUnavailable):
        backends.invoke(
            _model("agy"), "oi", workdir=tmp_path, sandbox=False, allow_write=False
        )


# ---------------------------------------------------------------------------
# Prompt longo
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("backend", BACKENDS)
def test_prompt_longo_e_encaminhado_ou_recusado(tmp_path, capturado, backend):
    """Defeito de 31/08/2026: `_stage_prompt` devolvia `--add-dir` para todos.

    A flag ia parar no argv de qualquer CLI, aceitasse ele ou não, e o defeito só
    aparecia acima de 24.000 caracteres. Agora o encaminhamento é decisão do
    adaptador: ou ele usa o meio que o seu CLI de fato aceita, ou recusa.
    """
    longo = "x" * (backends._ARG_LIMIT + 1)
    cap = backends.ADAPTERS[backend].regime.prompt_longo
    try:
        backends.invoke(_model(backend), longo, workdir=tmp_path)
    except backends.ContainmentUnsupported:
        assert not cap.honrado
        return

    assert cap.honrado, f"{backend} encaminhou prompt longo sem declarar como"
    argv = _argv(capturado)
    assert longo not in argv, "prompt gigante não pode ir na linha de comando"
    encaminhados = list(tmp_path.glob("fpch-prompt-*.md"))
    assert len(encaminhados) == 1
    assert encaminhados[0].read_text(encoding="utf-8") == longo
    # O prompt efetivo passa a ser um ponteiro para o arquivo encaminhado.
    assert any(str(encaminhados[0]) in tok for tok in argv)


@pytest.mark.parametrize("backend", BACKENDS)
def test_nenhum_add_dir_para_cli_que_nao_aceita(tmp_path, capturado, backend):
    """`--add-dir` só pode aparecer no argv de quem declarou aceitá-la.

    Vale para o encaminhamento de prompt longo e para `extra_dirs`. É este teste
    que impede o defeito de 31/08/2026 de voltar por outro caminho quando um
    quarto adaptador entrar.
    """
    regime = backends.ADAPTERS[backend].regime
    aceita = "--add-dir" in regime.extra_dirs.mecanismo or "--add-dir" in regime.prompt_longo.mecanismo

    extra = tmp_path / "clone"
    extra.mkdir()
    longo = "y" * (backends._ARG_LIMIT + 1)
    try:
        backends.invoke(_model(backend), longo, workdir=tmp_path, extra_dirs=[extra])
    except backends.ContainmentUnsupported:
        return
    if not aceita:
        assert "--add-dir" not in _argv(capturado)


def test_prompt_curto_nao_encaminha_arquivo(tmp_path, capturado):
    backends.invoke(_model("agy"), "oi", workdir=tmp_path)
    assert list(tmp_path.glob("fpch-prompt-*.md")) == []


# ---------------------------------------------------------------------------
# looks_like_failure — o `exit 0` mentiroso
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("assinatura", backends._FAILURE_SIGNATURES)
def test_assinaturas_de_falha_sao_reconhecidas(assinatura):
    texto = "preambulo qualquer " + assinatura.upper() + " e mais texto de enchimento aqui"
    assert backends.looks_like_failure(texto) == assinatura


def test_limiar_de_quarenta_caracteres():
    assert backends.looks_like_failure("a" * 39) == "resposta suspeitamente curta"
    assert backends.looks_like_failure("a" * 40) is None
    # O limiar mede o texto SEM espaços nas pontas.
    assert backends.looks_like_failure("   " + "a" * 39 + "   ") == "resposta suspeitamente curta"


def test_resposta_real_passa():
    assert backends.looks_like_failure("A resposta e que o modulo compila e a suite passa.") is None


def test_falha_semantica_com_exit_zero_nao_vira_sucesso(tmp_path, monkeypatch):
    """A lição de 16/07/2026: `exit == 0` não é contrato de sucesso."""
    monkeypatch.setattr(backends.shutil, "which", lambda b: f"/fake/bin/{b}")
    monkeypatch.setattr(
        backends,
        "_run",
        lambda argv, timeout_s, cwd=None: (0, "jetski: no output produced - auto-denied"),
    )
    r = backends.invoke(_model("agy"), "oi", workdir=tmp_path)
    assert r.ok is False
    assert r.exit_code == 0
    assert "falha semântica" in (r.error or "")


# ---------------------------------------------------------------------------
# Erros de disponibilidade e timeout
# ---------------------------------------------------------------------------


def test_backend_desconhecido_levanta_backend_unavailable(tmp_path, capturado):
    with pytest.raises(backends.BackendUnavailable):
        backends.invoke(_model("inexistente"), "oi", workdir=tmp_path)


def test_backend_fora_do_path_levanta_backend_unavailable(tmp_path, monkeypatch):
    monkeypatch.setattr(backends.shutil, "which", lambda b: None)
    with pytest.raises(backends.BackendUnavailable):
        backends.invoke(_model("agy"), "oi", workdir=tmp_path)


def test_timeout_devolve_result_nao_ok(tmp_path, monkeypatch):
    monkeypatch.setattr(backends.shutil, "which", lambda b: f"/fake/bin/{b}")

    def estoura(argv, timeout_s, cwd=None):
        raise subprocess.TimeoutExpired(cmd=argv, timeout=timeout_s)

    monkeypatch.setattr(backends, "_run", estoura)
    r = backends.invoke(_model("agy"), "oi", workdir=tmp_path, timeout_s=7)
    assert r.ok is False
    assert r.exit_code == -1
    assert r.error == "timeout após 7s"
    assert r.backend == "agy"


def test_timeout_e_repassado_ao_run(tmp_path, capturado):
    backends.invoke(_model("agy"), "oi", workdir=tmp_path, timeout_s=42)
    assert capturado[0]["timeout_s"] == 42


def test_default_timeout_permanece_no_contrato():
    assert backends.DEFAULT_TIMEOUT_S == 600


# ---------------------------------------------------------------------------
# A interface declarada
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("backend", BACKENDS)
def test_todo_adaptador_declara_regime_completo(backend):
    """Regime é DADO, não comentário — e todo eixo traz mecanismo verificável."""
    adapter = backends.ADAPTERS[backend]
    assert adapter.nome == backend
    regime = adapter.regime
    for eixo in (
        "isolamento",
        "bloqueio_escrita",
        "concessao_escrita",
        "workdir",
        "extra_dirs",
        "prompt_longo",
    ):
        cap = getattr(regime, eixo)
        assert isinstance(cap, backends.Capacidade)
        assert cap.mecanismo.strip(), f"{backend}.{eixo} sem mecanismo declarado"
