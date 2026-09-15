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

import re
import subprocess

import pytest

from fpch import backends
from fpch.models import Model, Pool, TaskClass


# ---------------------------------------------------------------------------
# Instrumentação — nada aqui sai para o sistema operacional
# ---------------------------------------------------------------------------

BACKENDS = sorted(backends.ADAPTERS)


#: O codex exige id no formato "<slug> (<esforço>)"; os demais aceitam qualquer id.
_ID_DE_TESTE = {"codex": "modelo-de-teste (low)"}


def _model(backend: str) -> Model:
    return Model(
        id=_ID_DE_TESTE.get(backend, "modelo-de-teste"),
        backend=backend,
        pool=Pool.LOCAL,
        power=3,
        cost=1,
        good_for=(TaskClass.STANDARD,),
    )


RESPOSTA_PLAUSIVEL = "resposta longa o bastante para nao disparar o limiar de 40 chars"

_NONCE_NA_INSTRUCAO = re.compile(r"this code: ([0-9a-f]{16})")


def _nonce(argv: list[str]) -> str:
    """O nonce que `invoke` pôs na instrução — lido do argv, como o modelo leria."""
    achados = {m.group(1) for tok in argv for m in _NONCE_NA_INSTRUCAO.finditer(tok)}
    assert len(achados) == 1, f"esperava exatamente um nonce no argv, achei {achados}"
    return achados.pop()


def _prompt(argv: list[str]) -> str:
    """O prompt efetivo: após `-p` (agy/copilot/claude) ou o último, após `--` (codex)."""
    if "-p" in argv:
        return argv[argv.index("-p") + 1]
    assert argv[-2] == "--", f"prompt sem `-p` tem de vir por último, após `--`: {argv}"
    return argv[-1]


def _sentinela(argv: list[str]) -> str:
    return f"FPCH-FIM-{_nonce(argv)}"


def _responde(monkeypatch, fabrica):
    """Instala `_run` falso cuja saída é `fabrica(argv)` → (exit, texto)."""
    chamadas: list[list[str]] = []

    def fake_run(argv, timeout_s, cwd=None):
        chamadas.append(argv)
        return fabrica(argv)

    monkeypatch.setattr(backends, "_run", fake_run)
    monkeypatch.setattr(backends.shutil, "which", lambda b: f"/fake/bin/{b}")
    return chamadas


@pytest.fixture
def capturado(monkeypatch):
    """Substitui `_run` por captura. Devolve a lista de chamadas feitas.

    A saída falsa CUMPRE o contrato de saída (C18): ecoa a sentinela da chamada,
    como um modelo obediente faria. Quem precisa de saída que não cumpre usa
    `_responde` diretamente.
    """
    chamadas: list[dict] = []

    def fake_run(argv, timeout_s, cwd=None):
        chamadas.append({"argv": argv, "timeout_s": timeout_s, "cwd": cwd})
        return 0, f"{RESPOSTA_PLAUSIVEL}\n{_sentinela(argv)}"

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
    assert argv[:4] == ["agy", "--model", "modelo-de-teste", "-p"]
    # O prompt efetivo é o do chamador seguido da instrução do contrato (C18).
    assert argv[4].startswith("oi\n")
    assert "OUTPUT CONTRACT" in argv[4]
    assert "--sandbox" in argv


def test_argv_copilot_caso_simples(tmp_path, capturado):
    backends.invoke(_model("copilot"), "oi", workdir=tmp_path)
    argv = _argv(capturado)
    assert argv[0] == "copilot"
    assert _pares(argv, "-C") == [str(tmp_path)]
    assert argv[-2] == "-p" and argv[-1].startswith("oi\n")
    # Sem `--silent`, as estatísticas do copilot viriam depois da sentinela.
    assert "--silent" in argv


