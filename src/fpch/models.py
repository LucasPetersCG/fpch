"""Catálogo de modelos e pools de cota do FPCH.

Verificado empiricamente em 14/09/2026 na máquina do autor: `agy --model
__invalido__` (o erro enumera os modelos aceitos) seguido de ping real, uma
chamada por modelo com checagem de eco de token, em `agy`, `copilot` e `codex`.
A verificação anterior (16/07/2026) listava a família Gemini 3.5 Flash, que o
`agy` deixou de aceitar.

Conceito central: POOL DE COTA. O mesmo CLI (`agy`) fala com modelos que
consomem cotas DIFERENTES e independentes. Rotear ignorando o pool desperdiça
a folga de um enquanto esgota o outro — é o erro que este módulo existe para
evitar.

**Catálogo embutido e catálogo em vigor (Onda 2A).** `CATALOG` continua sendo o
default embutido, byte a byte o que sempre foi — `tests/test_policy.py` prova que
`policy._DEFAULT_MODELS` o espelha, e essa prova só vale enquanto `CATALOG` for
uma constante literal. O que a Onda 2A acrescenta é a possibilidade de **derivar**
um catálogo de uma `Policy` já carregada (`catalog_from_policy`) e de colocá-lo
*em vigor* durante uma execução (`use_catalog`), sem reescrever a constante.

Este módulo **não importa `fpch.policy`** em tempo de execução, de propósito: a
conversão lê apenas `policy.modelos` de forma estrutural (`.id`, `.pool`, …), o
que mantém a direção da dependência em `policy → (ninguém)` e permite testar a
conversão com qualquer objeto que tenha o mesmo formato.
"""

from __future__ import annotations

from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from enum import Enum
from typing import TYPE_CHECKING

if TYPE_CHECKING:  # pragma: no cover - só para o verificador de tipos
    from .policy import ModelEntry, Policy


class Pool(str, Enum):
    """Cotas independentes. Esgotar uma NÃO afeta as outras."""

    AGY_GOOGLE = "agy:google"        # Gemini + GPT-OSS via assinatura Antigravity
    AGY_ANTHROPIC = "agy:anthropic"  # Claude via assinatura Antigravity (cota à parte)
    COPILOT = "copilot"              # GitHub Copilot AI credits
    CODEX = "codex"                  # assinatura ChatGPT/Codex (cota à parte)
    CLAUDE_SUB = "claude"            # assinatura Anthropic direta (a do host)
    LOCAL = "local"                  # Ollama etc. — custo real zero


class TaskClass(str, Enum):
    """Classes de tarefa. Espelha a tabela de roteamento do CLAUDE.md do autor."""

    MECHANICAL = "mechanical"        # localizar, listar, grep, renomear, formatar
    STANDARD = "standard"            # implementar em padrão conhecido, testes, docs
    HARD = "hard"                    # debugar, arquitetura, migração, segurança
    ORCHESTRATION = "orchestration"  # síntese, decisão, comunicação — fica no host


@dataclass(frozen=True)
class Model:
    """Um modelo endereçável.

    `id` é a string EXATA aceita por `--model`. Tem espaços e parênteses:
    sempre passar como argumento único de lista, nunca interpolar em shell.
    """

    id: str
    backend: str
    pool: Pool
    # Capacidade relativa 1-5 (juízo do autor + evidência de bench, não medida formal).
    power: int
    # Custo relativo de cota 1-5. Alto = gastar com parcimônia.
    cost: int
    good_for: tuple[TaskClass, ...]
    notes: str = ""


