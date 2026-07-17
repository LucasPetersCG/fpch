"""CLI do FPCH.

    fpch models                      catálogo + o que está instalado
    fpch plan hard                   mostra a cadeia de escalada, sem gastar cota
    fpch ask standard "prompt"       roteia e imprime
    fpch audit                       para onde a cota foi
    fpch canib add <url> [--note]    enfileira alvo
    fpch canib list                  fila
    fpch canib accept <id>           aprovação humana (obrigatória)
    fpch canib run <id>              extrai + propõe → ficha em docs/
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from . import audit, backends, cannibalize, citations, router
from .models import CATALOG, TaskClass

REPO_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_WORKDIR = REPO_ROOT / ".fpch-work"
DEFAULT_FICHE_DIR = REPO_ROOT / "docs" / "referencias" / "fichamentos"


def _cmd_models(_: argparse.Namespace) -> int:
    by_pool: dict[str, list] = {}
    for m in CATALOG:
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
    try:
        chain = router.plan(TaskClass(args.task_class))
    except router.NoBackendAvailable as exc:
        print(f"erro: {exc}", file=sys.stderr)
        return 1
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
    try:
        result = router.route(
            TaskClass(args.task_class),
            prompt,
            workdir=DEFAULT_WORKDIR,
            model_id=args.model,
            timeout_s=args.timeout,
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


def _cmd_audit(_: argparse.Namespace) -> int:
    s = audit.summary()
    if not s["total_calls"]:
        print("sem registros ainda.")
        return 0
    print(f"chamadas totais: {s['total_calls']}\n")
    print(f"{'pool':<18}{'chamadas':>10}{'falhas':>8}{'lat.méd':>10}{'chars':>10}")
    for pool, agg in sorted(s["by_pool"].items()):
        print(f"{pool:<18}{agg['calls']:>10}{agg['fail']:>8}{agg['latency_avg']:>9}s{agg['chars_out']:>10}")
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
    sa.add_argument("--timeout", type=int, default=backends.DEFAULT_TIMEOUT_S)
    sa.add_argument("--allow-write", action="store_true", help="⚠️ permite que o agente escreva")
    sa.add_argument("--label", help="rótulo para o audit log")
    sa.add_argument("--json", action="store_true")
    sa.set_defaults(fn=_cmd_ask)

    sub.add_parser("audit", help="resumo de uso por pool").set_defaults(fn=_cmd_audit)

    cc = sub.add_parser("cite-check", help="valida citações file:line contra o disco (sem LLM)")
    cc.add_argument("file", help="markdown a validar")
    cc.add_argument("root", help="raiz do código citado")
    cc.set_defaults(fn=_cmd_cite_check)

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
    return args.fn(args)


if __name__ == "__main__":
    raise SystemExit(main())
