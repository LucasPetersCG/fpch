"""CLI do FPCH.

    fpch [--policy P] models         catálogo + o que está instalado
    fpch [--policy P] plan hard      mostra a cadeia de escalada, sem gastar cota
    fpch [--policy P] ask standard "prompt"   roteia e imprime
    fpch config --dump               política efetiva, bloco a bloco, com a origem
    fpch check                       roda os hooks determinísticos da política
    fpch audit                       para onde a cota foi
    fpch audit --verify              confere o encadeamento de hashes da trilha
    fpch audit --causes              causa raiz e fonte, agregadas
    fpch audit --process             marcos do processo de falha, por trajetória
    fpch improve                     lê a trilha e PROPÕE mudança de política (não aplica)
    fpch --policy P improve --apply <id>   autoriza uma proposta, no arquivo que você nomeou
    fpch quote-check <ficha> <fonte> trechos entre aspas existem na fonte?
    fpch canib add <url> [--note]    enfileira alvo
    fpch canib list                  fila
    fpch canib accept <id>           aprovação humana (obrigatória)
    fpch canib run <id>              extrai + propõe → ficha em docs/

**A política é carregada uma vez, em `main()`, antes de qualquer subcomando.**
`--policy` é global e vem ANTES do subcomando (`fpch --policy p.toml plan hard`),
porque a política não é opção de um comando: é o contexto em que todos rodam.
A cascata inteira (linha de comando → `FPCH_POLICY` → `./fpch.policy.toml` →
`~/.fpch/policy.toml` → default embutido) é resolvida por `policy.load()`; aqui
só se passa adiante o caminho pedido na linha de comando.

Política malformada **derruba o processo antes de qualquer trabalho**, com código
de saída 2 e a mensagem em `stderr`. Códigos de saída, por convenção deste CLI:

    0  o comando fez o que prometeu
    1  o comando rodou e o resultado reprovou (hook falhou, citação fabricada…)
    2  o comando não chegou a rodar (política ilegível, uso errado)

Separar 1 de 2 importa: um `fpch check` que devolve 1 disse algo sobre o projeto;
um que devolve 2 não chegou a olhar para ele. Confundir os dois faria uma política
quebrada parecer uma verificação reprovada — e o inverso, pior ainda.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from . import audit, backends, cannibalize, citations, hooks, improve, models, policy, quotes, router
from .models import TaskClass

REPO_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_WORKDIR = REPO_ROOT / ".fpch-work"
DEFAULT_FICHE_DIR = REPO_ROOT / "docs" / "referencias" / "fichamentos"

#: Código de saída para "não chegou a rodar" — ver o docstring do módulo.
EXIT_USO = 2


def _cmd_models(args: argparse.Namespace) -> int:
    print(f"(política: {args.policy_obj.fonte})")
    by_pool: dict[str, list] = {}
    for m in args.catalog:
        by_pool.setdefault(m.pool.value, []).append(m)
    for pool, models in by_pool.items():
        installed = backends.available(models[0].backend)
        flag = "" if installed else "  [BACKEND AUSENTE]"
        print(f"\n{pool}{flag}")
        for m in models:
            classes = ",".join(c.value for c in m.good_for)
            print(f"  {m.id:<32} pot={m.power} custo={m.cost}  [{classes}]")
            if m.notes:
                print(f"    └ {m.notes}")
    print()
    return 0


def _cmd_plan(args: argparse.Namespace) -> int:
    # `router.plan` lê o catálogo em vigor via `models.candidates()`, e `main()`
    # já colocou em vigor o catálogo derivado da política. `max_escalations` vem
    # do bloco [escalada] — é o que faz `--policy` mudar o tamanho da cadeia.
    try:
        chain = router.plan(
            TaskClass(args.task_class),
            max_escalations=args.policy_obj.escalada.max_escalations,
        )
    except router.NoBackendAvailable as exc:
        print(f"erro: {exc}", file=sys.stderr)
        return 1
    print(f"(política: {args.policy_obj.fonte})")
    print(f"cadeia para '{args.task_class}':")
    for i, m in enumerate(chain):
        tag = "preferido" if i == 0 else f"fallback {i}"
        print(f"  {i + 1}. {m.id:<32} ({m.pool.value})  [{tag}]")
    return 0


def _cmd_ask(args: argparse.Namespace) -> int:
    prompt = args.prompt
    if prompt == "-":
        prompt = sys.stdin.read()
    if args.file:
        prompt = Path(args.file).read_text(encoding="utf-8")

    DEFAULT_WORKDIR.mkdir(parents=True, exist_ok=True)
    # `--timeout` ausente herda [backends].timeout_s da política; passado na
    # linha de comando, vence — quem digita o número está pedindo aquele número.
    timeout_s = args.timeout if args.timeout is not None else args.policy_obj.backends.timeout_s
    try:
        result = router.route(
            TaskClass(args.task_class),
            prompt,
            workdir=DEFAULT_WORKDIR,
            model_id=args.model,
            max_escalations=args.policy_obj.escalada.max_escalations,
            timeout_s=timeout_s,
            allow_write=args.allow_write,
            label=args.label,
        )
    except router.NoBackendAvailable as exc:
        print(f"erro: {exc}", file=sys.stderr)
        return 1

    if args.json:
        print(json.dumps(
            {
                "ok": result.ok,
                "model": result.model,
                "pool": result.pool.value,
                "latency_s": result.latency_s,
                "text": result.text,
                "error": result.error,
            },
            ensure_ascii=False,
        ))
    else:
        if not result.ok:
            print(f"[falha] {result.model}: {result.error}", file=sys.stderr)
            return 1
        print(result.text)
        print(f"\n--- {result.model} ({result.pool.value}) {result.latency_s}s", file=sys.stderr)
    return 0 if result.ok else 1


def _cmd_cite_check(args: argparse.Namespace) -> int:
    text = Path(args.file).read_text(encoding="utf-8")
    root = Path(args.root)
    if not root.exists():
        print(f"raiz não existe: {root}", file=sys.stderr)
        return 1
    report = citations.validate(text, root)
    print(report.summary())
    for c in report.failures():
        print(f"  {c.verdict:<20} {c.path}:{c.line}  {c.detail}")
    # Falha o processo quando há fabricação: permite usar como hook em CI/pre-commit.
    return 1 if report.fabricated else 0


def _cmd_quote_check(args: argparse.Namespace) -> int:
    fiche, source = Path(args.fiche), Path(args.source)
    for p in (fiche, source):
        if not p.exists():
            print(f"não existe: {p}", file=sys.stderr)
            return 1
    report = quotes.validate_files(fiche, source)
    print(report.summary())
    for q in report.absent:
        print(f"  AUSENTE      {q.text[:80]}…")
    for q in report.approximate:
        print(f"  aproximado   {q.detail}  |  {q.text[:60]}…")
    for p in report.bad_pages:
        print(f"  PÁGINA       p. {p.page} — {p.detail}")
    if args.emit_block:
        print()
        print(quotes.render_block(report, source.name))
    # Só trecho ausente e página inexistente reprovam. "Aproximado" costuma ser
    # ruído da extração do PDF — reprovar por isso seria acusar o inocente, que é
    # exatamente o erro documentado em citations.py.
    return 1 if report.failed else 0


def _cmd_config(args: argparse.Namespace) -> int:
    """`fpch config --dump` — seção 5.4 da fatia funcional.

    Só imprime o que `policy.dump()` produz: valor e origem por bloco, mais os
    blocos imutáveis com a razão de cada um. Reimplementar a formatação aqui
    abriria a chance de a CLI mostrar uma política diferente da que de fato está
    valendo, que é o oposto do propósito do comando.
    """
    if not args.dump:
        print("uso: fpch [--policy CAMINHO] config --dump", file=sys.stderr)
        return EXIT_USO
    print(policy.dump(args.policy_obj))
    return 0


def _cmd_check(args: argparse.Namespace) -> int:
    """`fpch check` — seção 6 da fatia funcional.

    **Decisão registrada (item 6 da Onda 2A): política sem nenhum hook aplicável
    devolve código de saída DIFERENTE de zero**, com a razão explícita na tela.

    A alternativa considerada era sair com 0 imprimindo "nenhum hook declarado".
    Foi recusada: `fpch check` só existe para afirmar algo sobre o projeto, e
    saída zero é lida — por humano e por CI — como "verificado e aprovado". Um
    projeto sem verificador nenhum não foi aprovado; ele não foi verificado, e o
    nome disso é `absent`. `hooks.overall()` já decide exatamente assim para o
    conjunto vazio, e `hooks.py` é explícito em que a CLI não deve reinterpretar
    o veredito — daí `return report.exit_code` sem nenhuma tradução no caminho.
    É a seção 3 do contrato aplicada ao próprio verificador: promessa que falha
    jamais é lida como promessa cumprida, e promessa que nunca foi feita também
    não.

    Quem quiser um `check` que não reprove por ausência declara um hook na
    política — que é precisamente o comportamento que se quer induzir.
    """
    quando = None if args.todos else args.quando
    report = hooks.check(
        args.policy_obj,
        quando=quando,
        cwd=args.cwd,
        timeout_s=args.timeout,
        label=args.label,
    )
    print(hooks.format_report(report))
    if not report.results:
        print(
            "nenhum hook declarado na política em vigor "
            f"({args.policy_obj.fonte}) para quando="
            f"{'qualquer momento' if quando is None else repr(quando)}.\n"
            "Nada foi verificado — isto NÃO é aprovação. Declare um bloco [[hooks]] "
            "com nome/cmd/quando/criterio para que `fpch check` possa afirmar algo.",
            file=sys.stderr,
        )
    return report.exit_code


def _cmd_improve(args: argparse.Namespace) -> int:
    """`fpch improve` — seção 8 da fatia funcional, o laço com portão.

    Dois modos, e a separação entre eles é o ponto do comando: **sem `--apply`**,
    lê a trilha e no máximo grava uma proposta em disco, sem tocar em política
    nenhuma; **com `--apply ID`**, edita o arquivo de política que o humano nomeou
    em `--policy`.

    `--apply` sem `--policy` é erro de uso (código 2), não um default conveniente.
    A cascata de `policy.load()` decide qual política *vale*; deixá-la decidir qual
    arquivo é *editado* faria o laço escrever num arquivo que ninguém apontou — e o
    portão de aprovação humana perderia a metade que importa, que é o humano saber
    exatamente o que assinou.
    """
    trilha = Path(args.trilha) if args.trilha else None
    propostas = Path(args.propostas) if args.propostas else None

    if args.apply:
        if not args.policy:
            print(
                "erro de uso: `improve --apply` escreve na política e por isso exige que você "
                "nomeie o arquivo:\n"
                f"    fpch --policy <arquivo.toml> improve --apply {args.apply}\n"
                "A cascata decide qual política vale, não qual arquivo é editado.",
                file=sys.stderr,
            )
            return EXIT_USO
        resultado = improve.aplicar(
            args.apply,
            policy_path=Path(args.policy),
            trilha=trilha,
            propostas_dir=propostas,
        )
        saida = sys.stdout if resultado.ok else sys.stderr
        print(improve.format_resultado(resultado), file=saida)
        return resultado.exit_code

    analise = improve.propor(args.policy_obj, trilha=trilha, propostas_dir=propostas,
                             label=args.label)
    print(improve.format_analise(analise))
    return analise.exit_code


def _cmd_audit(args: argparse.Namespace) -> int:
    trilha = Path(args.trilha) if getattr(args, "trilha", None) else None

    verify = getattr(args, "verify", False)
    causes = getattr(args, "causes", False)
    process = getattr(args, "process", False)

    # --verify, --causes e --process são mutuamente exclusivas. Fazer as duas
    # coisas pela metade (spec da fatia, §3) é pior do que escolher uma — e o
    # argparse recusar a combinação devolveria código 2 ("não chegou a
    # rodar"), errado aqui: o comando é válido, só ambíguo. A CLI resolve por
    # uma ordem de precedência fixa (verify > causes > process — íntegro
    # antes de citável) e AVISA em stderr qual flag prevaleceu, para que
    # ninguém leia a saída de uma flag pensando que é de outra.
    if verify and (causes or process):
        outras = ", ".join(n for n, v in (("--causes", causes), ("--process", process)) if v)
        print(f"fpch: --verify e {outras} foram passados juntos — --verify prevalece.",
              file=sys.stderr)
        return _cmd_audit_verify(trilha)
    if verify:
        return _cmd_audit_verify(trilha)

    if causes and process:
        print("fpch: --causes e --process foram passados juntos — --causes prevalece.",
              file=sys.stderr)
        return _cmd_audit_causes(trilha)
    if causes:
        return _cmd_audit_causes(trilha)
    if process:
        return _cmd_audit_process(trilha)

    s = audit.summary(trilha)
    if not s["total_calls"]:
        print("sem registros ainda.")
        return 0
    print(f"chamadas totais: {s['total_calls']}\n")
    print(f"{'pool':<18}{'chamadas':>10}{'falhas':>8}{'lat.méd':>10}{'chars':>10}")
    for pool, agg in sorted(s["by_pool"].items()):
        print(f"{pool:<18}{agg['calls']:>10}{agg['fail']:>8}{agg['latency_avg']:>9}s{agg['chars_out']:>10}")
    return 0


def _cmd_audit_verify(trilha: Path | None) -> int:
    """`fpch audit --verify` — §4.2 da fatia funcional.

    Chama `audit.verify_chain()` (não reimplementa a verificação aqui) e traduz
    o par `(íntegra, índice)` num veredito legível. Duas armadilhas evitadas:

    1. `verify_chain` devolve `(True, None)` tanto para "cadeia íntegra e
       conferida" quanto para "trilha vazia ou inexistente" — são a mesma coisa
       para quem só quer saber se algo está quebrado, mas são vereditos
       DIFERENTES para quem pergunta "isto prova integridade?". Ausência de
       dado não é integridade confirmada (seção 3 do contrato: promessa que
       falha jamais é lida como promessa cumprida — e aqui a promessa nem chegou
       a ser testada). Por isso a contagem de linhas via `audit.read_all()`
       decide qual dos dois vereditos é impresso.
    2. O índice do primeiro defeito é a posição física (linha não vazia, a
       partir de zero) — documentado no docstring de `verify_chain` — e é
       repetido aqui explicitamente para que quem lê a tela não precise abrir
       `audit.py` para saber o que o número significa.
    """
    ok, indice = audit.verify_chain(trilha)

    if not ok:
        print(
            f"trilha QUEBRADA — primeiro defeito no índice {indice} "
            "(conta linhas não vazias, na ordem física do arquivo, a partir de zero).",
            file=sys.stderr,
        )
        return 1

    conferidas = len(audit.read_all(trilha))
    if conferidas == 0:
        print(
            "trilha vazia ou inexistente — nada foi conferido. "
            "Isto NÃO é integridade confirmada, é ausência de dado.",
            file=sys.stderr,
        )
        return 1

    print(f"trilha íntegra — {conferidas} linha(s) conferida(s).")
    return 0


def _cmd_audit_causes(trilha: Path | None) -> int:
    """`fpch audit --causes` — causa raiz e fonte, agregadas por `audit.summary()`.

    Duas tabelas, cada uma com um balde `"?"` para vocabulário fora do
    vocabulário vigente (linha antiga, esquema anterior). O balde nunca é
    escondido: esconder o que não se entendeu é o oposto do que esta trilha
    existe para fazer. Trilha sem nada classificado não é erro — é ausência
    de dado — por isso a saída é uma frase honesta e o código é 0.
    """
    s = audit.summary(trilha)
    by_cause: dict = s["by_cause"]
    by_source: dict = s["by_source"]

    if not by_cause and not by_source:
        print("nenhuma causa registrada ainda nesta trilha.")
        return 0

    print("causa raiz")
    if not by_cause:
        print("  nenhuma causa registrada ainda nesta trilha.")
    else:
        print(f"  {'categoria / subtipo':<32}{'total':>7}")
        for categoria in (*audit.CAUSE_TAXONOMY, "?"):
            balde = by_cause.get(categoria)
            if not balde:
                continue
            print(f"  {categoria:<32}{balde['total']:>7}")
            subtipos = balde["subtypes"]
            ordem = audit.CAUSE_TAXONOMY.get(categoria, ())
            chaves = [s2 for s2 in ordem if s2 in subtipos]
            chaves += sorted(s2 for s2 in subtipos if s2 not in ordem)
            for subtipo in chaves:
                print(f"    {subtipo:<30}{subtipos[subtipo]:>7}")

    print()
    print("fonte")
    if not by_source:
        print("  nenhuma fonte registrada ainda nesta trilha.")
    else:
        print(f"  {'source / source_ref':<32}{'total':>7}")
        for fonte in (*audit.SOURCES, "?"):
            balde = by_source.get(fonte)
            if not balde:
                continue
            print(f"  {fonte:<32}{balde['total']:>7}")
            for ref in sorted(balde["refs"]):
                print(f"    {ref:<30}{balde['refs'][ref]:>7}")

    return 0


#: Nota de granularidade das derivadas de `audit.failure_process()`. Reaproveita
#: a formulação do docstring de `failure_process` (fonte da verdade) em vez de
#: inventar uma nova — é o que impede alguém de ler `fix_window` /
#: `observability_lag` como se fossem a métrica de Zhao et al. (2026).
_NOTA_GRANULARIDADE = (
    "nota de granularidade: fix_window e observability_lag são medidos em "
    "EVENTOS DA TRILHA, não em turnos internos do agente. Zhao et al. (2026) "
    "medem passos de raciocínio-e-ação dentro de uma execução; o FPCH "
    "registra uma linha por chamada de backend. A adoção aqui é da estrutura "
    "conceitual — três marcos e duas derivadas —, não da unidade de medida. "
    "Comparar os números desta tabela com os números do artigo seria erro de "
    "leitura."
)

#: Legenda da marca de trajetória remarcada, impressa só quando alguma linha a usa.
_NOTA_REMARCADO = (
    "* marco remarcado nesta trajetória — o último valor anotado prevalece; "
    "mudar de opinião sobre um rótulo retrospectivo é legítimo, escondê-lo não é."
)


def _fmt_marco(valor: int | None) -> str:
    """Marco não anotado vira `—`, nunca `0` — zero é medida, traço é ausência dela."""
    return "—" if valor is None else str(valor)


def _cmd_audit_process(trilha: Path | None) -> int:
    """`fpch audit --process` — marcos do processo de falha, por `audit.failure_process()`.

    Uma linha por trajetória anotada: os `seq` de `t_err`/`t_lock`/`t_obs` e as
    duas derivadas. `observability_lag` pode ser NEGATIVO — pela fórmula
    `t_obs − t_lock`, valor negativo significa que o sinal do erro já era
    observável ANTES do ponto de irrecuperabilidade, achado legítimo e não um
    defeito de cálculo. Por isso o valor é impresso com sinal, nunca com
    `abs()`, e a legenda de rodapé explica o sentido do negativo.
    """
    marcos = audit.failure_process(trilha)
    if not marcos:
        print("nenhuma trajetória com marco anotado ainda nesta trilha.")
        return 0

    algum_remarcado = False
    print(f"{'trajetória':<24}{'t_err':>7}{'t_lock':>8}{'t_obs':>7}"
          f"{'fix_window':>12}{'obs_lag':>10}")
    for tid in sorted(marcos):
        m = marcos[tid]
        marca = "*" if m["remarcado"] else ""
        if m["remarcado"]:
            algum_remarcado = True
        print(f"{tid:<24}{_fmt_marco(m['t_err']):>7}{_fmt_marco(m['t_lock']):>8}"
              f"{_fmt_marco(m['t_obs']):>7}{_fmt_marco(m['fix_window']):>12}"
              f"{_fmt_marco(m['observability_lag']):>10}{marca}")

    print()
    if algum_remarcado:
        print(_NOTA_REMARCADO)
    print(
        "nota: observability_lag negativo significa que o sinal do erro era "
        "observável ANTES do ponto de irrecuperabilidade (t_lock) — achado "
        "legítimo, não defeito de cálculo."
    )
    print(_NOTA_GRANULARIDADE)
    return 0


def _cmd_canib(args: argparse.Namespace) -> int:
    if args.canib_cmd == "add":
        t = cannibalize.add(args.url, args.note or "", args.focus)
        print(f"{t.id}  [{t.status}]  {t.focus:<9} {t.kind:<6} {t.url}")
        print(f"\naprove com:  fpch canib accept {t.id}")
        return 0

    if args.canib_cmd == "runall":
        pending = cannibalize.listing(cannibalize.STATUS_ACCEPTED)
        if args.focus:
            pending = [t for t in pending if t.focus == args.focus]
        if not pending:
            print("nada aprovado para rodar.", file=sys.stderr)
            return 1
        DEFAULT_WORKDIR.mkdir(parents=True, exist_ok=True)
        ok = fail = 0
        for t in pending:
            print(f"\n=== {t.id} [{t.focus}] {t.url}", file=sys.stderr)
            try:
                path = cannibalize.run(
                    t,
                    workdir=DEFAULT_WORKDIR,
                    fiche_dir=DEFAULT_FICHE_DIR,
                    extract_model=args.extract_model,
                    propose_model=args.propose_model,
                )
                print(f"    ok → {path}", file=sys.stderr)
                ok += 1
            except RuntimeError as exc:
                # Um alvo hostil ou offline não pode derrubar a fila inteira.
                print(f"    FALHOU: {exc}", file=sys.stderr)
                fail += 1
        print(f"\n{ok} ok, {fail} falhas", file=sys.stderr)
        return 0 if fail == 0 else 1

    if args.canib_cmd == "list":
        rows = cannibalize.listing(args.status)
        if not rows:
            print("fila vazia.")
            return 0
        for t in rows:
            note = f"  — {t.note}" if t.note else ""
            print(f"{t.id}  {t.status:<9} {t.kind:<6} {t.url}{note}")
            if t.fiche_path:
                print(f"          └ {t.fiche_path}")
        return 0

    if args.canib_cmd in ("accept", "reject"):
        status = cannibalize.STATUS_ACCEPTED if args.canib_cmd == "accept" else cannibalize.STATUS_REJECTED
        t = cannibalize.set_status(args.id, status)
        if not t:
            print(f"id não encontrado: {args.id}", file=sys.stderr)
            return 1
        print(f"{t.id} → {t.status}")
        return 0

    if args.canib_cmd == "run":
        matches = [t for t in cannibalize.listing() if t.id == args.id]
        if not matches:
            print(f"id não encontrado: {args.id}", file=sys.stderr)
            return 1
        target = matches[0]
        if target.status != cannibalize.STATUS_ACCEPTED:
            print(
                f"alvo está '{target.status}', não '{cannibalize.STATUS_ACCEPTED}'.\n"
                f"aprovação humana é obrigatória:  fpch canib accept {target.id}",
                file=sys.stderr,
            )
            return 1
        DEFAULT_WORKDIR.mkdir(parents=True, exist_ok=True)
        print(f"canibalizando {target.url} ...", file=sys.stderr)
        try:
            path = cannibalize.run(
                target,
                workdir=DEFAULT_WORKDIR,
                fiche_dir=DEFAULT_FICHE_DIR,
                extract_model=args.extract_model,
                propose_model=args.propose_model,
            )
        except RuntimeError as exc:
            print(f"erro: {exc}", file=sys.stderr)
            return 1
        print(f"\nficha: {path}")
        print("⚠️  Tier D até você conferir contra a fonte primária.")
        return 0

    return 1


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="fpch", description="FPCH — roteador multi-modelo e canibalizador")
    # Global e ANTES do subcomando: a política é o contexto de todos eles, não
    # opção de um. `policy.load()` resolve a cascata completa; daqui só sobe o
    # nível 1 (linha de comando).
    p.add_argument(
        "--policy",
        metavar="CAMINHO",
        help="arquivo de política TOML (nível 1 da cascata; vence FPCH_POLICY, "
             "./fpch.policy.toml, ~/.fpch/policy.toml e o default embutido)",
    )
    sub = p.add_subparsers(dest="cmd", required=True)

    sub.add_parser("models", help="lista o catálogo por pool de cota").set_defaults(fn=_cmd_models)

    sp = sub.add_parser("plan", help="mostra a cadeia de escalada sem gastar cota")
    sp.add_argument("task_class", choices=[c.value for c in TaskClass])
    sp.set_defaults(fn=_cmd_plan)

    sa = sub.add_parser("ask", help="roteia um prompt")
    sa.add_argument("task_class", choices=[c.value for c in TaskClass])
    sa.add_argument("prompt", nargs="?", default="-", help="prompt, ou '-' para stdin")
    sa.add_argument("--file", help="lê o prompt deste arquivo")
    sa.add_argument("--model", help="força um modelo e desliga a escalada")
    sa.add_argument("--timeout", type=int, default=None,
                    help=f"segundos (default: [backends].timeout_s da política, "
                         f"hoje {backends.DEFAULT_TIMEOUT_S} no embutido)")
    sa.add_argument("--allow-write", action="store_true", help="⚠️ permite que o agente escreva")
    sa.add_argument("--label", help="rótulo para o audit log")
    sa.add_argument("--json", action="store_true")
    sa.set_defaults(fn=_cmd_ask)

    cf = sub.add_parser("config", help="mostra a política efetiva")
    cf.add_argument("--dump", action="store_true",
                    help="imprime cada bloco com o valor e a origem, mais os blocos imutáveis")
    cf.set_defaults(fn=_cmd_config)

    ck = sub.add_parser("check", help="roda os hooks determinísticos declarados na política")
    ck.add_argument("--quando", default=hooks.QUANDO_SEMPRE,
                    help=f"momento a casar (default: {hooks.QUANDO_SEMPRE!r}); "
                         f"hooks com quando={hooks.QUANDO_SEMPRE!r} sempre entram")
    ck.add_argument("--todos", action="store_true",
                    help="roda todos os hooks declarados, seja qual for o `quando`")
    ck.add_argument("--cwd", help="diretório de trabalho dos hooks (default: o atual)")
    ck.add_argument("--timeout", type=float, default=hooks.DEFAULT_TIMEOUT_S,
                    help=f"teto por hook, em segundos (default: {hooks.DEFAULT_TIMEOUT_S})")
    ck.add_argument("--label", help="rótulo para os eventos `verify` na trilha")
    ck.set_defaults(fn=_cmd_check)

    im = sub.add_parser("improve", help="lê a trilha e PROPÕE alteração de política (nunca aplica sozinho)")
    im.add_argument("--apply", metavar="ID",
                    help="autoriza uma proposta já gravada; exige --policy nomeando o arquivo a editar")
    im.add_argument("--trilha", help="trilha de auditoria a analisar (default: a de FPCH_AUDIT_LOG / ~/.fpch)")
    im.add_argument("--propostas", help="diretório das propostas (default: FPCH_PROPOSTAS_DIR / ~/.fpch/propostas)")
    im.add_argument("--label", help="rótulo para os eventos de recusa na trilha")
    im.set_defaults(fn=_cmd_improve)

    au = sub.add_parser("audit", help="resumo de uso por pool")
    au.add_argument("--verify", action="store_true",
                    help="confere o encadeamento de hashes da trilha, em vez do resumo por pool")
    au.add_argument("--causes", action="store_true",
                    help="causa raiz e fonte agregadas (audit.summary().by_cause / by_source)")
    au.add_argument("--process", action="store_true",
                    help="marcos do processo de falha por trajetória (audit.failure_process())")
    au.add_argument("--trilha", help="trilha de auditoria a conferir (default: a de FPCH_AUDIT_LOG / ~/.fpch)")
    au.set_defaults(fn=_cmd_audit)

    cc = sub.add_parser("cite-check", help="valida citações file:line contra o disco (sem LLM)")
    cc.add_argument("file", help="markdown a validar")
    cc.add_argument("root", help="raiz do código citado")
    cc.set_defaults(fn=_cmd_cite_check)

    qc = sub.add_parser("quote-check",
                        help="valida trechos entre aspas contra o texto da fonte (sem LLM)")
    qc.add_argument("fiche", help="fichamento markdown a validar")
    qc.add_argument("source", help="texto integral da fonte (extraído do PDF)")
    qc.add_argument("--emit-block", action="store_true",
                    help="imprime o bloco markdown para embutir na ficha")
    qc.set_defaults(fn=_cmd_quote_check)

    sc = sub.add_parser("canib", help="canibaliza repos/artigos")
    csub = sc.add_subparsers(dest="canib_cmd", required=True)

    ca = csub.add_parser("add")
    ca.add_argument("url")
    ca.add_argument("--note", help="por que este alvo importa")
    ca.add_argument("--focus", choices=["conceito", "funcao"], default="conceito",
                    help="conceito = minerar ideias; funcao = minerar mecanismos")

    cl = csub.add_parser("list")
    cl.add_argument("--status", choices=["pending", "accepted", "rejected", "done"])

    cra = csub.add_parser("runall", help="roda todos os aprovados")
    cra.add_argument("--focus", choices=["conceito", "funcao"])
    cra.add_argument("--extract-model")
    cra.add_argument("--propose-model")

    cac = csub.add_parser("accept")
    cac.add_argument("id")

    cr = csub.add_parser("reject")
    cr.add_argument("id")

    cru = csub.add_parser("run")
    cru.add_argument("id")
    cru.add_argument("--extract-model")
    cru.add_argument("--propose-model")

    sc.set_defaults(fn=_cmd_canib)
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)

    # UMA carga, antes de qualquer trabalho. Falha aqui derruba o processo com
    # mensagem legível e código 2: política malformada não degrada em silêncio
    # nem é descoberta no meio de uma execução já com efeito colateral no disco.
    try:
        pol = policy.load(args.policy)
        catalogo = models.catalog_from_policy(pol)
    except (policy.PolicyError, models.CatalogError) as exc:
        print(f"erro de política: {exc}", file=sys.stderr)
        return EXIT_USO

    args.policy_obj = pol
    args.catalog = catalogo

    # O catálogo derivado fica em vigor só durante este subcomando: é assim que
    # `router.plan/route` — que ainda não recebem catálogo por parâmetro — passam
    # a enxergar a política sem que o estado vaze para o processo inteiro.
    with models.use_catalog(catalogo):
        return args.fn(args)


if __name__ == "__main__":
    raise SystemExit(main())
