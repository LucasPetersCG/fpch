"""Invocação dos CLIs de agent.

REGRA DE OURO — a fronteira legal do FPCH:
Só invocamos o BINÁRIO OFICIAL de cada fornecedor, em modo headless documentado
por ele. Nunca lemos token/credencial, nunca forjamos header, nunca reempacotamos
a cota como API. Essa é exatamente a linha que separa uso pretendido de banimento
(Anthropic cortou clientes que forjavam identidade em 04/04/2026; Google responde
403; GitHub AUP proíbe). O ilícito é mentir sobre quem é o cliente — não automatizar.

Ver `docs/analises/camada-multi-modelo.md`.

SEGUNDA REGRA — contenção declarada, nunca presumida (31/08/2026):
Cada backend é um adaptador que DECLARA, como dado, qual regime de contenção
sabe honrar e por qual mecanismo verificável. Onde não souber honrar o regime
pedido, o adaptador recusa a chamada em vez de executá-la com contenção menor
do que a solicitada. Conceder em silêncio menos do que se pediu é a mesma classe
de falha do `exit 0` documentado em `_FAILURE_SIGNATURES`: uma promessa que
falhou sendo lida como promessa cumprida.
"""

from __future__ import annotations

import shutil
import subprocess
import time
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Protocol

from .models import Model, Pool

# CreateProcess do Windows corta a linha de comando em 32767 chars. Deixamos folga
# para as demais flags. Acima disto o prompt vai para arquivo (ver `_stage_prompt`).
_ARG_LIMIT = 24_000

DEFAULT_TIMEOUT_S = 600


@dataclass
class Result:
    ok: bool
    text: str
    exit_code: int
    latency_s: float
    model: str
    backend: str
    pool: Pool
    error: str | None = None


class BackendUnavailable(RuntimeError):
    """Backend inutilizável para esta chamada. O router captura e escala."""


class ContainmentUnsupported(BackendUnavailable):
    """O adaptador não sabe honrar o regime de contenção pedido.

    Herda de `BackendUnavailable` DE PROPÓSITO: o laço de escalada do
    `router.py` já captura essa exceção e passa ao próximo modelo da cadeia.
    Assim a recusa por contenção vira degradação de rota, não queda da chamada —
    e o router não precisa saber que este conceito existe.
    """


# --------------------------------------------------------------------------
# Regime de contenção — declarado como DADO, não como comentário
# --------------------------------------------------------------------------


class Suporte(str, Enum):
    """Como (ou se) um adaptador honra um eixo do regime de contenção."""

    FLAG = "flag"          # há flag oficial e documentada no `--help` do CLI
    PROCESSO = "processo"  # honrado fora do CLI, pelo próprio processo (cwd)
    OMISSAO = "omissao"    # honrado por NÃO passar a flag que afrouxaria
    AUSENTE = "ausente"    # o CLI não oferece meio algum — declara incapacidade


@dataclass(frozen=True)
class Capacidade:
    """Um eixo do regime, com o mecanismo verificável que o sustenta.

    `mecanismo` é texto auditável: ou a flag real que o CLI aceita, ou a razão
    concreta da ausência. Nunca uma promessa vaga.
    """

    suporte: Suporte
    mecanismo: str

    @property
    def honrado(self) -> bool:
        return self.suporte is not Suporte.AUSENTE


@dataclass(frozen=True)
class Regime:
    """O que o adaptador sabe fazer, eixo a eixo.

    Verificado em 31/08/2026 rodando `--help` de cada binário na máquina do
    autor. Flag que não aparece no `--help` não entra aqui: declara-se AUSENTE.
    """

    isolamento: Capacidade
    bloqueio_escrita: Capacidade
    concessao_escrita: Capacidade
    workdir: Capacidade
    extra_dirs: Capacidade
    prompt_longo: Capacidade


@dataclass(frozen=True)
class Ctx:
    """O regime PEDIDO por quem chama. O adaptador honra ou recusa."""

    workdir: Path
    sandbox: bool = True
    allow_write: bool = False
    extra_dirs: tuple[Path, ...] = field(default_factory=tuple)


