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

TERCEIRA REGRA — sucesso é contrato positivo, não ausência de erro (C18):
Até 14/09/2026 o sucesso era `exit 0` MENOS uma lista de frases de erro dos
fornecedores. Lista de negação só reconhece o erro que alguém já viu: se o
fornecedor muda a redação, a falha semântica passa calada — foi assim que uma
mensagem de erro virou "artefato" e rendeu 6 fichas alucinadas em 16/07/2026.
Agora cada chamada exige um contrato de saída verificável e independente de
fornecedor (sentinela com nonce por chamada, ver `FpchContratoSaida`), e falha
FECHADO quando ele não é cumprido. A lista de negação continua, mas como defesa
em profundidade, depois do contrato — nunca no lugar dele.
"""

from __future__ import annotations

import re
import secrets
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

#: Vocabulário fechado do contrato de saída exigido numa chamada. Espelhado em
#: `audit.CONTRACTS` (a trilha valida o campo na construção do evento).
#: `"envelope"` NÃO está aqui de propósito: nenhum adaptador tem hoje o esquema
#: do envelope JSON verificado — ver `Regime.saida_estruturada`.
FPCH_CONTRATO_SENTINELA = "sentinela"
FPCH_CONTRATO_NENHUM = "nenhum"
FPCH_CONTRATOS = (FPCH_CONTRATO_SENTINELA, FPCH_CONTRATO_NENHUM)

#: PREFIXO estável do erro de contrato não cumprido. A mensagem completa
#: acrescenta a assinatura da lista de negação (quando alguma casar) e um trecho
#: saneado da saída — sem isso, cota, autenticação e recusa de ferramenta ficariam
#: indistinguíveis de modelo desobediente (classe de C11). Compare com
#: `startswith`, nunca com igualdade.
FPCH_ERRO_SENTINELA_AUSENTE = "contrato de saída não cumprido: sentinela ausente"


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
    # Contrato de saída EXIGIDO nesta chamada. O default é `"nenhum"`, e não
    # `"sentinela"`, porque um `Result` montado à mão (duplo de teste, chamador
    # programático) não passou por validação alguma — e anunciar contrato não
    # verificado seria a mesma mentira do `exit 0`.
    contract: str = FPCH_CONTRATO_NENHUM
    # Assinatura de `looks_like_failure` que casou numa saída com exit 0, com ou
    # sem contrato cumprido. Campo estruturado para que a trilha separe cota,
    # recusa e desobediência sem reinterpretar o texto de `error`.
    failure_signature: str | None = None


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
    # Envelope estruturado (JSON) com campo de erro documentado. Só vira FLAG
    # quando a flag E o esquema do envelope estão verificados sem invocar modelo;
    # flag presente com esquema não documentado continua AUSENTE (C18).
    saida_estruturada: Capacidade


@dataclass(frozen=True)
class Ctx:
    """O regime PEDIDO por quem chama. O adaptador honra ou recusa."""

    workdir: Path
    sandbox: bool = True
    allow_write: bool = False
    extra_dirs: tuple[Path, ...] = field(default_factory=tuple)
    # Instrução do contrato de saída, anexada pelo `_stage_prompt` ao prompt E ao
    # ponteiro do arquivo encaminhado. Vazia quando o chamador desligou o contrato.
    instrucao_saida: str = ""


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


def _stage_prompt(
    prompt: str, workdir: Path, instrucao_saida: str = ""
) -> tuple[str, Path | None]:
    """Contorna o limite de linha de comando sem usar stdin.

    Nenhum dos três CLIs lê o prompt de stdin em modo texto: `agy -p`, `copilot
    -p` e `claude -p` querem o prompt como argumento. Para prompts grandes,
    gravamos em arquivo e mandamos o agente lê-lo.

    Devolve (prompt_efetivo, arquivo_encaminhado_ou_None). **Não devolve flag
    alguma**: expor o arquivo ao agente é decisão de cada adaptador, porque é
    ele quem sabe o que o seu CLI aceita. Devolver `--add-dir` daqui foi o
    defeito encontrado em 31/08/2026 — a flag ia parar em argv de CLI que talvez
    não a aceitasse, e só quebrava acima de 24.000 caracteres.

    `instrucao_saida` (contrato C18) entra nos DOIS lugares quando o prompt é
    encaminhado: no fim do arquivo, como parte da tarefa, e no fim do ponteiro.
    Só no arquivo, o contrato dependeria de o agente ler o arquivo até o fim;
    só no ponteiro, a instrução ficaria longe do trabalho que ela encerra. O
    limite de linha de comando é medido sobre o prompt JÁ com a instrução.
    """
    completo = prompt + instrucao_saida
    if len(completo) <= _ARG_LIMIT:
        return completo, None

    workdir.mkdir(parents=True, exist_ok=True)
    staged = workdir / f"fpch-prompt-{int(time.time() * 1000)}.md"
    staged.write_text(completo, encoding="utf-8")
    pointer = (
        f"Read the file at {staged} and execute the instructions it contains. "
        f"The file IS your task. Do not ask for confirmation."
    ) + instrucao_saida
    return pointer, staged


# --------------------------------------------------------------------------
# Contrato de saída — sentinela com nonce por chamada (C18)
# --------------------------------------------------------------------------

_PREFIXO_SENTINELA = "FPCH-FIM-"
_NONCE_BYTES = 8  # 16 dígitos hex: adivinhar ou reaproveitar por acaso é inviável
_TRECHO_MAX = 200
#: Decoração markdown tolerada em volta da linha da sentinela. Não enfraquece a
#: prova — quem prova é o nonce —, e evita falso negativo de modelo que formata.
_DECORACAO = "`*_"


@dataclass(frozen=True)
class FpchContratoSaida:
    """Contrato positivo de sucesso: a resposta termina com `FPCH-FIM-<nonce>`.

    **Regra escolhida: a sentinela tem de ser a ÚLTIMA LINHA NÃO-VAZIA**, sozinha
    na linha (espaço nas pontas, CR de CRLF e decoração markdown `*`/`` ` ``/`_`
    tolerados). "Última ocorrência" foi descartada porque aceitaria conteúdo
    DEPOIS da sentinela — e conteúdo depois do fim declarado é justamente a forma
    de um erro que o fornecedor anexa à resposta ("...\\nError: quota exceeded").
    Falhar fechado aqui é o ponto.

    **Defesa contra eco.** Um erro de fornecedor que cita o prompt carrega o
    texto da instrução. Por isso a instrução descreve a sentinela em partes — a
    palavra, o hífen e o código — e nunca contém a cadeia `FPCH-FIM-<nonce>`
    concatenada. Mesmo que contivesse, ela estaria no meio de uma frase, e a
    regra da última linha exige a sentinela sozinha na linha. Um prompt ecoado
    não cumpre o contrato.

    **O que a sentinela prova, e o que não prova.** Prova que o texto devolvido
    veio de um modelo que recebeu ESTA chamada (o nonce é gerado aqui, depois do
    prompt montado) e chegou ao fim da resposta obedecendo à instrução. Não
    prova que o conteúdo acima dela está correto — isso continua com o humano ou
    com hook determinístico, como diz `router.py`.
    """

    nonce: str

    @classmethod
    def novo(cls) -> FpchContratoSaida:
        return cls(nonce=secrets.token_hex(_NONCE_BYTES))

    @property
    def sentinela(self) -> str:
        return f"{_PREFIXO_SENTINELA}{self.nonce}"

    @property
    def instrucao(self) -> str:
        # Em inglês, como os demais prompts enviados aos modelos. A sentinela é
        # descrita em partes para nunca aparecer inteira aqui (defesa contra eco).
        return (
            "\n\n---\n"
            "OUTPUT CONTRACT (set by the harness, mandatory): when your answer is "
            "complete, add one final line containing only a marker made by joining, "
            "with no spaces, the word FPCH-FIM, a hyphen, and this code: "
            f"{self.nonce}\n"
            "Write nothing after that marker line, and do not write the marker "
            "anywhere else.\n"
        )

    @property
    def _token_re(self) -> re.Pattern[str]:
        # Só a sentinela DESTA chamada, como token inteiro: `FPCH-FIM-<nonce>`
        # seguido de mais um dígito hex (17) ou colado a letra/dígito não é ela,
        # e fica intacto.
        return re.compile(rf"(?<![0-9A-Za-z]){re.escape(self.sentinela)}(?![0-9A-Za-z])")

    def aplicar(self, bruto: str) -> tuple[str, bool]:
        """Devolve `(texto_sem_sentinela, cumprido)`.

        Remove a linha validada e qualquer outra ocorrência exata da sentinela
        DESTA chamada — o nonce acabou de ser gerado, então toda ocorrência dele
        é marca do contrato, nunca conteúdo. Cadeias de mesmo formato com OUTRO
        nonce ficam no texto: não há como distingui-las de conteúdo legítimo (os
        testes do próprio FPCH contêm `FPCH-FIM-0123456789abcdef`), e um nonce
        alheio não cumpre contrato futuro, porque cada chamada gera o seu.
        Apagá-las corromperia a ficha de quem canibaliza este repositório.
        """
        linhas = bruto.splitlines(keepends=True)
        i = len(linhas) - 1
        while i >= 0 and not linhas[i].strip():
            i -= 1
        cumprido = i >= 0 and linhas[i].strip().strip(_DECORACAO).strip() == self.sentinela
        corpo = "".join(linhas[:i]) if cumprido else bruto
        return self._token_re.sub("", corpo).strip(), cumprido


#: Acréscimo, em caracteres, que a instrução do contrato soma ao prompt. É
#: constante porque o nonce tem tamanho fixo. A trilha grava `prompt_chars` SEM
#: ele (série comparável com a anterior a C18); somar isto quando
#: `contract == "sentinela"` dá o tamanho efetivamente enviado.
FPCH_CONTRATO_INSTRUCAO_CHARS = len(FpchContratoSaida(nonce="0" * (2 * _NONCE_BYTES)).instrucao)


def _trecho(texto: str, limite: int = _TRECHO_MAX) -> str:
    """Trecho saneado da saída para mensagem de erro: uma linha, sem controle."""
    limpo = "".join(c if c.isprintable() else " " for c in texto)
    return " ".join(limpo.split())[:limite]


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
#
# Desde C18 (14/09/2026) esta lista é SECUNDÁRIA. O contrato positivo é a
# sentinela (`FpchContratoSaida`); as assinaturas rodam DEPOIS dele, sobre o
# texto já sem sentinela, e só pegam o caso em que o modelo cumpriu o contrato
# mas o conteúdo ainda é uma mensagem de erro conhecida. Continuar a lista vale a
# pena; confiar só nela, não — ela não reconhece a redação que ninguém viu ainda.
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
    """Devolve a assinatura encontrada, ou None se o texto parece resposta real.

    Defesa em profundidade, não contrato: roda depois de `FpchContratoSaida`.
    """
    low = text.lower()
    for sig in _FAILURE_SIGNATURES:
        if sig in low:
            return sig
    # HEURÍSTICA, não regra: resposta curta demais para ser trabalho quase sempre
    # é recusa ou saudação. Pode reprovar resposta curta legítima ("sim.") — o
    # custo aceito é uma escalada a mais, preferível a aceitar recusa como sucesso.
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

    Lacuna declarada (C18, `agy --help` da versão 1.2.2 em 14/09/2026): o
    `--help` documenta `--output-format (text, json, stream-json)`, mas NÃO o
    esquema do JSON — nenhum campo de erro nomeado. Verificá-lo exigiria invocar
    modelo. Sem esquema verificado não há parser: o contrato aqui é a sentinela.
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
        saida_estruturada=Capacidade(
            Suporte.AUSENTE,
            "--output-format json existe no --help (1.2.2), mas o esquema do "
            "envelope não é documentado; verificar exigiria invocar modelo",
        ),
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

        efetivo, staged = _stage_prompt(prompt, ctx.workdir, ctx.instrucao_saida)

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

    C18 (`copilot --help`, GitHub Copilot CLI 1.0.83, 14/09/2026): `-s/--silent`
    ("Output only the agent response (no stats), useful for scripting with -p")
    passa a ir sempre. Sem ele, estatísticas impressas depois da resposta
    ficariam depois da sentinela e reprovariam toda chamada pela regra da última
    linha. Lacuna declarada: `--output-format json` (JSONL) existe no `--help`,
    mas o esquema dos eventos não é documentado — sem parser, contrato por
    sentinela.
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
        saida_estruturada=Capacidade(
            Suporte.AUSENTE,
            "--output-format json (JSONL) existe no --help (1.0.83), mas o esquema "
            "dos eventos não é documentado; verificar exigiria invocar modelo",
        ),
    )

    def build_argv(self, model: Model, prompt: str, ctx: Ctx) -> list[str]:
        efetivo, staged = _stage_prompt(prompt, ctx.workdir, ctx.instrucao_saida)

        # `--silent`: só a resposta do agente, sem estatísticas depois dela.
        argv = ["copilot", "-C", str(ctx.workdir), "--silent"]

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

    Lacuna declarada (C18, `claude --help`, Claude Code 2.1.270, 14/09/2026): o
    `--help` documenta `--output-format "json" (single result)`, mas não o
    esquema desse resultado — `is_error`/`result` não aparecem no `--help`, nem
    no `sdk-tools.d.ts` instalado (que descreve só entradas/saídas de
    ferramentas). Verificar o esquema exigiria invocar modelo; logo, sem parser
    de envelope. Contrato por sentinela.
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
        saida_estruturada=Capacidade(
            Suporte.AUSENTE,
            "--output-format json existe no --help (2.1.270), mas o esquema do "
            "resultado não é documentado; verificar exigiria invocar modelo",
        ),
    )

    _FERRAMENTAS_DE_ESCRITA = ("Write", "Edit", "MultiEdit", "NotebookEdit", "Bash")

    def build_argv(self, model: Model, prompt: str, ctx: Ctx) -> list[str]:
        if ctx.sandbox and ctx.allow_write:
            raise _recusa(self.nome, "concessao_escrita", self.regime.concessao_escrita)

        efetivo, staged = _stage_prompt(prompt, ctx.workdir, ctx.instrucao_saida)

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
    output_contract: bool = True,
) -> Result:
    """Chama o backend do modelo e devolve o resultado normalizado.

    `sandbox=True` e `allow_write=False` são os defaults deliberadamente. Um
    sub-agente que só pesquisa não precisa escrever — e o output dele é DADO
    NÃO-CONFIÁVEL, jamais instrução para o host.

    `output_contract=True` (default) exige o contrato de saída por sentinela e
    falha fechado sem ele — ver `FpchContratoSaida`. Desligar é decisão
    deliberada de quem chama, e fica registrada em `Result.contract="nenhum"` e
    no evento `attempt` da trilha. Sem contrato, o sucesso volta a depender só de
    `exit 0` + lista de negação, que é exatamente o modo de falha de C18 — e o
    texto volta INTACTO: sem nonce não há sentinela a remover, e cadeia com
    formato de sentinela é conteúdo.

    Ordem de avaliação: exit≠0 → contrato → lista de negação (defesa em
    profundidade, sobre o texto já sem sentinela). A lista roda também quando o
    contrato falha, só para DIAGNÓSTICO: a assinatura vai para `error` e para
    `Result.failure_signature`, e o veredito continua sendo o do contrato.

    Levanta `BackendUnavailable` quando o backend é desconhecido ou não está no
    PATH, e `ContainmentUnsupported` (subclasse dele) quando o adaptador não sabe
    honrar o regime pedido. Nos dois casos o router escala para o próximo modelo.
    """
    adapter = ADAPTERS.get(model.backend)
    if adapter is None:
        raise BackendUnavailable(f"backend desconhecido: {model.backend}")
    if not available(model.backend):
        raise BackendUnavailable(f"{model.backend} não está no PATH")

    # O nonce nasce aqui, por chamada, depois de o prompt do chamador existir:
    # nenhum texto anterior — nem o prompt, nem a saída de outro estágio — pode
    # trazer a sentinela desta chamada pronta.
    contrato = FpchContratoSaida.novo() if output_contract else None
    nome_contrato = FPCH_CONTRATO_SENTINELA if contrato else FPCH_CONTRATO_NENHUM

    workdir.mkdir(parents=True, exist_ok=True)
    ctx = Ctx(
        workdir=workdir,
        sandbox=sandbox,
        allow_write=allow_write,
        extra_dirs=tuple(extra_dirs or ()),
        instrucao_saida=contrato.instrucao if contrato else "",
    )
    argv = adapter.build_argv(model, prompt, ctx)

    started = time.monotonic()
    try:
        code, bruto = _run(argv, timeout_s, cwd=workdir)
        latency = time.monotonic() - started

        if contrato is not None:
            text, cumprido = contrato.aplicar(bruto)
        else:
            text, cumprido = bruto, True

        signature: str | None = None
        if code != 0:
            ok, error = False, text[:400]
        elif not cumprido:
            # exit 0 com texto plausível e sem sentinela é falha: o contrato é
            # positivo, não depende de reconhecer a redação do erro. A lista de
            # negação roda mesmo assim, para dizer POR QUE — cota, recusa e
            # desobediência não são a mesma falha nem pedem a mesma correção.
            signature = looks_like_failure(text)
            ok, error = False, FPCH_ERRO_SENTINELA_AUSENTE
            if signature:
                error += f"; assinatura: {signature!r}"
            trecho = _trecho(text)
            if trecho:
                error += f" — {trecho}"
        else:
            # Defesa em profundidade — ver _FAILURE_SIGNATURES.
            signature = looks_like_failure(text)
            ok = signature is None
            error = (
                f"falha semântica (exit 0, mas output é erro): {signature!r} — {_trecho(text)}"
                if signature
                else None
            )

        return Result(
            ok=ok,
            text=text,
            exit_code=code,
            latency_s=round(latency, 2),
            model=model.id,
            backend=model.backend,
            pool=model.pool,
            error=error,
            contract=nome_contrato,
            failure_signature=signature,
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
            contract=nome_contrato,
        )