def test_argv_claude_caso_simples(tmp_path, capturado):
    backends.invoke(_model("claude"), "oi", workdir=tmp_path)
    argv = _argv(capturado)
    assert argv[0] == "claude"
    # O prompt do `claude` é POSICIONAL depois de `-p`, e as opções variádicas
    # (`--disallowedTools`, `--add-dir`) engolem valor solto que venha depois.
    # Por isso `-p <prompt>` tem de ser o final do argv.
    assert argv[-2] == "-p" and argv[-1].startswith("oi\n")


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
    conteudo = encaminhados[0].read_text(encoding="utf-8")
    assert conteudo.startswith(longo)
    # O prompt efetivo passa a ser um ponteiro para o arquivo encaminhado.
    assert any(str(encaminhados[0]) in tok for tok in argv)
    # C18: a instrução do contrato chega ao modelo pelos dois caminhos — no fim
    # do arquivo encaminhado e no fim do ponteiro —, com o MESMO nonce.
    nonce = _nonce(argv)  # achado no argv = está no ponteiro
    assert conteudo.find("OUTPUT CONTRACT") >= len(longo), "instrução tem de vir depois da tarefa"
    assert f"this code: {nonce}" in conteudo[len(longo):]


def _encaminhou(tmp_path) -> bool:
    return bool(list(tmp_path.glob("fpch-prompt-*.md")))


def test_limite_de_argv_e_medido_com_a_instrucao_somada(tmp_path, capturado):
    """Prompt que cabe sozinho, mas estoura com a instrução, TEM de ir para arquivo.

    Medir só o prompt do chamador deixaria passar para a linha de comando um
    argumento maior que `_ARG_LIMIT` — exatamente o que o limite existe para impedir.
    """
    tamanho = backends._ARG_LIMIT - backends.FPCH_CONTRATO_INSTRUCAO_CHARS + 1
    assert tamanho <= backends._ARG_LIMIT, "premissa: sozinho, o prompt cabe"
    backends.invoke(_model("agy"), "p" * tamanho, workdir=tmp_path)
    assert _encaminhou(tmp_path)
    assert all(len(tok) <= backends._ARG_LIMIT for tok in _argv(capturado))


@pytest.mark.parametrize("folga", [0, 1])
def test_prompt_com_instrucao_exatamente_no_limite_ou_abaixo_nao_encaminha(
    tmp_path, capturado, folga
):
    tamanho = backends._ARG_LIMIT - backends.FPCH_CONTRATO_INSTRUCAO_CHARS - folga
    backends.invoke(_model("agy"), "p" * tamanho, workdir=tmp_path)
    assert not _encaminhou(tmp_path)
    [efetivo] = _pares(_argv(capturado), "-p")
    assert len(efetivo) == backends._ARG_LIMIT - folga


def test_acrescimo_da_instrucao_e_constante():
    a, b = backends.FpchContratoSaida.novo(), backends.FpchContratoSaida.novo()
    assert len(a.instrucao) == len(b.instrucao) == backends.FPCH_CONTRATO_INSTRUCAO_CHARS


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
    """A lição de 16/07/2026: `exit == 0` não é contrato de sucesso.

    Sem sentinela, quem reprova é o contrato — antes mesmo da lista de negação.
    """
    _responde(monkeypatch, lambda argv: (0, "jetski: no output produced - auto-denied"))
    r = backends.invoke(_model("agy"), "oi", workdir=tmp_path)
    assert r.ok is False
    assert r.exit_code == 0
    assert (r.error or "").startswith(backends.FPCH_ERRO_SENTINELA_AUSENTE)


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
        "saida_estruturada",
    ):
        cap = getattr(regime, eixo)
        assert isinstance(cap, backends.Capacidade)
        assert cap.mecanismo.strip(), f"{backend}.{eixo} sem mecanismo declarado"


# ---------------------------------------------------------------------------
# Contrato de saída — sentinela com nonce por chamada (C18)
# ---------------------------------------------------------------------------


def test_sentinela_presente_da_ok_e_some_do_texto(tmp_path, monkeypatch):
    _responde(monkeypatch, lambda argv: (0, f"{RESPOSTA_PLAUSIVEL}\n{_sentinela(argv)}"))
    r = backends.invoke(_model("claude"), "oi", workdir=tmp_path)
    assert r.ok is True
    assert r.error is None
    assert r.contract == backends.FPCH_CONTRATO_SENTINELA
    assert r.text == RESPOSTA_PLAUSIVEL
    assert "FPCH-FIM" not in r.text


