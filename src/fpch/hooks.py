"""Hooks determinísticos do FPCH — Camada 4 do TCC, a camada que não opina.

Implementa a seção 6 de `docs/decisoes/fatia-funcional-2026-08-31.md`. Um hook é
um comando declarado na política (`policy.HookEntry`), executado sem *shell*, cujo
veredito é decidido por um critério fechado e registrado na trilha.

Três decisões governam este módulo, e nenhuma é de conveniência:

**Critério que não se reconhece devolve `absent`, jamais `pass`.** É aqui que o
princípio da seção 3 da spec vira código executável. A tentação natural — "não sei
avaliar, então deixo passar" — é exatamente a falha que o trabalho inteiro combate:
promessa que falha lida como promessa cumprida. Um verificador sem critério não
reprovou nem aprovou; ele *não verificou*, e o nome disso é `absent`. Pela mesma
razão o conjunto vazio de hooks não é `pass`: ver `overall()`.

**Hook com critério irreconhecível não é sequer executado.** Se o veredito já é
`absent` por construção, rodar o processo só produziria efeito colateral sem valor
de verificação — e um hook pode escrever no disco. Declarar a ausência antes de
gastar o processo é a leitura honesta.

**Falha de execução nunca vira `pass`.** Comando fora do PATH, permissão negada e
timeout são reprovações com razão explícita na `evidence`, não silêncios otimistas.
Isso espelha a lição que `backends.py` já documenta: `exit == 0` de um processo que
nunca rodou não significa sucesso, significa que ninguém olhou.

Sobre o diagnóstico de causa: um hook determinístico que reprova é, por construção,
evidência do lado da infraestrutura ou do artefato produzido — nunca do modelo, que
não participa desta camada. Daí `fault_side = "infraestrutura"` em toda reprovação.
Já `cause_category` só é preenchida quando existe base objetiva (timeout, binário
ausente, erro do sistema operacional ⇒ `"ambiente"`). Um `pytest` que reprova não
diz, por si, se a causa foi epistêmica ou de competência — e a Fase 5 (`improve.py`)
vai ler este campo para decidir se pode propor alteração de política. Campo vazio é
melhor que campo inventado: um rótulo chutado aqui vira, lá adiante, uma proposta
automatizada apoiada em evidência que não existe.

Toda saída de comando é **dado**, nunca instrução. Ela é truncada, gravada e
comparada — jamais interpretada.
"""

from __future__ import annotations

import subprocess
import uuid
from dataclasses import dataclass, field
from pathlib import Path

from . import audit
from .policy import HookEntry, Policy

#: Vereditos possíveis. `absent` não é um meio-termo entre os outros dois: é a
#: declaração de que nenhuma verificação aconteceu.
VERDICTS = ("pass", "fail", "absent")

#: Critérios que este módulo sabe avaliar. Qualquer outro valor — inclusive
#: vazio — cai em `absent`. A lista é fechada de propósito: critério aberto
#: reintroduziria pela porta dos fundos o "não sei avaliar, então passa".
CRITERIOS = ("exit_zero", "saida_vazia")

#: `quando` que casa com qualquer momento.
QUANDO_SEMPRE = "sempre"

#: Limite de `evidence`, em caracteres. Fixado pela seção 6 da spec. A trilha é
#: para auditar veredito, não para arquivar saída de build.
EVIDENCE_MAX = 500

#: Teto de execução de um hook. `policy.BackendsBlock.timeout_s` (600s) é o teto
#: de uma chamada a modelo de terceiro, ordem de grandeza que não serve aqui:
#: hook é verificação local e determinística. `HookEntry` não tem campo próprio
#: de timeout — ver a nota de escopo no fim deste módulo.
DEFAULT_TIMEOUT_S = 120


def _truncate(texto: str, limite: int = EVIDENCE_MAX) -> str:
    """Trunca preservando o teto: o resultado nunca excede `limite` caracteres."""
    if len(texto) <= limite:
        return texto
    return texto[: limite - 3] + "..."