class BackendAdapter(Protocol):
    """Interface declarada de adaptador de backend (Camada 2 do TCC, IHR).

    Um adaptador diz três coisas: como se chama, que regime sabe honrar, e como
    monta o `argv`. `build_argv` levanta `ContainmentUnsupported` sempre que o
    `ctx` pedir mais contenção do que o `regime` declara saber entregar.
    """

    nome: str
    regime: Regime

    def build_argv(self, model: Model, prompt: str, ctx: Ctx) -> list[str]: ...


def _recusa(nome: str, eixo: str, cap: Capacidade) -> ContainmentUnsupported:
    return ContainmentUnsupported(
        f"{nome}: não sabe honrar '{eixo}' como pedido — {cap.mecanismo}. "
        f"Recusando em vez de executar com contenção menor do que a solicitada."
    )


def available(backend: str) -> bool:
    return shutil.which(backend) is not None


def _stage_prompt(prompt: str, workdir: Path) -> tuple[str, Path | None]:
    """Contorna o limite de linha de comando sem usar stdin.

    Nenhum dos três CLIs lê o prompt de stdin em modo texto: `agy -p`, `copilot
    -p` e `claude -p` querem o prompt como argumento. Para prompts grandes,
    gravamos em arquivo e mandamos o agente lê-lo.

    Devolve (prompt_efetivo, arquivo_encaminhado_ou_None). **Não devolve flag
    alguma**: expor o arquivo ao agente é decisão de cada adaptador, porque é
    ele quem sabe o que o seu CLI aceita. Devolver `--add-dir` daqui foi o
    defeito encontrado em 31/08/2026 — a flag ia parar em argv de CLI que talvez
    não a aceitasse, e só quebrava acima de 24.000 caracteres.
    """
    if len(prompt) <= _ARG_LIMIT:
        return prompt, None

    workdir.mkdir(parents=True, exist_ok=True)
    staged = workdir / f"fpch-prompt-{int(time.time() * 1000)}.md"
    staged.write_text(prompt, encoding="utf-8")
    pointer = (
        f"Read the file at {staged} and execute the instructions it contains. "
        f"The file IS your task. Do not ask for confirmation."
    )
    return pointer, staged


# Assinaturas de falha que os CLIs emitem COM EXIT CODE 0.
#
# Descoberto do jeito caro, em 16/07/2026: `agy --sandbox --mode plan` recusou as
# ferramentas de fetch, imprimiu "jetski: no output produced — a tool required the
# 'unsandboxed' permission that headless mode cannot prompt for, so it was
# auto-denied", e saiu com 0. O router marcou sucesso, e o estágio seguinte do
# canibalizador analisou a MENSAGEM DE ERRO como se fosse o artefato — produzindo
# 6 fichas inteiramente alucinadas.
#
# Lição: `exit == 0` não é contrato de sucesso nestes CLIs. Confiar nele é confiar
# num campo que o fornecedor não prometeu.
_FAILURE_SIGNATURES = (
    "no output produced",
    "auto-denied",
    "headless mode cannot prompt",
    "permission that headless",
    "ineligibletiererror",
    "client no longer supported",
    "quota exceeded",
    "rate limit exceeded",
)


def looks_like_failure(text: str) -> str | None:
    """Devolve a assinatura encontrada, ou None se o texto parece resposta real."""
    low = text.lower()
    for sig in _FAILURE_SIGNATURES:
        if sig in low:
            return sig
    # Resposta curta demais para ser trabalho: quase sempre é recusa ou saudação.
    if len(text.strip()) < 40:
        return "resposta suspeitamente curta"
    return None