def test_sem_sentinela_exit_zero_e_texto_plausivel_e_falha(tmp_path, monkeypatch):
    longo = "Aqui está a análise completa do repositório, com arquitetura e riscos. " * 5
    _responde(monkeypatch, lambda argv: (0, longo))
    r = backends.invoke(_model("agy"), "oi", workdir=tmp_path)
    assert r.ok is False
    assert r.exit_code == 0
    assert (r.error or "").startswith(backends.FPCH_ERRO_SENTINELA_AUSENTE)


def test_regressao_c18_erro_de_fornecedor_fora_da_lista_de_negacao(tmp_path, monkeypatch):
    """O teste de C18: redação de erro que a lista de negação nunca viu.

    Antes, `exit 0` + texto com mais de 40 caracteres + nenhuma das 8 frases
    conhecidas = sucesso. Foi assim que uma mensagem de erro virou artefato.
    """
    erro = "service temporarily degraded, please retry your request in a few minutes"
    assert backends.looks_like_failure(erro) is None, "premissa: a lista não conhece a frase"
    _responde(monkeypatch, lambda argv: (0, erro))
    r = backends.invoke(_model("agy"), "oi", workdir=tmp_path)
    assert r.ok is False
    assert (r.error or "").startswith(backends.FPCH_ERRO_SENTINELA_AUSENTE)


def test_instrucao_nunca_contem_a_sentinela_inteira():
    contrato = backends.FpchContratoSaida.novo()
    assert contrato.sentinela not in contrato.instrucao
    assert contrato.nonce in contrato.instrucao


@pytest.mark.parametrize("backend", BACKENDS)
def test_eco_do_prompt_com_a_instrucao_nao_cumpre_o_contrato(tmp_path, monkeypatch, backend):
    """Erro de fornecedor que cita o prompt carrega a instrução — e o nonce."""

    def ecoa(argv):
        prompt = _prompt(argv)
        return 0, f"Error: request could not be completed. Original prompt follows:\n{prompt}"

    _responde(monkeypatch, ecoa)
    r = backends.invoke(_model(backend), "oi, analise o repositorio", workdir=tmp_path)
    assert r.ok is False
    assert (r.error or "").startswith(backends.FPCH_ERRO_SENTINELA_AUSENTE)


def test_eco_truncado_logo_depois_do_nonce_nao_cumpre(tmp_path, monkeypatch):
    """Eco cortado bem no nonce: a última linha termina no código, mas não é a sentinela."""

    def ecoa_truncado(argv):
        prompt = _prompt(argv)
        corte = prompt.index(_nonce(argv)) + 16
        return 0, "upstream error while echoing input:\n" + prompt[:corte]

    _responde(monkeypatch, ecoa_truncado)
    r = backends.invoke(_model("agy"), "oi", workdir=tmp_path)
    assert r.ok is False
    assert (r.error or "").startswith(backends.FPCH_ERRO_SENTINELA_AUSENTE)


def test_sentinela_de_outra_chamada_nao_cumpre(tmp_path, monkeypatch):
    alheia = "FPCH-FIM-" + "0123456789abcdef"
    _responde(monkeypatch, lambda argv: (0, f"{RESPOSTA_PLAUSIVEL}\n{alheia}"))
    r = backends.invoke(_model("agy"), "oi", workdir=tmp_path)
    assert r.ok is False
    assert (r.error or "").startswith(backends.FPCH_ERRO_SENTINELA_AUSENTE)
    # Nonce alheio não é marca desta chamada: fica no texto, como conteúdo.
    assert alheia in r.text


def test_conteudo_depois_da_sentinela_nao_cumpre(tmp_path, monkeypatch):
    """Regra escolhida: sentinela é a ÚLTIMA linha não-vazia.

    Conteúdo depois do fim declarado é a forma de um erro anexado pelo
    fornecedor — aceitar "última ocorrência" deixaria isto passar.
    """
    _responde(
        monkeypatch,
        lambda argv: (0, f"{RESPOSTA_PLAUSIVEL}\n{_sentinela(argv)}\nservice degraded, retry later"),
    )
    r = backends.invoke(_model("agy"), "oi", workdir=tmp_path)
    assert r.ok is False
    assert (r.error or "").startswith(backends.FPCH_ERRO_SENTINELA_AUSENTE)
    assert "FPCH-FIM" not in r.text


