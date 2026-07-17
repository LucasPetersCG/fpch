"""Validação determinística de citações — um hook, não um juiz.

## Por que este módulo existe, e a história que quase o corrompeu

Em 17/07/2026 o pipeline rodou contra um clone local. Um script ad-hoc de PowerShell
"provou" que 9 de 26 citações (35%) apontavam além do fim do arquivo — o modelo
estaria fabricando `file:line`. O achado foi reportado ao autor como fato.

**Era falso.** O script usava `(Get-Content $p | Measure-Object -Line).Lines`, que
**não conta linhas em branco**. Ele subcontava todo arquivo:

    arquivo       real   Measure-Object
    README.md      92         59
    train.py      630        545

`README.md:92` — a citação "fabricada" mais citada como prova — existe, e contém
exatamente `MIT`, que era o fato alegado. Confirmado por três métodos independentes
(`git grep -c ""`, split do texto bruto, contagem de array).

**As citações do modelo estavam certas. O verificador é que estava errado.**

Este módulo, escrito para pegar a fabricação, deu `0 fabricadas` — a resposta
correta. Ele foi ignorado em favor do script ad-hoc, porque o script confirmava a
hipótese que se queria confirmar.

## As lições, em ordem de importância

1. **O verificador também precisa ser verificado.** Um hook determinístico não é
   automaticamente correto — é apenas automaticamente *consistente*. Consistente e
   errado é pior que ausente, porque produz falsa confiança e acusa quem estava
   certo.
2. **Contrato de ferramenta > intuição sobre ferramenta.** `Measure-Object -Line`
   parece contar linhas. Não conta: conta linhas *não-vazias*. Ninguém leu a doc
   porque o número saiu plausível.
3. **Resultado que confirma a hipótese merece MAIS ceticismo, não menos.** O erro
   passou justamente porque "o modelo fabrica citações" era a narrativa da sessão.
4. Prompt não conserta fabricação *quando ela existe* — mas antes de acusar, meça
   com um instrumento que você validou.

O ponto de Huang et al. (ICLR 2024, Tier A) — auto-correção sem feedback externo
degrada — continua valendo. Só que o feedback externo precisa ser **calibrado**.
Um verificador não-calibrado é só mais um gerador de alegações confiantes.

## Contrato deste módulo

Conta linhas com `sum(1 for _ in open(path,'rb'))` — divide por `b"\\n"`, conta
linhas em branco, não depende de encoding. Validado contra `git grep -c ""` em
17/07/2026.

Ver `docs/SESSAO-2026-07-16-handoff.md` §9-bis e `docs/analises/modelo-canibalizacao.md`.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

# `caminho/arquivo.ext:123` — o formato que pedimos ao modelo.
_CITE = re.compile(r"([\w./\\-]+\.(?:py|md|txt|toml|json|jsonc|ipynb|lock|ts|tsx|js|rs|go|yaml|yml|sh|ps1)):(\d+)")

# Diretórios que modelos inventam por hábito, porque a maioria dos projetos os tem.
# O `src/` alucinado apareceu em duas fichas independentes, contra dois repos que
# não têm `src/`.
_HABIT_PREFIXES = ("src/", "src\\", "lib/", "app/")


@dataclass(frozen=True)
class Citation:
    path: str
    line: int
    verdict: str  # "ok" | "linha-inexistente" | "arquivo-inexistente" | "prefixo-inventado"
    detail: str = ""

    @property
    def ok(self) -> bool:
        return self.verdict == "ok"


@dataclass
class Report:
    citations: tuple[Citation, ...]

    @property
    def total(self) -> int:
        return len(self.citations)

    @property
    def valid(self) -> int:
        return sum(1 for c in self.citations if c.ok)

    @property
    def fabricated(self) -> int:
        return self.total - self.valid

    @property
    def fabrication_rate(self) -> float:
        return 0.0 if not self.total else self.fabricated / self.total

    def failures(self) -> list[Citation]:
        return [c for c in self.citations if not c.ok]

    def summary(self) -> str:
        if not self.total:
            return "nenhuma citação file:line encontrada"
        pct = round(self.fabrication_rate * 100)
        return f"{self.total} citações · {self.valid} válidas · {self.fabricated} fabricadas ({pct}%)"


def _count_lines(path: Path) -> int:
    try:
        with path.open("rb") as fh:
            return sum(1 for _ in fh)
    except OSError:
        return -1


def validate(text: str, root: Path) -> Report:
    """Confere cada `file:line` do texto contra os arquivos reais sob `root`.

    Deduplica: a mesma citação repetida é um erro, não vários.
    """
    seen: set[tuple[str, int]] = set()
    out: list[Citation] = []

    for match in _CITE.finditer(text):
        raw_path, raw_line = match.group(1), int(match.group(2))
        key = (raw_path, raw_line)
        if key in seen:
            continue
        seen.add(key)

        target = root / raw_path
        if target.exists():
            n = _count_lines(target)
            if n >= 0 and raw_line > n:
                out.append(Citation(raw_path, raw_line, "linha-inexistente",
                                    f"o arquivo tem {n} linhas"))
            else:
                out.append(Citation(raw_path, raw_line, "ok"))
            continue

        # Caminho não existe. O modelo inventou um nível de diretório?
        bare = Path(raw_path).name
        matches = list(root.rglob(bare))
        matches = [m for m in matches if ".git" not in m.parts]
        if matches:
            real = matches[0].relative_to(root).as_posix()
            if any(raw_path.startswith(p) for p in _HABIT_PREFIXES):
                verdict = "prefixo-inventado"
            else:
                verdict = "arquivo-inexistente"
            out.append(Citation(raw_path, raw_line, verdict, f"caminho real: {real}"))
        else:
            out.append(Citation(raw_path, raw_line, "arquivo-inexistente",
                                "nenhum arquivo com esse nome sob a raiz"))

    return Report(tuple(out))


def render_block(report: Report, root: Path) -> str:
    """Bloco markdown para embutir na ficha. Sempre embutido — inclusive quando
    o resultado é bom. Um validador que só aparece na falha não é auditável."""
    if not report.total:
        return (
            "> ⚠️ **Validação de citações:** nenhuma citação `arquivo:linha` "
            "encontrada. Sem citação não há o que verificar — trate o relatório "
            "como não-ancorado.\n"
        )

    pct = round(report.fabrication_rate * 100)
    icon = "✅" if pct == 0 else ("⚠️" if pct < 20 else "❌")
    lines = [
        f"> {icon} **Validação determinística de citações** (contra `{root.name}`, sem LLM)",
        f"> {report.summary()}",
    ]
    if report.failures():
        lines.append(">")
        lines.append("> | Citação | Problema | Detalhe |")
        lines.append("> |---|---|---|")
        for c in report.failures():
            lines.append(f"> | `{c.path}:{c.line}` | {c.verdict} | {c.detail} |")
        lines.append(">")
        lines.append(
            "> Citação fabricada não invalida só a linha — ela indica que o modelo "
            "gerou evidência sob demanda. Trate as afirmações **não citadas** com a "
            "mesma desconfiança."
        )
    return "\n".join(lines) + "\n"