@dataclass(frozen=True)
class HookResult:
    """Veredito de uma execução, com o que a sustenta.

    `evidence` é o que torna o veredito auditável fora deste processo — e é o
    campo que a Fase 5 exige não-vazio para admitir uma proposta. Por isso ela é
    preenchida em todos os caminhos, inclusive nos de sucesso e nos de `absent`.
    """

    nome: str
    verdict: str                        # "pass" | "fail" | "absent"
    evidence: str
    criterio: str
    exit_code: int | None = None
    duration_s: float = 0.0
    cause_category: str | None = None   # só quando há base objetiva
    fault_side: str | None = None       # "infraestrutura" nas reprovações
    #: A trilha aceitou o evento? `audit.write` devolve bool justamente para que
    #: a perda não seja invisível; propagar aqui é o que permite ao chamador
    #: saber que rodou a verificação mas não a registrou.
    trilha_ok: bool = True

    @property
    def ok(self) -> bool:
        return self.verdict == "pass"


@dataclass(frozen=True)
class CheckReport:
    """Relatório agregado de um conjunto de hooks, com veredito geral."""

    verdict: str                              # "pass" | "fail" | "absent"
    results: tuple[HookResult, ...] = ()
    trajectory_id: str = ""
    quando: str | None = None
    #: Razão do veredito geral quando não há hook algum — ver `overall()`.
    nota: str = ""

    @property
    def ok(self) -> bool:
        return self.verdict == "pass"

    @property
    def exit_code(self) -> int:
        """0 se e somente se o veredito geral for `pass`.

        Existe para que `fpch check` seja uma linha: `return report.exit_code`.
        Qualquer outra fiação abriria espaço para a CLI reinterpretar o veredito,
        que é precisamente o que não pode acontecer com `absent`.
        """
        return 0 if self.ok else 1


def overall(results: tuple[HookResult, ...] | list[HookResult]) -> str:
    """Veredito geral: `pass` só se **todos** passarem.

    Com reprovação e ausência no mesmo conjunto, o geral é `fail` — é o mais
    acionável dos dois, e `absent` continua visível item a item no relatório.

    **Conjunto vazio devolve `absent`, não `pass`.** Zero hooks executados é zero
    verificação feita, e a única leitura coerente com o princípio deste módulo é
    a mesma dada ao critério irreconhecível: não houve reprovação, mas também não
    houve aprovação. A alternativa — `pass` para conjunto vazio — faria `fpch
    check` responder "tudo certo" a um projeto sem verificador nenhum, que é
    literalmente a promessa vazia que o TCC condena.
    """
    if not results:
        return "absent"
    if any(r.verdict == "fail" for r in results):
        return "fail"
    if any(r.verdict != "pass" for r in results):
        return "absent"
    return "pass"


def applicable(
    hooks: tuple[HookEntry, ...] | list[HookEntry],
    quando: str | None = None,
) -> tuple[HookEntry, ...]:
    """Hooks aplicáveis a um momento.

    `quando=None` devolve todos. Caso contrário, casa o momento pedido ou o
    curinga `"sempre"` — a comparação é exata e sem normalização inventada:
    momento que não casa é momento que não roda, e adivinhar sinônimos aqui
    faria a política significar coisa diferente do que está escrita nela.
    """
    if quando is None:
        return tuple(hooks)
    return tuple(h for h in hooks if h.quando == quando or h.quando == QUANDO_SEMPRE)


def _avalia(criterio: str, code: int, saida: str) -> tuple[str, str]:
    """Aplica o critério. Devolve `(verdict, evidence)` — ambos já definitivos."""
    if criterio == "exit_zero":
        verdict = "pass" if code == 0 else "fail"
        return verdict, _truncate(f"exit={code}; saída: {saida}" if saida else f"exit={code}; sem saída")
    # "saida_vazia": o código de saída é deliberadamente ignorado — o critério
    # declarado é sobre a saída, e ampliá-lo em silêncio faria a política dizer
    # no código algo diferente do que diz no arquivo. O `exit` fica na evidência
    # para quem auditar, não na decisão.
    verdict = "pass" if not saida else "fail"
    return verdict, _truncate(
        f"exit={code}; sem saída" if not saida else f"exit={code}; saída inesperada: {saida}"
    )