def test_sentinela_repetida_no_meio_e_no_fim_cumpre_e_some_toda(tmp_path, monkeypatch):
    _responde(
        monkeypatch,
        lambda argv: (0, f"{RESPOSTA_PLAUSIVEL}\n{_sentinela(argv)}\nmais texto\n{_sentinela(argv)}"),
    )
    r = backends.invoke(_model("agy"), "oi", workdir=tmp_path)
    assert r.ok is True
    assert "FPCH-FIM" not in r.text


@pytest.mark.parametrize(
    "molde",
    [
        "{resp}\r\n{sent}\r\n",
        "{resp}\r\n{sent}   \r\n\r\n",
        "{resp}\n   {sent}\t\n\n  \n",
        "{resp}\n**{sent}**",
        "{resp}\n`{sent}`",
    ],
)
def test_crlf_espaco_final_e_decoracao_sao_tolerados(tmp_path, monkeypatch, molde):
    _responde(
        monkeypatch,
        lambda argv: (0, molde.format(resp=RESPOSTA_PLAUSIVEL, sent=_sentinela(argv))),
    )
    r = backends.invoke(_model("copilot"), "oi", workdir=tmp_path)
    assert r.ok is True, r.error
    assert r.text == RESPOSTA_PLAUSIVEL


@pytest.mark.parametrize("backend", BACKENDS)
def test_prompt_encaminhado_cumpre_contrato_com_nonce_do_ponteiro(tmp_path, monkeypatch, backend):
    longo = "z" * (backends._ARG_LIMIT + 1)
    chamadas = _responde(monkeypatch, lambda argv: (0, f"{RESPOSTA_PLAUSIVEL}\n{_sentinela(argv)}"))
    r = backends.invoke(_model(backend), longo, workdir=tmp_path)
    assert r.ok is True
    [encaminhado] = tmp_path.glob("fpch-prompt-*.md")
    assert f"this code: {_nonce(chamadas[0])}" in encaminhado.read_text(encoding="utf-8")


def test_lista_de_negacao_ainda_reprova_com_sentinela_presente(tmp_path, monkeypatch):
    _responde(
        monkeypatch,
        lambda argv: (0, f"jetski: no output produced - tool auto-denied\n{_sentinela(argv)}"),
    )
    r = backends.invoke(_model("agy"), "oi", workdir=tmp_path)
    assert r.ok is False
    assert "falha semântica" in (r.error or "")
    assert "FPCH-FIM" not in (r.error or "")


def test_heuristica_de_resposta_curta_mede_o_texto_sem_sentinela(tmp_path, monkeypatch):
    _responde(monkeypatch, lambda argv: (0, f"ok\n{_sentinela(argv)}"))
    r = backends.invoke(_model("agy"), "oi", workdir=tmp_path)
    assert r.ok is False
    assert "resposta suspeitamente curta" in (r.error or "")


def test_exit_diferente_de_zero_continua_falha_e_sem_sentinela_no_erro(tmp_path, monkeypatch):
    _responde(monkeypatch, lambda argv: (2, f"boom\n{_sentinela(argv)}"))
    r = backends.invoke(_model("agy"), "oi", workdir=tmp_path)
    assert r.ok is False
    assert r.exit_code == 2
    assert r.error == "boom"


def test_opt_out_e_deliberado_e_registrado(tmp_path, monkeypatch):
    chamadas = _responde(monkeypatch, lambda argv: (0, RESPOSTA_PLAUSIVEL))
    r = backends.invoke(_model("agy"), "oi", workdir=tmp_path, output_contract=False)
    assert r.ok is True
    assert r.contract == backends.FPCH_CONTRATO_NENHUM
    # Sem contrato, nada de instrução no prompt: o argv volta ao de antes de C18.
    assert chamadas[0][:5] == ["agy", "--model", "modelo-de-teste", "-p", "oi"]


def test_opt_out_mantem_a_lista_de_negacao(tmp_path, monkeypatch):
    _responde(monkeypatch, lambda argv: (0, "quota exceeded for this account, try again tomorrow"))
    r = backends.invoke(_model("agy"), "oi", workdir=tmp_path, output_contract=False)
    assert r.ok is False
    assert "falha semântica" in (r.error or "")