# --------------------------------------------------------------------------
# Adaptadores
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class AgyAdapter:
    """Antigravity CLI. Flags verificadas em `agy --help` (31/08/2026).

    O que existe: `--model`, `-p`, `--sandbox`, `--add-dir` (repetível),
    `--dangerously-skip-permissions`, `--mode (accept-edits|plan)`.
    O que NÃO existe: qualquer flag de diretório de trabalho, e qualquer flag de
    negação de ferramenta de escrita.

    Contenção de escrita: o `agy` headless não consegue PERGUNTAR por permissão —
    e por isso auto-nega a ferramenta que a exige (é a origem literal da
    assinatura "auto-denied" em `_FAILURE_SIGNATURES`). Logo, `allow_write=False`
    é honrado pelo par `--sandbox` + ausência de `--dangerously-skip-permissions`.
    Sem o `--sandbox` não sobra mecanismo algum, e aí o adaptador RECUSA.

    `--mode plan` conteria escrita, mas está proibido de propósito: ele também
    recusa as ferramentas de LEITURA (fetch/web) e sinaliza isso com exit 0 —
    ver `_FAILURE_SIGNATURES`. Trocar um defeito silencioso por outro não é
    contenção.
    """

    nome: str = "agy"
    regime: Regime = Regime(
        isolamento=Capacidade(Suporte.FLAG, "--sandbox"),
        bloqueio_escrita=Capacidade(
            Suporte.OMISSAO,
            "--sandbox + ausência de --dangerously-skip-permissions "
            "(headless auto-nega o que exige permissão); exige sandbox=True",
        ),
        concessao_escrita=Capacidade(Suporte.FLAG, "--dangerously-skip-permissions"),
        workdir=Capacidade(Suporte.PROCESSO, "cwd do processo — o agy não tem flag de diretório"),
        extra_dirs=Capacidade(Suporte.FLAG, "--add-dir (repetível)"),
        prompt_longo=Capacidade(Suporte.FLAG, "arquivo encaminhado + --add-dir do workdir"),
    )

    def build_argv(self, model: Model, prompt: str, ctx: Ctx) -> list[str]:
        if not ctx.allow_write and not ctx.sandbox:
            # Único mecanismo de bloqueio de escrita do agy depende do sandbox.
            raise _recusa(self.nome, "bloqueio_escrita", self.regime.bloqueio_escrita)

        if ctx.sandbox and ctx.allow_write:
            # `--sandbox` + `--dangerously-skip-permissions` é um estado que mente
            # sobre si mesmo. A flag de concessão não libera só a escrita: desliga
            # o portão de permissão INTEIRO. E o `--sandbox` do agy não é fronteira
            # de segurança — o próprio projeto registrou o caso em que ele escreveu
            # 530 KB na raiz (issue #36). Anunciar contenção enquanto se desliga a
            # permissão é a mesma classe de falha do `exit 0` que este módulo
            # documenta: um sinal que promete o que não entrega.
            #
            # Não se remove a capacidade — remove-se a mentira. Quem precisa de
            # escrita no agy pede `sandbox=False`, e aí a ausência de contenção
            # está dita em voz alta, no lugar de ficar implícita.
            raise _recusa(self.nome, "concessao_escrita", self.regime.concessao_escrita)

        efetivo, staged = _stage_prompt(prompt, ctx.workdir)

        argv = ["agy", "--model", model.id, "-p", efetivo]
        if ctx.sandbox:
            argv.append("--sandbox")
        if ctx.allow_write:
            argv.append("--dangerously-skip-permissions")
        for d in ctx.extra_dirs:
            argv += ["--add-dir", str(d)]
        if staged is not None:
            argv += ["--add-dir", str(ctx.workdir)]
        return argv


