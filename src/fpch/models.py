"""Catálogo de modelos e pools de cota do FPCH.

Verificado empiricamente em 16/07/2026 na máquina do autor via
`agy --model __invalido__` (o erro enumera os modelos aceitos).

Conceito central: POOL DE COTA. O mesmo CLI (`agy`) fala com modelos que
consomem cotas DIFERENTES e independentes. Rotear ignorando o pool desperdiça
a folga de um enquanto esgota o outro — é o erro que este módulo existe para
evitar.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum


class Pool(str, Enum):
    """Cotas independentes. Esgotar uma NÃO afeta as outras."""

    AGY_GOOGLE = "agy:google"        # Gemini + GPT-OSS via assinatura Antigravity
    AGY_ANTHROPIC = "agy:anthropic"  # Claude via assinatura Antigravity (cota à parte)
    COPILOT = "copilot"              # GitHub Copilot AI credits
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
    Model("Gemini 3.5 Flash (Low)", "agy", Pool.AGY_GOOGLE, power=2, cost=1,
          good_for=(TaskClass.MECHANICAL,),
          notes="Piso do arsenal. Só é escolhido se Medium falhar — custa o mesmo e é mais fraco."),
    Model("Gemini 3.5 Flash (Medium)", "agy", Pool.AGY_GOOGLE, power=3, cost=1,
          good_for=(TaskClass.MECHANICAL, TaskClass.STANDARD)),
    Model("Gemini 3.5 Flash (High)", "agy", Pool.AGY_GOOGLE, power=3, cost=2,
          good_for=(TaskClass.MECHANICAL, TaskClass.STANDARD),
          notes="Cavalo de batalha indicado pelo autor: rápido e bom até tarefa mediana."),
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
    Model("default", "copilot", Pool.COPILOT, power=4, cost=3,
          good_for=(TaskClass.STANDARD, TaskClass.HARD),
          notes="Backend mais maduro: reporta AI credits, tem --max-ai-credits e allowlist."),
)

BY_ID: dict[str, Model] = {m.id: m for m in CATALOG}


def candidates(task: TaskClass, exclude_pools: frozenset[Pool] = frozenset()) -> list[Model]:
    """Modelos que servem à classe, do mais barato ao mais caro.

    Empate de custo é desfeito por potência decrescente: entre dois modelos que
    custam o mesmo, pegar o mais forte é grátis.
    """
    viable = [
        m for m in CATALOG
        if task in m.good_for and m.pool not in exclude_pools
    ]
    return sorted(viable, key=lambda m: (m.cost, -m.power))