def test_timeout_registra_o_contrato_exigido(tmp_path, monkeypatch):
    def estoura(argv):
        raise subprocess.TimeoutExpired(cmd=argv, timeout=1)

    _responde(monkeypatch, estoura)
    r = backends.invoke(_model("agy"), "oi", workdir=tmp_path, timeout_s=1)
    assert r.ok is False
    assert r.contract == backends.FPCH_CONTRATO_SENTINELA


def test_nonce_e_novo_a_cada_chamada(tmp_path, capturado):
    backends.invoke(_model("agy"), "oi", workdir=tmp_path)
    backends.invoke(_model("agy"), "oi", workdir=tmp_path)
    assert _nonce(capturado[0]["argv"]) != _nonce(capturado[1]["argv"])


def test_result_montado_a_mao_nao_anuncia_contrato():
    r = backends.Result(
        ok=True, text="x", exit_code=0, latency_s=0.0, model="m", backend="b", pool=Pool.LOCAL,
    )
    assert r.contract == backends.FPCH_CONTRATO_NENHUM


@pytest.mark.parametrize("backend", BACKENDS)
def test_envelope_estruturado_e_lacuna_declarada(backend):
    """Nenhum esquema de envelope JSON foi verificável sem invocar modelo.

    `claude`, `copilot` e `agy` documentam `--output-format json` no `--help`,
    mas nenhum documenta o esquema. Enquanto for assim a capacidade é AUSENTE e
    `"envelope"` não existe no vocabulário. Quem mudar isto precisa trazer o
    parser E o teste de `is_error`/JSON malformado junto.
    """
    cap = backends.ADAPTERS[backend].regime.saida_estruturada
    assert cap.suporte is backends.Suporte.AUSENTE
    # `agy`/`copilot`/`claude` chamam a flag `--output-format json`; o codex, `--json`.
    assert "--output-format json" in cap.mecanismo or "--json" in cap.mecanismo
    assert "envelope" not in backends.FPCH_CONTRATOS


def test_so_a_sentinela_exata_desta_chamada_e_removida():
    """Fronteira de token: 17 hex, letra colada e nonce alheio ficam intactos."""
    c = backends.FpchContratoSaida(nonce="0123456789abcdef")
    literal_do_repo = "FPCH-FIM-fedcba9876543210"  # como aparece em teste citado
    bruto = (
        f"cita {literal_do_repo} de outro teste\n"
        f"dezessete hex {c.sentinela}f fica\n"
        f"colado X{c.sentinela} fica\n"
        f"no meio ({c.sentinela}) sai\n"
        f"{c.sentinela}"
    )
    texto, cumprido = c.aplicar(bruto)
    assert cumprido is True
    assert literal_do_repo in texto
    assert f"{c.sentinela}f" in texto
    assert f"X{c.sentinela}" in texto
    assert "no meio () sai" in texto


def test_dezessete_hex_na_ultima_linha_nao_cumpre():
    c = backends.FpchContratoSaida(nonce="0123456789abcdef")
    texto, cumprido = c.aplicar(f"{RESPOSTA_PLAUSIVEL}\n{c.sentinela}f")
    assert cumprido is False
    assert texto.endswith(f"{c.sentinela}f")


def test_sem_contrato_texto_com_formato_de_sentinela_volta_intacto(tmp_path, monkeypatch):
    bruto = f"{RESPOSTA_PLAUSIVEL}\nFPCH-FIM-0123456789abcdef"
    _responde(monkeypatch, lambda argv: (0, bruto))
    r = backends.invoke(_model("agy"), "oi", workdir=tmp_path, output_contract=False)
    assert r.ok is True
    assert r.text == bruto


# --- diagnóstico da falha de contrato (C11: não achatar causas distintas) ---


def test_contrato_falho_com_quota_exceeded_traz_prefixo_e_assinatura(tmp_path, monkeypatch):
    _responde(monkeypatch, lambda argv: (0, "Error: Quota exceeded for model tier, try again later"))
    r = backends.invoke(_model("agy"), "oi", workdir=tmp_path)
    assert r.ok is False
    assert r.error.startswith(backends.FPCH_ERRO_SENTINELA_AUSENTE)
    assert "assinatura: 'quota exceeded'" in r.error
    assert "Quota exceeded for model tier" in r.error
    assert r.failure_signature == "quota exceeded"