# Ordem importa: dentro de um pool, o primeiro que serve à classe é o preferido.
CATALOG: tuple[Model, ...] = (
    # --- agy / cota Google -------------------------------------------------
    # Três gerações de Flash coexistem no `agy` (14/09/2026). Mesma potência e
    # custo entre elas: o desempate de `candidates()` é a ordem, e por isso a
    # geração mais nova vem primeiro e as anteriores ficam como fallback.
    Model("Gemini 3.8 Flash (Low)", "agy", Pool.AGY_GOOGLE, power=2, cost=1,
          good_for=(TaskClass.MECHANICAL,),
          notes="Piso do arsenal. Só é escolhido se Medium falhar — custa o mesmo e é mais fraco."),
    Model("Gemini 3.8 Flash (Medium)", "agy", Pool.AGY_GOOGLE, power=3, cost=1,
          good_for=(TaskClass.MECHANICAL, TaskClass.STANDARD)),
    Model("Gemini 3.8 Flash (High)", "agy", Pool.AGY_GOOGLE, power=3, cost=2,
          good_for=(TaskClass.MECHANICAL, TaskClass.STANDARD),
          notes="Cavalo de batalha indicado pelo autor: rápido e bom até tarefa mediana."),
    Model("Gemini 3.7 Flash (Low)", "agy", Pool.AGY_GOOGLE, power=2, cost=1,
          good_for=(TaskClass.MECHANICAL,),
          notes="Fallback do Gemini 3.8 Flash (Low)."),
    Model("Gemini 3.7 Flash (Medium)", "agy", Pool.AGY_GOOGLE, power=3, cost=1,
          good_for=(TaskClass.MECHANICAL, TaskClass.STANDARD),
          notes="Fallback do Gemini 3.8 Flash (Medium)."),
    Model("Gemini 3.7 Flash (High)", "agy", Pool.AGY_GOOGLE, power=3, cost=2,
          good_for=(TaskClass.MECHANICAL, TaskClass.STANDARD),
          notes="Fallback do Gemini 3.8 Flash (High)."),
    Model("Gemini 3.6 Flash (Low)", "agy", Pool.AGY_GOOGLE, power=2, cost=1,
          good_for=(TaskClass.MECHANICAL,),
          notes="Fallback do Gemini 3.7 Flash (Low)."),
    Model("Gemini 3.6 Flash (Medium)", "agy", Pool.AGY_GOOGLE, power=3, cost=1,
          good_for=(TaskClass.MECHANICAL, TaskClass.STANDARD),
          notes="Fallback do Gemini 3.7 Flash (Medium)."),
    Model("Gemini 3.6 Flash (High)", "agy", Pool.AGY_GOOGLE, power=3, cost=2,
          good_for=(TaskClass.MECHANICAL, TaskClass.STANDARD),
          notes="Fallback do Gemini 3.7 Flash (High)."),
    Model("Gemini 3.1 Pro (Low)", "agy", Pool.AGY_GOOGLE, power=4, cost=2,
          good_for=(TaskClass.STANDARD,)),
    Model("Gemini 3.1 Pro (High)", "agy", Pool.AGY_GOOGLE, power=4, cost=3,
          good_for=(TaskClass.STANDARD, TaskClass.HARD),
          notes="Teto da cota Google. Tentar aqui ANTES de gastar cota Anthropic."),
    Model("GPT-OSS 120B (Medium)", "agy", Pool.AGY_GOOGLE, power=3, cost=1,
          good_for=(TaskClass.MECHANICAL, TaskClass.STANDARD),
          notes="Diversidade de família — útil para segunda opinião independente."),

    # --- agy / cota Anthropic (SEPARADA da cota Google) --------------------
    Model("Claude Sonnet 4.6 (Thinking)", "agy", Pool.AGY_ANTHROPIC, power=4, cost=3,
          good_for=(TaskClass.STANDARD, TaskClass.HARD)),
    Model("Claude Opus 4.6 (Thinking)", "agy", Pool.AGY_ANTHROPIC, power=5, cost=5,
          good_for=(TaskClass.HARD,),
          notes="Caro (autor). Só quando Gemini 3.1 Pro (High) não dá conta."),

    # --- copilot -----------------------------------------------------------
    Model("default", "copilot", Pool.COPILOT, power=2, cost=2,
          good_for=(TaskClass.MECHANICAL, TaskClass.STANDARD),
          notes="Resolve via auto para mai-code-1.1-flash (única availableModel em "
                "14/09/2026). --model explícito é recusado com exit 0 nesta conta."),

    # --- codex / assinatura ChatGPT (cota à parte) -------------------------
    # id no formato "<slug> (<esforço>)", espelhando a nomenclatura do agy; o
    # adaptador separa as duas partes. power/cost são juízo provisório, pendente
    # de revisão do autor — o ping de 14/09/2026 prova que a rota funciona, não
    # mede capacidade.
    Model("gpt-5.6-luna (low)", "codex", Pool.CODEX, power=3, cost=1,
          good_for=(TaskClass.MECHANICAL,)),
    Model("gpt-5.6-luna (medium)", "codex", Pool.CODEX, power=3, cost=2,
          good_for=(TaskClass.MECHANICAL, TaskClass.STANDARD)),
    Model("gpt-5.6-terra (medium)", "codex", Pool.CODEX, power=4, cost=2,
          good_for=(TaskClass.STANDARD,)),
    Model("gpt-5.5 (medium)", "codex", Pool.CODEX, power=4, cost=2,
          good_for=(TaskClass.STANDARD,),
          notes="Geração anterior."),
    Model("gpt-5.6-sol (high)", "codex", Pool.CODEX, power=5, cost=3,
          good_for=(TaskClass.STANDARD, TaskClass.HARD)),
    Model("gpt-6-astra (high)", "codex", Pool.CODEX, power=5, cost=4,
          good_for=(TaskClass.HARD,),
          notes="Topo da lista — gastar com parcimônia."),
)

#: Índice do catálogo **em vigor**, por `id`.
#:
#: Começa como o índice do default embutido e volta a ele sempre que `use_catalog`
#: sai de escopo. É mutado *no lugar* (nunca rebindado) porque `router.py` faz
#: `from .models import BY_ID` no import: quem já segura a referência precisa ver
#: a mesma troca que `candidates()` vê, sob pena de `--model` e a cadeia de
#: escalada passarem a discordar sobre qual catálogo está valendo.
BY_ID: dict[str, Model] = {m.id: m for m in CATALOG}