@dataclass(frozen=True)
class CopilotAdapter:
    """GitHub Copilot CLI. Flags verificadas em `copilot --help` (31/08/2026).

    O que existe: `-p/--prompt <text>`, `-C <directory>`, `--add-dir <directory>`
    (repetível), `--deny-tool[=tools...]`, `--disallow-temp-dir`.
    O que NÃO existe: flag de sandbox de processo. O isolamento aqui é por
    negação de ferramenta — o agente não roda shell nem alcança o diretório
    temporário do sistema. É contenção de superfície de ferramenta, e está
    declarada como tal, não como sandbox de sistema operacional.

    Correção de 31/08/2026: o `copilot` ACEITA `--add-dir`, ao contrário do que
    se supunha. `extra_dirs` deixa de ser privilégio do `agy`.
    """

    nome: str = "copilot"
    regime: Regime = Regime(
        isolamento=Capacidade(
            Suporte.FLAG,
            "--deny-tool shell + --disallow-temp-dir (contenção de ferramenta, "
            "não sandbox de SO)",
        ),
        bloqueio_escrita=Capacidade(Suporte.FLAG, "--deny-tool shell --deny-tool write"),
        concessao_escrita=Capacidade(Suporte.OMISSAO, "ausência de --deny-tool write"),
        workdir=Capacidade(Suporte.FLAG, "-C <directory>"),
        extra_dirs=Capacidade(Suporte.FLAG, "--add-dir (repetível)"),
        prompt_longo=Capacidade(Suporte.FLAG, "arquivo encaminhado + --add-dir do workdir"),
    )

    def build_argv(self, model: Model, prompt: str, ctx: Ctx) -> list[str]:
        efetivo, staged = _stage_prompt(prompt, ctx.workdir)

        argv = ["copilot", "-C", str(ctx.workdir)]

        # Allowlist por negação: leitura e busca ficam, nada que mute o disco.
        negadas: list[str] = []
        if ctx.sandbox:
            negadas.append("shell")
        if not ctx.allow_write:
            negadas += ["shell", "write"]
        vistas: set[str] = set()
        for t in negadas:
            if t not in vistas:
                vistas.add(t)
                argv += ["--deny-tool", t]

        if ctx.sandbox:
            argv.append("--disallow-temp-dir")
        for d in ctx.extra_dirs:
            argv += ["--add-dir", str(d)]
        if staged is not None:
            argv += ["--add-dir", str(ctx.workdir)]

        # O prompt fica por último: `--deny-tool` e `--add-dir` são variádicas no
        # parser do copilot, e opção variádica seguida de valor solto engoliria o
        # prompt. Terminar em `-p <texto>` fecha essa porta.
        argv += ["-p", efetivo]
        return argv


@dataclass(frozen=True)
class ClaudeAdapter:
    """Claude Code CLI. Flags verificadas em `claude --help` (31/08/2026).

    O que existe: `-p/--print` (o prompt é POSICIONAL), `--add-dir
    <directories...>`, `--disallowedTools <tools...>`, `--permission-mode
    (acceptEdits|auto|bypassPermissions|manual|dontAsk|plan)`, `--restricted`.
    O que NÃO existe: flag de diretório de trabalho — vai por cwd do processo.

    Defeito corrigido em 31/08/2026: este backend rodava `claude -p <prompt>` e
    mais nada. Ignorava isolamento, diretório de trabalho e diretórios extras,
    concedendo em silêncio muito mais do que o chamador pedia.

    `--restricted` remove as ferramentas que executam comando/código e confina as
    ferramentas de arquivo aos diretórios de trabalho — é o análogo honesto do
    `--sandbox` do agy. Mas ele também recusa `bypassPermissions` e só deixa uma
    PESSOA aprovar escrita; em modo headless não há pessoa. Logo `sandbox=True`
    com `allow_write=True` é incoerente neste CLI, e o adaptador RECUSA em vez de
    fingir que concedeu escrita.
    """

    nome: str = "claude"
    regime: Regime = Regime(
        isolamento=Capacidade(Suporte.FLAG, "--restricted (incompatível com allow_write=True)"),
        bloqueio_escrita=Capacidade(
            Suporte.FLAG,
            "--disallowedTools Write Edit MultiEdit NotebookEdit Bash",
        ),
        concessao_escrita=Capacidade(
            Suporte.FLAG,
            "--permission-mode acceptEdits; indisponível sob --restricted",
        ),
        workdir=Capacidade(Suporte.PROCESSO, "cwd do processo — o claude não tem flag de diretório"),
        extra_dirs=Capacidade(Suporte.FLAG, "--add-dir (repetível)"),
        prompt_longo=Capacidade(Suporte.FLAG, "arquivo encaminhado + --add-dir do workdir"),
    )

    _FERRAMENTAS_DE_ESCRITA = ("Write", "Edit", "MultiEdit", "NotebookEdit", "Bash")

    def build_argv(self, model: Model, prompt: str, ctx: Ctx) -> list[str]:
        if ctx.sandbox and ctx.allow_write:
            raise _recusa(self.nome, "concessao_escrita", self.regime.concessao_escrita)

        efetivo, staged = _stage_prompt(prompt, ctx.workdir)

        argv = ["claude"]
        if ctx.sandbox:
            argv.append("--restricted")
        if ctx.allow_write:
            argv += ["--permission-mode", "acceptEdits"]
        else:
            argv.append("--disallowedTools")
            argv += list(self._FERRAMENTAS_DE_ESCRITA)
        for d in ctx.extra_dirs:
            argv += ["--add-dir", str(d)]
        if staged is not None:
            argv += ["--add-dir", str(ctx.workdir)]

        # `--disallowedTools` e `--add-dir` são variádicas: precisam ser seguidas
        # por outra OPÇÃO, nunca por valor solto. E em `claude` o prompt é
        # argumento POSICIONAL depois de `-p`. Por isso o par `-p <prompt>` é
        # sempre o último — do contrário a variádica engoliria o prompt.
        argv += ["-p", efetivo]
        return argv