def test_contrato_falho_com_frase_desconhecida_traz_prefixo_e_trecho(tmp_path, monkeypatch):
    frase = "service temporarily degraded, please retry your request in a few minutes"
    _responde(monkeypatch, lambda argv: (0, frase))
    r = backends.invoke(_model("agy"), "oi", workdir=tmp_path)
    assert r.error == f"{backends.FPCH_ERRO_SENTINELA_AUSENTE} — {frase}"
    assert "assinatura" not in r.error
    assert r.failure_signature is None


def test_trecho_do_erro_e_saneado_e_limitado(tmp_path, monkeypatch):
    sujo = "linha1\r\n\tlinha2\x1b[31m" + "y" * 500
    _responde(monkeypatch, lambda argv: (0, sujo))
    r = backends.invoke(_model("agy"), "oi", workdir=tmp_path)
    trecho = r.error.split(" — ", 1)[1]
    assert len(trecho) == 200
    assert "\n" not in r.error and "\r" not in r.error and "\x1b" not in r.error
    assert trecho.startswith("linha1 linha2 [31m")


def test_contrato_falho_com_saida_vazia_nao_pendura_separador(tmp_path, monkeypatch):
    _responde(monkeypatch, lambda argv: (0, ""))
    r = backends.invoke(_model("agy"), "oi", workdir=tmp_path)
    assert r.error == (
        f"{backends.FPCH_ERRO_SENTINELA_AUSENTE}; assinatura: 'resposta suspeitamente curta'"
    )


def test_failure_signature_tambem_quando_o_contrato_passa(tmp_path, monkeypatch):
    _responde(
        monkeypatch, lambda argv: (0, f"rate limit exceeded on upstream provider\n{_sentinela(argv)}")
    )
    r = backends.invoke(_model("agy"), "oi", workdir=tmp_path)
    assert r.ok is False
    assert r.failure_signature == "rate limit exceeded"


def test_sucesso_nao_tem_failure_signature(tmp_path, capturado):
    r = backends.invoke(_model("agy"), "oi", workdir=tmp_path)
    assert r.ok is True and r.failure_signature is None


# ---------------------------------------------------------------------------
# codex — id "<slug> (<esforço>)", mapeamento de sandbox, prompt por último
# ---------------------------------------------------------------------------


def _codex(model_id: str = "gpt-5.6-luna (medium)") -> Model:
    return Model(
        id=model_id, backend="codex", pool=Pool.CODEX, power=3, cost=2,
        good_for=(TaskClass.STANDARD,),
    )


def test_argv_codex_caso_simples(tmp_path, capturado):
    backends.invoke(_codex(), "oi", workdir=tmp_path)
    argv = _argv(capturado)
    assert argv[:2] == ["codex", "exec"]
    assert "--skip-git-repo-check" in argv and "--ephemeral" in argv
    assert _pares(argv, "-C") == [str(tmp_path)]
    assert _pares(argv, "-m") == ["gpt-5.6-luna"]
    assert _pares(argv, "-s") == ["read-only"]
    assert argv[-2] == "--" and argv[-1].startswith("oi\n")
    assert "OUTPUT CONTRACT" in argv[-1]


@pytest.mark.parametrize(
    ("model_id", "slug", "esforco"),
    [
        ("gpt-5.6-luna (low)", "gpt-5.6-luna", "low"),
        ("gpt-5.5 (xhigh)", "gpt-5.5", "xhigh"),
        ("gpt-6-astra (max)", "gpt-6-astra", "max"),
    ],
)
def test_codex_esforco_vai_sempre_explicito(tmp_path, capturado, model_id, slug, esforco):
    """A config do autor fixa `xhigh`: omitir o esforço herdaria o mais caro."""
    backends.invoke(_codex(model_id), "oi", workdir=tmp_path)
    argv = _argv(capturado)
    assert _pares(argv, "-m") == [slug]
    assert _pares(argv, "-c") == [f"model_reasoning_effort={esforco}"]