def _executa(cmd: list[str], cwd: Path, timeout_s: float) -> tuple[str, str, int | None, str | None]:
    """Roda o comando. Devolve `(estado, evidence_ou_saida, exit_code, cause)`.

    `estado` é `"ok"` quando o processo rodou até o fim (aí o segundo item é a
    saída bruta), ou `"erro"` quando nem chegou lá (aí o segundo item já é a
    razão, pronta para virar evidência).
    """
    try:
        proc = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout_s,
            shell=False,               # nunca. `argv` é lista, nunca string interpolada.
            cwd=str(cwd),
        )
    except subprocess.TimeoutExpired:
        return (
            "erro",
            f"o hook estourou o timeout de {timeout_s}s e foi interrompido; "
            "sem veredito, isto é reprovação e não aprovação",
            None,
            "ambiente",
        )
    except FileNotFoundError:
        return (
            "erro",
            f"comando não encontrado no PATH: {cmd[0]!r}; "
            "hook declarado mas não executável não é hook que passou",
            None,
            "ambiente",
        )
    except (PermissionError, NotADirectoryError, OSError) as exc:
        return (
            "erro",
            f"o sistema operacional recusou a execução de {cmd[0]!r}: {exc}",
            None,
            "ambiente",
        )

    saida = (proc.stdout or "").strip()
    erro = (proc.stderr or "").strip()
    if erro:
        # stderr conta como saída: um hook cujo critério é `saida_vazia` e que
        # despeja aviso em stderr não está silencioso, está reclamando.
        saida = f"{saida}\n{erro}".strip()
    return "ok", saida, proc.returncode, None


def run_hook(
    hook: HookEntry,
    *,
    cwd: Path | str | None = None,
    timeout_s: float = DEFAULT_TIMEOUT_S,
    trajectory_id: str | None = None,
    audit_path: Path | None = None,
    label: str | None = None,
    emitir: bool = True,
) -> HookResult:
    """Executa um hook e devolve seu veredito, emitindo um evento `verify`.

    `cwd` e `timeout_s` são explícitos por princípio: hook que herda o diretório
    de trabalho de quem por acaso o chamou é hook cujo resultado não é
    reproduzível, e processo sem teto é processo que pendura a suíte inteira.

    `emitir=False` existe para quem quiser compor o veredito sem tocar a trilha
    (a Fase 5 relendo uma proposta, por exemplo). O padrão é emitir: a seção 6 da
    spec é explícita em que esta é a primeira vez que um validador deixa rastro.
    """
    destino = Path(cwd) if cwd is not None else Path.cwd()
    tid = trajectory_id or f"hook-{uuid.uuid4().hex[:12]}"
    criterio = (hook.criterio or "").strip()
    cmd = list(hook.cmd)

    cause: str | None = None
    lado: str | None = None
    exit_code: int | None = None
    inicio = _monotonic()

    if criterio not in CRITERIOS:
        # O ponto do módulo. Nada é executado: o veredito já é `absent`, e rodar
        # o comando só produziria efeito colateral sem valor de verificação.
        verdict = "absent"
        evidence = _truncate(
            f"critério {hook.criterio!r} não reconhecido (conhecidos: {', '.join(CRITERIOS)}); "
            "verificador sem critério é verificação AUSENTE, nunca aprovada — comando não executado"
        )
    elif not cmd:
        verdict = "absent"
        evidence = _truncate(
            f"hook {hook.nome!r} declara critério {criterio!r} mas nenhum comando; "
            "não há o que executar, logo não há o que aprovar"
        )
    else:
        estado, bruto, exit_code, cause_exec = _executa(cmd, destino, timeout_s)
        if estado == "erro":
            verdict, evidence = "fail", _truncate(bruto)
            cause = cause_exec
        else:
            verdict, evidence = _avalia(criterio, exit_code or 0, bruto)

    duracao = round(_monotonic() - inicio, 4)

    if verdict == "fail":
        # Por construção: nesta camada o modelo não participa. O que reprovou foi
        # o artefato produzido ou o ambiente que o hospeda.
        lado = "infraestrutura"

    trilha_ok = True
    if emitir:
        trilha_ok = audit.write(
            audit.Event(
                event="verify",
                trajectory_id=tid,
                seq=audit.next_seq(tid),
                label=label,
                component=hook.nome,
                verdict=verdict,
                evidence=evidence,
                # `exit_code` e `latency_s` não são exclusivos de `attempt`: aqui
                # são o dado objetivo que permite reconferir o veredito sem
                # reexecutar o hook.
                exit_code=exit_code,
                latency_s=duracao,
                cause_category=cause,
                fault_side=lado,
            ),
            path=audit_path,
        )

    return HookResult(
        nome=hook.nome,
        verdict=verdict,
        evidence=evidence,
        criterio=criterio,
        exit_code=exit_code,
        duration_s=duracao,
        cause_category=cause,
        fault_side=lado,
        trilha_ok=trilha_ok,
    )