class CatalogError(ValueError):
    """Entrada de catálogo que cita um pool ou uma classe de tarefa inexistente.

    É erro, e não aviso: um catálogo com pool desconhecido não tem como ser
    roteado, e seguir adiante ignorando a entrada faria o `fpch` operar com um
    catálogo silenciosamente diferente do que está escrito na política — o modo
    de falha que a seção 3 da fatia funcional proíbe.
    """


def model_from_entry(entry: ModelEntry) -> Model:
    """Converte uma entrada de política (dado simples) num `Model` tipado.

    `pool` e `good_for` chegam como texto e saem como `Pool`/`TaskClass`. Valor
    fora do conjunto conhecido levanta `CatalogError` nomeando o modelo, o campo
    e os valores aceitos — nunca é descartado nem substituído por um default.
    """
    try:
        pool = Pool(entry.pool)
    except ValueError:
        raise CatalogError(
            f"modelo {entry.id!r}: pool desconhecido {entry.pool!r} "
            f"(válidos: {sorted(p.value for p in Pool)})"
        ) from None

    classes: list[TaskClass] = []
    for nome in entry.good_for:
        try:
            classes.append(TaskClass(nome))
        except ValueError:
            raise CatalogError(
                f"modelo {entry.id!r}: classe de tarefa desconhecida em good_for: {nome!r} "
                f"(válidas: {sorted(c.value for c in TaskClass)})"
            ) from None

    return Model(
        id=entry.id,
        backend=entry.backend,
        pool=pool,
        power=entry.power,
        cost=entry.cost,
        good_for=tuple(classes),
        notes=entry.notes or "",
    )


def catalog_from_policy(policy: Policy) -> tuple[Model, ...]:
    """Catálogo derivado de uma `Policy` já carregada.

    A ordem do arquivo é preservada: dentro de um pool, o primeiro que serve à
    classe continua sendo o preferido em caso de empate — a mesma regra que
    `CATALOG` documenta, agora sob controle de quem escreve a política.
    """
    return tuple(model_from_entry(e) for e in policy.modelos)


def index_by_id(catalog: Sequence[Model]) -> dict[str, Model]:
    """Índice `id -> Model` de um catálogo qualquer."""
    return {m.id: m for m in catalog}


# --------------------------------------------------------------------------
# Catálogo em vigor
#
# Costura deliberadamente pequena. `router.plan()`/`router.route()` não recebem
# catálogo por parâmetro — `router.py` pertence a outra onda desta rodada e não
# pode ser editado aqui —, então o catálogo em vigor é um estado de módulo que
# `candidates()` consulta como default e que `use_catalog()` troca durante um
# escopo bem delimitado (a CLI embrulha UMA execução de subcomando).
#
# Isto é um andaime, não o desenho final: a forma correta é o catálogo descer
# como parâmetro explícito de `router.plan/route`. Registrado aqui para que a
# onda que puder tocar `router.py` remova o estado global em vez de herdá-lo.
# --------------------------------------------------------------------------

_CATALOGO_EM_VIGOR: tuple[Model, ...] = CATALOG


def active_catalog() -> tuple[Model, ...]:
    """O catálogo em vigor — o embutido, ou o que `use_catalog()` colocou no lugar."""
    return _CATALOGO_EM_VIGOR


@contextmanager
def use_catalog(catalog: Sequence[Model]) -> Iterator[tuple[Model, ...]]:
    """Coloca `catalog` em vigor durante o bloco e restaura o anterior ao sair.

    Restaura mesmo em caso de exceção: um catálogo que vaza de uma execução para
    a seguinte faria dois comandos idênticos responderem coisas diferentes, e
    seria justamente o tipo de estado invisível que esta ferramenta existe para
    não ter.
    """
    global _CATALOGO_EM_VIGOR
    anterior = _CATALOGO_EM_VIGOR
    anterior_by_id = dict(BY_ID)
    _CATALOGO_EM_VIGOR = tuple(catalog)
    BY_ID.clear()
    BY_ID.update(index_by_id(_CATALOGO_EM_VIGOR))
    try:
        yield _CATALOGO_EM_VIGOR
    finally:
        _CATALOGO_EM_VIGOR = anterior
        BY_ID.clear()
        BY_ID.update(anterior_by_id)


def candidates(
    task: TaskClass,
    exclude_pools: frozenset[Pool] = frozenset(),
    catalog: Sequence[Model] | None = None,
) -> list[Model]:
    """Modelos que servem à classe, do mais barato ao mais caro.

    Empate de custo é desfeito por potência decrescente: entre dois modelos que
    custam o mesmo, pegar o mais forte é grátis.

    `catalog=None` usa o catálogo em vigor (`active_catalog()`), que por sua vez
    é o default embutido enquanto ninguém tiver chamado `use_catalog()`. O
    parâmetro é o terceiro e opcional de propósito: `candidates(task, pools)`
    continua significando exatamente o que significava antes.
    """
    fonte = active_catalog() if catalog is None else catalog
    viable = [
        m for m in fonte
        if task in m.good_for and m.pool not in exclude_pools
    ]
    return sorted(viable, key=lambda m: (m.cost, -m.power))