@pytest.mark.parametrize(
    "model_id",
    [
        "default",
        "gpt-5.6-luna",
        "gpt-5.6-luna (Low)",
        "gpt-5.6-luna (turbo)",
        "gpt-5.6-luna  (low)",
        "-m evil (low)",
        "gpt-5.6-luna (low) --dangerously-bypass-approvals-and-sandbox",
        "",
    ],
)
def test_codex_id_malformado_e_recusado_sem_invocar(tmp_path, capturado, model_id):
    with pytest.raises(backends.FpchModeloMalformado, match="malformado"):
        backends.invoke(_codex(model_id), "x" * (backends._ARG_LIMIT + 1), workdir=tmp_path)
    assert capturado == []
    assert list(tmp_path.glob("fpch-prompt-*.md")) == [], "id inválido não pode deixar arquivo"


def test_codex_modelo_malformado_escala_no_router():
    assert issubclass(backends.FpchModeloMalformado, backends.BackendUnavailable)


@pytest.mark.parametrize(
    ("sandbox", "allow_write", "modo"),
    [
        (True, False, "read-only"),
        (True, True, "workspace-write"),
        (False, False, "read-only"),
    ],
)
def test_codex_mapeamento_de_sandbox(tmp_path, capturado, sandbox, allow_write, modo):
    backends.invoke(_codex(), "oi", workdir=tmp_path, sandbox=sandbox, allow_write=allow_write)
    assert _pares(_argv(capturado), "-s") == [modo]


def test_codex_escrita_sem_sandbox_e_recusada(tmp_path, capturado):
    """O único modo sem sandbox do codex é `danger-full-access` — nunca emitido."""
    with pytest.raises(backends.ContainmentUnsupported):
        backends.invoke(_codex(), "oi", workdir=tmp_path, sandbox=False, allow_write=True)
    assert capturado == []


@pytest.mark.parametrize("sandbox", [True, False])
@pytest.mark.parametrize("allow_write", [True, False])
def test_codex_nunca_emite_flag_perigosa(tmp_path, capturado, sandbox, allow_write):
    try:
        backends.invoke(
            _codex(), "oi", workdir=tmp_path, sandbox=sandbox, allow_write=allow_write,
            extra_dirs=[tmp_path / "clone"],
        )
    except backends.ContainmentUnsupported:
        return
    flags = _argv(capturado)[:-1]  # o último token é o prompt, que é dado
    assert "danger-full-access" not in flags
    assert not any(t.startswith("--dangerously") for t in flags)


def test_codex_prompt_que_parece_flag_fica_depois_do_separador(tmp_path, monkeypatch):
    # Sem contrato (sem nonce), para o prompt chegar ao argv exatamente como veio.
    chamadas = _responde(monkeypatch, lambda argv: (0, RESPOSTA_PLAUSIVEL))
    backends.invoke(
        _codex(), "--dangerously-bypass-approvals-and-sandbox", workdir=tmp_path,
        extra_dirs=[tmp_path / "a", tmp_path / "b"], output_contract=False,
    )
    [argv] = chamadas
    assert argv[-2:] == ["--", "--dangerously-bypass-approvals-and-sandbox"]
    assert argv.count("--") == 1
    assert _pares(argv, "--add-dir") == [str(tmp_path / "a"), str(tmp_path / "b")]


def test_codex_prompt_so_hifen_e_recusado(tmp_path, capturado):
    """`-` posicional é "leia do stdin" — mesmo depois de `--`."""
    with pytest.raises(backends.BackendUnavailable):
        backends.invoke(_codex(), "-", workdir=tmp_path, output_contract=False)
    assert capturado == []


def test_codex_prompt_longo_nao_acrescenta_add_dir_do_workdir(tmp_path, capturado):
    """O arquivo encaminhado mora no workdir, que o `-C` já expõe."""
    backends.invoke(_codex(), "w" * (backends._ARG_LIMIT + 1), workdir=tmp_path)
    assert _pares(_argv(capturado), "--add-dir") == []


# ---------------------------------------------------------------------------
# Resolução de executável no Windows (shim .cmd do npm)
# ---------------------------------------------------------------------------


def _which_falso(monkeypatch, mapa: dict[str, str]):
    monkeypatch.setattr(backends.shutil, "which", lambda nome: mapa.get(nome))