# Despacho por dicionário, no lugar da cadeia if/elif. Acrescentar backend passa
# a ser acrescentar uma entrada aqui — e a declaração de regime que vem junto.
ADAPTERS: dict[str, BackendAdapter] = {
    a.nome: a for a in (AgyAdapter(), CopilotAdapter(), ClaudeAdapter())
}


def _run(argv: list[str], timeout_s: int, cwd: Path | None = None) -> tuple[int, str]:
    """Executa sem shell. `argv` é lista — nunca string interpolada."""
    proc = subprocess.run(
        argv,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=timeout_s,
        shell=False,
        cwd=str(cwd) if cwd is not None else None,
    )
    out = (proc.stdout or "").strip()
    if not out and proc.stderr:
        out = proc.stderr.strip()
    return proc.returncode, out


def invoke(
    model: Model,
    prompt: str,
    *,
    workdir: Path,
    timeout_s: int = DEFAULT_TIMEOUT_S,
    sandbox: bool = True,
    allow_write: bool = False,
    extra_dirs: list[Path] | None = None,
) -> Result:
    """Chama o backend do modelo e devolve o resultado normalizado.

    `sandbox=True` e `allow_write=False` são os defaults deliberadamente. Um
    sub-agente que só pesquisa não precisa escrever — e o output dele é DADO
    NÃO-CONFIÁVEL, jamais instrução para o host.

    Levanta `BackendUnavailable` quando o backend é desconhecido ou não está no
    PATH, e `ContainmentUnsupported` (subclasse dele) quando o adaptador não sabe
    honrar o regime pedido. Nos dois casos o router escala para o próximo modelo.
    """
    adapter = ADAPTERS.get(model.backend)
    if adapter is None:
        raise BackendUnavailable(f"backend desconhecido: {model.backend}")
    if not available(model.backend):
        raise BackendUnavailable(f"{model.backend} não está no PATH")

    workdir.mkdir(parents=True, exist_ok=True)
    ctx = Ctx(
        workdir=workdir,
        sandbox=sandbox,
        allow_write=allow_write,
        extra_dirs=tuple(extra_dirs or ()),
    )
    argv = adapter.build_argv(model, prompt, ctx)

    started = time.monotonic()
    try:
        code, text = _run(argv, timeout_s, cwd=workdir)
        latency = time.monotonic() - started

        # exit 0 não basta — ver _FAILURE_SIGNATURES.
        signature = looks_like_failure(text) if code == 0 else None
        ok = code == 0 and bool(text) and signature is None
        if signature:
            error = f"falha semântica (exit 0, mas output é erro): {signature!r} — {text[:200]}"
        elif code != 0:
            error = text[:400]
        else:
            error = None

        return Result(
            ok=ok,
            text=text,
            exit_code=code,
            latency_s=round(latency, 2),
            model=model.id,
            backend=model.backend,
            pool=model.pool,
            error=error,
        )
    except subprocess.TimeoutExpired:
        return Result(
            ok=False,
            text="",
            exit_code=-1,
            latency_s=round(time.monotonic() - started, 2),
            model=model.id,
            backend=model.backend,
            pool=model.pool,
            error=f"timeout após {timeout_s}s",
        )
