"""Validação determinística de **trechos citados** — o irmão de `citations.py`.

## O problema que este módulo resolve

`citations.py` responde "a linha `arquivo:123` existe?". Isso elimina a citação
fabricada de código, mas não cobre o caso do fichamento acadêmico, onde a
evidência é uma **frase entre aspas atribuída a um paper**. Uma ficha pode ter
todas as páginas certas e ainda assim conter uma frase que o autor do paper
nunca escreveu.

Este módulo confere cada trecho entre aspas contra o **texto integral extraído
do PDF original**, e cada `(p. N)` contra o número real de páginas.

## A calibração — e por que ela é o coração deste módulo

`citations.py` carrega a história de um verificador que **fabricou uma acusação**:
um script contou linhas com uma ferramenta que ignora linhas em branco, "provou"
35% de citações fabricadas, e estava errado. A lição registrada lá foi:

> Um hook determinístico não é automaticamente *correto* — é apenas
> automaticamente *consistente*. Consistente e errado é pior que ausente.

Comparar texto extraído de PDF é exatamente o terreno onde esse erro se repetiria.
A extração introduz ruído que **não é** desonestidade do modelo:

- hifenização de fim de linha (`harn-\\ness` → `harness`);
- ligaduras tipográficas (`ﬁ`, `ﬂ`);
- aspas e travessões curvos vindos do LaTeX;
- quebras de linha no meio da frase, espaços múltiplos, números de página
  intercalados no meio do parágrafo.

Um comparador ingênuo acusaria de fabricação uma citação perfeitamente honesta.
Por isso, aqui:

1. **Normaliza agressivamente** antes de comparar (§`_normalize`).
2. Quando a comparação exata falha, **não conclui fabricação de imediato**:
   procura a janela de palavras mais longa do trecho que ainda existe na fonte.
   Isso separa três coisas que um booleano confundiria — trecho **exato**,
   trecho **aproximado** (citação levemente editada, erro menor) e trecho
   **ausente** (nenhuma parte significativa aparece na fonte).
3. **Não avalia tradução.** As fichas deste projeto são em português e os papers
   são em inglês. Um trecho em português citando um paper em inglês é tradução,
   não cópia — verificá-lo contra o original acusaria o correto. Trechos assim
   são marcados `nao-verificavel`, que **não** é falha.

O veredito `ausente` é o único que conta como fabricação. Os demais são
informação para o humano decidir.

## Contrato

- Entrada: markdown da ficha + texto integral da fonte (com marcadores
  `===== PAGINA N =====`, como produzido pela extração do PDF).
- Nenhum LLM envolvido em nenhuma etapa.
- Sem dependência externa: só a biblioteca padrão.

Ver `citations.py` para a história completa do episódio que originou a regra.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from pathlib import Path

_PAGE_MARK = re.compile(r"=====\s*PAGINA\s+(\d+)\s*=====")

# Trecho citado: blockquote com aspas, ou aspas no meio do texto.
# Exige tamanho mínimo para não capturar termo solto entre aspas ("harness").
_QUOTED = re.compile(r"[\"“”«»]([^\"“”«»\n]{40,600})[\"“”«»]")

_PAGE_CITE = re.compile(r"\(\s*p\.?\s*(\d+)\s*\)", re.IGNORECASE)

# Marcadores explícitos de que o autor da ficha já assumiu não ser literal.
_PARAPHRASE_HINT = re.compile(r"par[áa]frase|tradu[çc][ãa]o livre|adaptado", re.IGNORECASE)

_PT_STOPWORDS = {
    "de", "que", "não", "para", "com", "uma", "dos", "das", "como", "mas",
    "por", "mais", "os", "as", "um", "ao", "à", "no", "na", "em", "é", "são",
    "do", "da", "se", "pelo", "pela", "isso", "ser",
}
_EN_STOPWORDS = {
    "the", "of", "and", "to", "that", "is", "in", "for", "with", "are", "this",
    "we", "on", "as", "by", "it", "be", "from", "an", "our", "which", "these",
}

# Janela mínima de palavras para considerar que "parte do trecho existe".
# Abaixo disso a coincidência deixa de ser evidência e vira acaso.
_MIN_WINDOW_WORDS = 6


@dataclass(frozen=True)
class Quote:
    text: str
    verdict: str  # "exato" | "aproximado" | "ausente" | "nao-verificavel"
    detail: str = ""

    @property
    def counts_as_failure(self) -> bool:
        return self.verdict == "ausente"


@dataclass(frozen=True)
class PageRef:
    page: int
    verdict: str  # "ok" | "fora-do-intervalo"
    detail: str = ""

    @property
    def counts_as_failure(self) -> bool:
        return self.verdict != "ok"


@dataclass
class QuoteReport:
    quotes: tuple[Quote, ...]
    pages: tuple[PageRef, ...]
    source_pages: int

    @property
    def checkable(self) -> tuple[Quote, ...]:
        return tuple(q for q in self.quotes if q.verdict != "nao-verificavel")

    @property
    def absent(self) -> tuple[Quote, ...]:
        return tuple(q for q in self.quotes if q.verdict == "ausente")

    @property
    def approximate(self) -> tuple[Quote, ...]:
        return tuple(q for q in self.quotes if q.verdict == "aproximado")

    @property
    def bad_pages(self) -> tuple[PageRef, ...]:
        return tuple(p for p in self.pages if p.counts_as_failure)

    @property
    def failed(self) -> bool:
        return bool(self.absent or self.bad_pages)

    def summary(self) -> str:
        n_check = len(self.checkable)
        n_skip = len(self.quotes) - n_check
        parts = [
            f"{len(self.quotes)} trechos entre aspas",
            f"{n_check} verificáveis",
            f"{sum(1 for q in self.checkable if q.verdict == 'exato')} exatos",
            f"{len(self.approximate)} aproximados",
            f"{len(self.absent)} ausentes",
        ]
        if n_skip:
            parts.append(f"{n_skip} em português (tradução, não verificável)")
        page_part = (
            f"{len(self.pages)} refs de página · {len(self.bad_pages)} fora do intervalo "
            f"(fonte tem {self.source_pages})"
        )
        return " · ".join(parts) + "\n" + page_part


def _normalize(text: str) -> str:
    """Reduz o texto ao que sobrevive à extração de PDF.

    Tudo que a extração costuma estropiar é apagado aqui, dos dois lados da
    comparação. O objetivo não é fidelidade tipográfica — é não acusar o inocente.
    """
    t = unicodedata.normalize("NFKC", text)
    # Hifenização de quebra de linha: "harn-\ness" -> "harness".
    t = re.sub(r"-\s*\n\s*", "", t)
    # Aspas, apóstrofos e travessões curvos -> ASCII.
    t = t.translate(str.maketrans({
        "‘": "'", "’": "'", "“": '"', "”": '"',
        "–": "-", "—": "-", "−": "-", "­": "",
        "«": '"', "»": '"', "…": "...",
    }))
    t = t.lower()
    # Qualquer coisa que não seja letra/número/espaço não distingue conteúdo.
    t = re.sub(r"[^\w\s]", " ", t, flags=re.UNICODE)
    return re.sub(r"\s+", " ", t).strip()


def _looks_portuguese(text: str) -> bool:
    words = re.findall(r"\w+", text.lower(), flags=re.UNICODE)
    if not words:
        return False
    pt = sum(1 for w in words if w in _PT_STOPWORDS)
    en = sum(1 for w in words if w in _EN_STOPWORDS)
    return pt > en


def _longest_present_window(needle_words: list[str], haystack: str) -> int:
    """Maior nº de palavras consecutivas do trecho que existem na fonte.

    Busca decrescente: se a janela inteira não bate, encurta. Serve para
    distinguir "citação levemente editada" de "frase que não existe no papel".
    """
    n = len(needle_words)
    for size in range(n, _MIN_WINDOW_WORDS - 1, -1):
        for start in range(0, n - size + 1):
            window = " ".join(needle_words[start:start + size])
            if window in haystack:
                return size
    return 0


def count_pages(source_text: str) -> int:
    marks = [int(m.group(1)) for m in _PAGE_MARK.finditer(source_text)]
    return max(marks) if marks else 0


def validate(fiche_text: str, source_text: str) -> QuoteReport:
    """Confere trechos entre aspas e refs `(p. N)` da ficha contra a fonte."""
    haystack = _normalize(source_text)
    total_pages = count_pages(source_text)

    quotes: list[Quote] = []
    seen: set[str] = set()

    for match in _QUOTED.finditer(fiche_text):
        raw = match.group(1).strip()
        key = _normalize(raw)
        if not key or key in seen:
            continue
        seen.add(key)

        # Trecho em português citando fonte em inglês = tradução. Não é cópia,
        # e cobrá-la como cópia seria acusar o correto.
        line_start = fiche_text.rfind("\n", 0, match.start()) + 1
        line_end = fiche_text.find("\n", match.end())
        context = fiche_text[line_start: line_end if line_end != -1 else len(fiche_text)]
        if _looks_portuguese(raw) or _PARAPHRASE_HINT.search(context):
            quotes.append(Quote(raw, "nao-verificavel",
                                "trecho em português ou marcado como paráfrase/tradução"))
            continue

        if key in haystack:
            quotes.append(Quote(raw, "exato"))
            continue

        words = key.split()
        best = _longest_present_window(words, haystack)
        if best >= _MIN_WINDOW_WORDS:
            pct = round(best / len(words) * 100)
            quotes.append(Quote(raw, "aproximado",
                                f"maior trecho contíguo presente: {best}/{len(words)} palavras ({pct}%)"))
        else:
            quotes.append(Quote(raw, "ausente",
                                "nenhuma sequência significativa do trecho aparece na fonte"))

    pages: list[PageRef] = []
    for m in sorted({int(m.group(1)) for m in _PAGE_CITE.finditer(fiche_text)}):
        if total_pages and m > total_pages:
            pages.append(PageRef(m, "fora-do-intervalo", f"a fonte tem {total_pages} páginas"))
        else:
            pages.append(PageRef(m, "ok"))

    return QuoteReport(tuple(quotes), tuple(pages), total_pages)


def render_block(report: QuoteReport, source_name: str) -> str:
    """Bloco markdown para embutir na ficha — inclusive quando o resultado é bom.

    Mesma regra de `citations.render_block`: validador que só aparece na falha
    não é auditável, é alarme.
    """
    if not report.quotes:
        return (
            "> ⚠️ **Validação de trechos:** a ficha não tem nenhum trecho entre aspas "
            "com tamanho verificável. Sem trecho não há o que conferir — trate as "
            "afirmações como não-ancoradas.\n"
        )

    icon = "✅" if not report.failed else ("⚠️" if not report.absent else "❌")
    lines = [
        f"> {icon} **Validação determinística de trechos** (contra `{source_name}`, sem LLM)",
        "> " + report.summary().replace("\n", "\n> "),
    ]

    problems = list(report.absent) + list(report.approximate)
    if problems:
        lines += [">", "> | Veredito | Trecho | Detalhe |", "> |---|---|---|"]
        for q in problems:
            snippet = q.text[:90].replace("|", "\\|") + ("…" if len(q.text) > 90 else "")
            lines.append(f"> | {q.verdict} | {snippet} | {q.detail} |")

    if report.bad_pages:
        lines += [">", "> Páginas fora do intervalo: " +
                  ", ".join(f"p. {p.page}" for p in report.bad_pages)]

    lines += [
        ">",
        "> **Como ler:** `aproximado` costuma ser ruído da extração do PDF "
        "(hifenização, ligadura, quebra de linha) ou edição leve da citação — "
        "não é acusação. Só `ausente` indica trecho que não existe na fonte. "
        "Este verificador prova que a frase está no documento; **não** prova que "
        "ela sustenta a afirmação em torno dela.",
    ]
    return "\n".join(lines) + "\n"


def validate_files(fiche: Path, source: Path) -> QuoteReport:
    return validate(fiche.read_text(encoding="utf-8"), source.read_text(encoding="utf-8"))