def _shim_npm(pasta, nome: str, alvo: str) -> str:
    alvo_path = pasta.joinpath(*alvo.split("\\"))
    alvo_path.parent.mkdir(parents=True, exist_ok=True)
    alvo_path.write_text("", encoding="utf-8")
    shim = pasta / f"{nome}.cmd"
    shim.write_text(
        "@ECHO off\r\nSETLOCAL\r\nCALL :find_dp0\r\n"
        'endLocal & goto #_undefined_# 2>NUL || title %COMSPEC% & "%_prog%"  '
        f'"%dp0%\\{alvo}" %*\r\n',
        encoding="utf-8",
    )
    return str(shim)


def test_resolve_fora_do_windows_nao_mexe(monkeypatch):
    _which_falso(monkeypatch, {})
    assert backends._resolve_executavel(["codex", "exec"], windows=False) == ["codex", "exec"]


def test_resolve_exe_no_path_fica_intacto(monkeypatch, tmp_path):
    """Caso do agy e do copilot: `CreateProcess` já acha o `.exe`."""
    _which_falso(monkeypatch, {
        "copilot.exe": str(tmp_path / "copilot.exe"),
        "copilot": str(tmp_path / "copilot.bat"),
    })
    argv = ["copilot", "-p", "x"]
    assert backends._resolve_executavel(argv, windows=True) == argv


def test_resolve_shim_npm_de_script_vira_node(monkeypatch, tmp_path):
    shim = _shim_npm(tmp_path, "codex", "node_modules\\@openai\\codex\\bin\\codex.js")
    _which_falso(monkeypatch, {"codex": shim, "node.exe": "C:\\node\\node.exe"})
    prompt = 'linha 1\nlinha "2" & echo PWNED %PATH%'
    argv = backends._resolve_executavel(["codex", "exec", "--", prompt], windows=True)
    assert argv[0] == "C:\\node\\node.exe"
    assert argv[1] == str(tmp_path / "node_modules" / "@openai" / "codex" / "bin" / "codex.js")
    assert argv[2:] == ["exec", "--", prompt], "os argumentos seguem intactos, sem cmd.exe"


def test_resolve_shim_npm_de_binario_nativo_vira_exe(monkeypatch, tmp_path):
    shim = _shim_npm(tmp_path, "claude", "node_modules\\@anthropic-ai\\claude-code\\bin\\claude.exe")
    _which_falso(monkeypatch, {"claude": shim})
    argv = backends._resolve_executavel(["claude", "-p", "x"], windows=True)
    exe = tmp_path / "node_modules" / "@anthropic-ai" / "claude-code" / "bin" / "claude.exe"
    assert argv == [str(exe), "-p", "x"]


def test_resolve_cmd_desconhecido_e_recusado(monkeypatch, tmp_path):
    """Executar um .cmd qualquer passaria o prompt pelo cmd.exe."""
    shim = tmp_path / "coisa.cmd"
    shim.write_text("@echo off\r\nalgo.exe %*\r\n", encoding="utf-8")
    _which_falso(monkeypatch, {"coisa": str(shim)})
    with pytest.raises(backends.BackendUnavailable, match="cmd.exe"):
        backends._resolve_executavel(["coisa", "x"], windows=True)


def test_resolve_shim_de_script_sem_node_e_recusado(monkeypatch, tmp_path):
    shim = _shim_npm(tmp_path, "codex", "node_modules\\@openai\\codex\\bin\\codex.js")
    _which_falso(monkeypatch, {"codex": shim})
    with pytest.raises(backends.BackendUnavailable, match="node.exe"):
        backends._resolve_executavel(["codex"], windows=True)


def test_run_fecha_stdin_do_processo(monkeypatch):
    """`_run` nunca herda stdin: `codex exec` anexaria um stdin em pipe ao prompt."""
    visto = {}

    class _Proc:
        returncode = 0
        stdout = "ok"
        stderr = ""

    def fake_subprocess_run(argv, **kw):
        visto.update(kw)
        return _Proc()

    monkeypatch.setattr(backends.subprocess, "run", fake_subprocess_run)
    monkeypatch.setattr(backends, "_resolve_executavel", lambda argv, **_: argv)
    assert backends._run(["qualquer"], 5) == (0, "ok")
    assert visto["stdin"] is backends.subprocess.DEVNULL
    assert visto["shell"] is False