def run_all(
    hooks: tuple[HookEntry, ...] | list[HookEntry],
    *,
    quando: str | None = QUANDO_SEMPRE,
    cwd: Path | str | None = None,
    timeout_s: float = DEFAULT_TIMEOUT_S,
    trajectory_id: str | None = None,
    audit_path: Path | None = None,
    label: str | None = None,
    emitir: bool = True,
) -> CheckReport:
    """Roda o conjunto aplicável a um momento e agrega o veredito.

    Todos os hooks rodam mesmo depois do primeiro `fail`: parar no primeiro erro
    daria um relatório que esconde os demais, e o valor da trilha está justamente
    em ter o veredito de cada componente, não só o do primeiro que quebrou.

    Todos compartilham o mesmo `trajectory_id`, de modo que `seq` ordene a
    verificação inteira como um episódio único — e um chamador de nível acima
    pode passar o `trajectory_id` da trajetória que produziu o artefato, amarrando
    "o que foi gerado" a "o que foi verificado" na mesma cadeia.
    """
    selecionados = applicable(hooks, quando)
    tid = trajectory_id or f"check-{uuid.uuid4().hex[:12]}"

    resultados = tuple(
        run_hook(
            h,
            cwd=cwd,
            timeout_s=timeout_s,
            trajectory_id=tid,
            audit_path=audit_path,
            label=label,
            emitir=emitir,
        )
        for h in selecionados
    )

    nota = ""
    if not resultados:
        nota = (
            f"nenhum hook aplicável a quando={quando!r} — verificação AUSENTE, não aprovada. "
            "Declare ao menos um [[hooks]] na política para que `fpch check` possa afirmar algo."
        )

    return CheckReport(
        verdict=overall(resultados),
        results=resultados,
        trajectory_id=tid,
        quando=quando,
        nota=nota,
    )


def check(
    policy: Policy,
    *,
    quando: str | None = QUANDO_SEMPRE,
    cwd: Path | str | None = None,
    timeout_s: float = DEFAULT_TIMEOUT_S,
    trajectory_id: str | None = None,
    audit_path: Path | None = None,
    label: str | None = None,
) -> CheckReport:
    """Conveniência: roda os hooks de uma `Policy` já carregada.

    Não carrega política sozinha de propósito — quem resolve a cascata (incluindo
    `--policy`) é a CLI, e um `policy.load()` escondido aqui dentro faria a
    verificação rodar com uma política diferente da que a CLI acabou de exibir.
    """
    return run_all(
        policy.hooks,
        quando=quando,
        cwd=cwd,
        timeout_s=timeout_s,
        trajectory_id=trajectory_id,
        audit_path=audit_path,
        label=label,
    )


def format_report(report: CheckReport) -> str:
    """Relatório em texto, uma linha por hook mais o veredito geral.

    A evidência aparece só nos vereditos que não passaram: quem lê um `check`
    quer ver o que quebrou, e despejar a saída de todo hook aprovado enterraria
    exatamente essa informação.
    """
    marca = {"pass": "ok  ", "fail": "FALHA", "absent": "AUSENTE"}
    linhas = [f"=== fpch check (quando={report.quando!r}, trajetória={report.trajectory_id}) ==="]
    for r in report.results:
        linhas.append(f"  [{marca.get(r.verdict, r.verdict)}] {r.nome} (criterio={r.criterio!r}, {r.duration_s}s)")
        if r.verdict != "pass":
            linhas.append(f"        {r.evidence}")
        if not r.trilha_ok:
            linhas.append("        AVISO: veredito NÃO registrado na trilha de auditoria.")
    if report.nota:
        linhas.append(f"  {report.nota}")
    linhas.append(f"veredito geral: {report.verdict}")
    return "\n".join(linhas)


def _monotonic() -> float:
    """Relógio monotônico isolado numa função para poder ser substituído em teste."""
    import time

    return time.monotonic()
