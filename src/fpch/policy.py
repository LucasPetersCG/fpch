"""Política externa do FPCH — carregamento em TOML, com cascata e default embutido.

Implementa a seção 5 de `docs/decisoes/fatia-funcional-2026-08-31.md`. Duas
regras governam este módulo e não são negociáveis:

1. **Ausência de arquivo nunca é erro.** Quem não escreveu `fpch.policy.toml`
   continua com o comportamento de hoje, byte a byte — é por isso que
   `_default()` abaixo reproduz os valores que já estão em `models.py`,
   `router.py` e `backends.py`, e não uma opinião nova sobre quais deveriam ser.
2. **Arquivo presente e errado é sempre erro, alto e cedo.** Chave desconhecida,
   tipo errado, TOML malformado — tudo isso interrompe o carregamento com uma
   mensagem que nomeia o arquivo e a chave. Política que falha em silêncio é
   exatamente o modo de falha que este projeto inteiro existe para combater
   (ver `backends.py`, a respeito de `exit == 0` que não significa sucesso).

Cascata de precedência, da maior para a menor:
    1. `--policy <caminho>` (parâmetro `cli_path` de `load()`)
    2. variável de ambiente `FPCH_POLICY`
    3. `./fpch.policy.toml` (diretório de trabalho atual)
    4. `~/.fpch/policy.toml`
    5. default embutido neste módulo

Os dois primeiros níveis são um **pedido explícito**: se o caminho não existir,
é erro. Os dois últimos são uma **convenção ambiente**: se o arquivo não estiver
lá, tenta o próximo nível sem se queixar.

Dentro do arquivo vencedor, um bloco ausente herda o valor do default embutido
— e `dump()` mostra essa origem por bloco, não só por arquivo. É o que permite
um `fpch.policy.toml` que só sobrescreve `[falha]` e deixa os modelos como estão.

Este módulo **não importa `fpch.models`**, de propósito: a Onda 2A é quem liga
esta política aos objetos `models.Model` de fato, e um import daqui para lá
criaria um ciclo (`models` não deveria precisar saber que `policy` existe).
Por isso os campos de `ModelEntry` abaixo são dados simples (`str`, `int`,
tupla de `str`) — quem consome decide como reconstruir `models.Model`.

Os quatro blocos que a seção 5.3 da spec declara **não-externalizáveis** —
regras de proveniência, cláusula anti-injeção, padrões de contenção, portão de
aprovação humana — não têm representação em `Policy`. Eles ficam em código, e
`IMMUTABLE_BLOCKS` só existe para que `dump()` os liste como imutáveis, com a
razão. Colocá-los num TOML gravável entregaria a defesa contra conteúdo hostil
a um agente que corre com permissão de escrita — o mesmo anti-padrão que a
canibalização (`cannibalize.py`) foi desenhada para evitar.
"""

from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

# `audit` é o único módulo do FPCH importado daqui, e a direção é segura: ele
# depende apenas da biblioteca padrão, então `policy → audit` não fecha ciclo
# nenhum (o docstring acima explica por que `models` continua de fora). O motivo
# do import é `CAUSE_TAXONOMY`: a taxonomia de causa raiz tem **uma** fonte, e é
# lá — ver `CausaRaizBlock`.
from .audit import CAUSE_TAXONOMY

# --------------------------------------------------------------------------
# Conjuntos válidos, espelhados à mão de `models.py` (Pool e TaskClass).
#
# Duplicados de propósito: importar `models` daqui criaria o ciclo que o
# docstring do módulo explica. O teste `test_catalogo_default_espelha_models`
# em `tests/test_policy.py` é quem garante que este espelho não diverge do
# original — se `models.py` ganhar um pool ou uma classe nova, o teste falha
# até alguém atualizar as duas constantes abaixo.
# --------------------------------------------------------------------------
POOLS_VALIDOS = frozenset({"agy:google", "agy:anthropic", "copilot", "claude", "local"})
TASK_CLASSES_VALIDAS = frozenset({"mechanical", "standard", "hard", "orchestration"})


class PolicyError(RuntimeError):
    """Política malformada, arquivo ilegível, ou caminho explícito ausente.

    Sempre carrega, na mensagem, qual chave e qual arquivo — nunca um erro
    genérico que obriga quem lê a adivinhar onde procurar.
    """


# --------------------------------------------------------------------------
# Blocos tipados
# --------------------------------------------------------------------------

@dataclass(frozen=True)
class MetaBlock:
    versao: int = 1


@dataclass(frozen=True)
class EscaladaBlock:
    max_escalations: int = 2
    ordenar_por: tuple[str, ...] = ("cost", "-power")


@dataclass(frozen=True)
class BackendsBlock:
    timeout_s: int = 600
    arg_limit: int = 24_000


@dataclass(frozen=True)
class VerificacaoBlock:
    """Limites que governam os hooks determinísticos executados por ``fpch check``."""

    timeout_s: int = 120


@dataclass(frozen=True)
class FalhaBlock:
    min_chars: int = 40
    assinaturas: tuple[str, ...] = (
        "no output produced",
        "auto-denied",
        "headless mode cannot prompt",
        "permission that headless",
        "ineligibletiererror",
        "client no longer supported",
        "quota exceeded",
        "rate limit exceeded",
    )


@dataclass(frozen=True)
class ModelEntry:
    """Um modelo do catálogo, em forma de dado simples (sem depender de `models.Model`)."""

    id: str
    backend: str
    pool: str
    power: int
    cost: int
    good_for: tuple[str, ...]
    notes: str = ""


@dataclass(frozen=True)
class HookEntry:
    """Um hook determinístico declarado na política (Fase 3, `hooks.py`, ainda não existe)."""

    nome: str
    cmd: tuple[str, ...]
    quando: str
    criterio: str


@dataclass(frozen=True)
class CausaRaizBlock:
    """Espelho somente leitura da taxonomia canônica mantida em ``audit.py``."""

    categorias: tuple[str, ...] = tuple(CAUSE_TAXONOMY)
    subtipos: tuple[str, ...] = tuple(
        subtipo
        for subtipos_da_categoria in CAUSE_TAXONOMY.values()
        for subtipo in subtipos_da_categoria
    )


@dataclass(frozen=True)
class ImmutableBlock:
    """Um dos quatro blocos que a spec proíbe externalizar — só para exibição em `dump()`."""

    nome: str
    razao: str
    referencia: str


IMMUTABLE_BLOCKS: tuple[ImmutableBlock, ...] = (
    ImmutableBlock(
        nome="taxonomia_de_causa_raiz",
        razao=(
            "A taxonomia define o significado dos números da trilha. Ela é mecanismo, "
            "derivado de audit.CAUSE_TAXONOMY, e não pode ser redefinida por política."
        ),
        referencia="audit.py::CAUSE_TAXONOMY",
    ),
    ImmutableBlock(
        nome="regras_de_proveniencia",
        razao=(
            "É a própria defesa contra o conteúdo hostil que a canibalização assume "
            "como premissa. Externalizar entregaria a defesa a um agente que corre "
            "com permissão de escrita."
        ),
        referencia="cannibalize.py:173-205",
    ),
    ImmutableBlock(
        nome="clausula_anti_injecao",
        razao=(
            "Mesma razão: a cláusula anti-injeção do prompt de extração é o que "
            "impede que texto de terceiro, tratado como dado, vire instrução. "
            "Um arquivo gravável poderia removê-la."
        ),
        referencia="cannibalize.py:208-247",
    ),
    ImmutableBlock(
        nome="padroes_de_contencao",
        razao=(
            "Os padrões de contenção (sandbox, allowlist de ferramentas) são o "
            "limite entre 'sub-agente que só pesquisa' e 'sub-agente que escreve "
            "no disco do autor'. Não é política de roteamento — é o cinto de "
            "segurança."
        ),
        referencia="backends.py:128-129",
    ),
    ImmutableBlock(
        nome="portao_de_aprovacao_humana",
        razao=(
            "O portão que decide se um alvo canibalizado pode ser processado "
            "precisa residir na fronteira do módulo, não numa configuração que o "
            "próprio agente automatizado poderia reescrever para se auto-aprovar."
        ),
        referencia=(
            "ver seção 7.4 da fatia funcional (Onda 2C: cannibalize.run() ainda "
            "não confere target.status na entrada; cli.py:213-219 é hoje a única "
            "barreira)"
        ),
    ),
)


@dataclass(frozen=True)
class Policy:
    """Política efetiva — um bloco por seção do TOML, mais a origem de cada um."""

    meta: MetaBlock
    escalada: EscaladaBlock
    backends: BackendsBlock
    verificacao: VerificacaoBlock
    falha: FalhaBlock
    modelos: tuple[ModelEntry, ...]
    hooks: tuple[HookEntry, ...]
    causa_raiz: CausaRaizBlock
    # nome do bloco -> descrição de onde veio ("default embutido", ou o rótulo
    # da fonte vencedora da cascata quando o bloco foi de fato sobrescrito).
    origem: dict[str, str] = field(default_factory=dict)
    # rótulo da fonte que venceu a cascata como um todo (útil para depuração e
    # para os testes de precedência).
    fonte: str = "default embutido"


# --------------------------------------------------------------------------
# Default embutido — espelha byte a byte o que já está em models.py,
# router.py e backends.py. Não é uma opinião nova: é o comportamento atual,
# tornado configurável. `tests/test_policy.py::test_catalogo_default_espelha_models`
# prova a equivalência com `models.CATALOG` para não deixar isto divergir.
# --------------------------------------------------------------------------

_DEFAULT_MODELS: tuple[ModelEntry, ...] = (
    # --- agy / cota Google ---------------------------------------------
    ModelEntry("Gemini 3.5 Flash (Low)", "agy", "agy:google", power=2, cost=1,
               good_for=("mechanical",),
               notes="Piso do arsenal. Só é escolhido se Medium falhar — custa o mesmo e é mais fraco."),
    ModelEntry("Gemini 3.5 Flash (Medium)", "agy", "agy:google", power=3, cost=1,
               good_for=("mechanical", "standard")),
    ModelEntry("Gemini 3.5 Flash (High)", "agy", "agy:google", power=3, cost=2,
               good_for=("mechanical", "standard"),
               notes="Cavalo de batalha indicado pelo autor: rápido e bom até tarefa mediana."),
    ModelEntry("Gemini 3.1 Pro (Low)", "agy", "agy:google", power=4, cost=2,
               good_for=("standard",)),
    ModelEntry("Gemini 3.1 Pro (High)", "agy", "agy:google", power=4, cost=3,
               good_for=("standard", "hard"),
               notes="Teto da cota Google. Tentar aqui ANTES de gastar cota Anthropic."),
    ModelEntry("GPT-OSS 120B (Medium)", "agy", "agy:google", power=3, cost=1,
               good_for=("mechanical", "standard"),
               notes="Diversidade de família — útil para segunda opinião independente."),
    # --- agy / cota Anthropic (SEPARADA da cota Google) -----------------
    ModelEntry("Claude Sonnet 4.6 (Thinking)", "agy", "agy:anthropic", power=4, cost=3,
               good_for=("standard", "hard")),
    ModelEntry("Claude Opus 4.6 (Thinking)", "agy", "agy:anthropic", power=5, cost=5,
               good_for=("hard",),
               notes="Caro (autor). Só quando Gemini 3.1 Pro (High) não dá conta."),
    # --- copilot ----------------------------------------------------------
    ModelEntry("default", "copilot", "copilot", power=4, cost=3,
               good_for=("standard", "hard"),
               notes="Backend mais maduro: reporta AI credits, tem --max-ai-credits e allowlist."),
)

# O default embutido NÃO declara hook algum, e isso é deliberado mesmo depois de
# `hooks.py` existir: hook é comando que roda na máquina do autor, e uma política
# padrão que já traz comandos executáveis prontos transformaria a mera instalação
# do FPCH em execução de código não pedida. Declarar hook é ato explícito do
# operador, em arquivo dele. Ver a §5.3 do contrato da fatia funcional.
_DEFAULT_HOOKS: tuple[HookEntry, ...] = ()

_TODOS_BLOCOS = ("meta", "escalada", "backends", "verificacao", "falha", "modelos", "hooks")


def _default() -> Policy:
    return Policy(
        meta=MetaBlock(),
        escalada=EscaladaBlock(),
        backends=BackendsBlock(),
        verificacao=VerificacaoBlock(),
        falha=FalhaBlock(),
        modelos=_DEFAULT_MODELS,
        hooks=_DEFAULT_HOOKS,
        causa_raiz=CausaRaizBlock(),
        origem={nome: "default embutido" for nome in _TODOS_BLOCOS},
        fonte="default embutido",
    )


# --------------------------------------------------------------------------
# Resolução da cascata — isolada em funções para poder ser trocada em teste
# (`monkeypatch.setattr(policy, "_cwd", ...)`) sem tocar o `~/.fpch` real nem
# o diretório de trabalho real de quem roda a suíte.
# --------------------------------------------------------------------------

def _cwd() -> Path:
    return Path.cwd()


def _home() -> Path:
    return Path.home()


def _resolve_source(cli_path: str | Path | None) -> tuple[Path | None, str, bool]:
    """Devolve (caminho, rótulo_de_origem, é_pedido_explícito).

    `caminho is None` significa "nenhum arquivo em nenhum nível: use o
    default". `é_pedido_explícito=True` significa que a ausência do arquivo
    é erro (níveis 1 e 2); nos níveis 3 e 4 a ausência apenas cede o lugar ao
    próximo nível da cascata.
    """
    if cli_path is not None:
        caminho = Path(cli_path)
        return caminho, f"linha de comando (--policy {caminho})", True

    env = os.environ.get("FPCH_POLICY")
    if env:
        caminho = Path(env)
        return caminho, f"variável de ambiente FPCH_POLICY={caminho}", True

    projeto = _cwd() / "fpch.policy.toml"
    if projeto.exists():
        return projeto, f"arquivo do projeto ({projeto})", False

    usuario = _home() / ".fpch" / "policy.toml"
    if usuario.exists():
        return usuario, f"arquivo do usuário ({usuario})", False

    return None, "default embutido", False


# --------------------------------------------------------------------------
# Validação estrita — tipo errado ou chave desconhecida falha alto e cedo,
# sempre nomeando bloco.campo e o arquivo.
# --------------------------------------------------------------------------

def _valida_tipo(valor: object, tipo: type | str, campo: str, caminho: Path) -> object:
    if tipo == "positive_int":
        if type(valor) is not int or valor <= 0:
            raise PolicyError(
                f"valor inválido para '{campo}' em {caminho}: "
                "esperado inteiro positivo"
            )
        return valor
    if tipo is int:
        # bool é subclasse de int em Python — sem esta checa, `true` no TOML
        # passaria como inteiro válido.
        if type(valor) is not int:
            raise PolicyError(
                f"valor de tipo errado para '{campo}' em {caminho}: "
                f"esperado inteiro, recebido {type(valor).__name__}"
            )
        return valor
    if tipo is str:
        if type(valor) is not str:
            raise PolicyError(
                f"valor de tipo errado para '{campo}' em {caminho}: "
                f"esperado texto, recebido {type(valor).__name__}"
            )
        return valor
    if tipo == "list_str":
        if not isinstance(valor, list) or not all(type(v) is str for v in valor):
            raise PolicyError(
                f"valor de tipo errado para '{campo}' em {caminho}: "
                f"esperado lista de textos"
            )
        return tuple(valor)
    raise AssertionError(f"tipo de validação desconhecido em policy.py: {tipo!r}")


def _extrai_tabela(dados: dict, nome_bloco: str, campos: dict[str, tuple[type | str, object]],
                    caminho: Path) -> dict:
    """Valida um bloco de tabela simples (`[bloco]`, não `[[bloco]]`) contra `campos`.

    `campos` mapeia nome_do_campo -> (tipo_esperado, default). Chave do TOML
    que não está em `campos` é erro; campo ausente usa o default.
    """
    bruto = dados.get(nome_bloco, {})
    if not isinstance(bruto, dict):
        raise PolicyError(f"'{nome_bloco}' deveria ser uma tabela TOML ([{nome_bloco}]), em {caminho}")
    desconhecidas = set(bruto) - set(campos)
    if desconhecidas:
        chave = sorted(desconhecidas)[0]
        raise PolicyError(f"chave desconhecida '{nome_bloco}.{chave}' em {caminho}")
    resultado: dict = {}
    for campo, (tipo, default) in campos.items():
        if campo in bruto:
            resultado[campo] = _valida_tipo(bruto[campo], tipo, f"{nome_bloco}.{campo}", caminho)
        else:
            resultado[campo] = default
    return resultado


_CAMPOS_META = {"versao": (int, 1)}
_CAMPOS_ESCALADA = {
    "max_escalations": (int, 2),
    "ordenar_por": ("list_str", ("cost", "-power")),
}
_CAMPOS_BACKENDS = {
    "timeout_s": (int, 600),
    "arg_limit": (int, 24_000),
}
_CAMPOS_VERIFICACAO = {
    "timeout_s": ("positive_int", 120),
}
_CAMPOS_FALHA = {
    "min_chars": (int, 40),
    "assinaturas": ("list_str", FalhaBlock().assinaturas),
}
_CAMPOS_MODELO = {
    "id": (str, None),
    "backend": (str, None),
    "pool": (str, None),
    "power": (int, None),
    "cost": (int, None),
    "good_for": ("list_str", None),
    "notes": (str, ""),
}
_MODELO_OBRIGATORIOS = frozenset({"id", "backend", "pool", "power", "cost", "good_for"})

_CAMPOS_HOOK = {
    "nome": (str, None),
    "cmd": ("list_str", None),
    "quando": (str, None),
    "criterio": (str, None),
}
_HOOK_OBRIGATORIOS = frozenset({"nome", "cmd", "quando", "criterio"})


def _extrai_array(dados: dict, nome_bloco: str, campos: dict[str, tuple[type | str, object]],
                   obrigatorios: frozenset[str], caminho: Path) -> list[dict]:
    """Valida um bloco de array-de-tabelas (`[[bloco]]`) contra `campos`."""
    bruto = dados.get(nome_bloco, [])
    if not isinstance(bruto, list):
        raise PolicyError(
            f"'{nome_bloco}' deveria ser uma lista de tabelas TOML ([[{nome_bloco}]]), em {caminho}"
        )
    itens = []
    for i, item in enumerate(bruto):
        rotulo = f"{nome_bloco}[{i}]"
        if not isinstance(item, dict):
            raise PolicyError(f"'{rotulo}' deveria ser uma tabela TOML, em {caminho}")
        faltantes = obrigatorios - set(item)
        if faltantes:
            chave = sorted(faltantes)[0]
            raise PolicyError(f"'{rotulo}' sem campo obrigatório '{chave}', em {caminho}")
        desconhecidas = set(item) - set(campos)
        if desconhecidas:
            chave = sorted(desconhecidas)[0]
            raise PolicyError(f"chave desconhecida '{rotulo}.{chave}' em {caminho}")
        valores: dict = {}
        for campo, (tipo, default) in campos.items():
            if campo in item:
                valores[campo] = _valida_tipo(item[campo], tipo, f"{rotulo}.{campo}", caminho)
            else:
                valores[campo] = default
        itens.append(valores)
    return itens


def _bloco_modelos(dados: dict, caminho: Path) -> tuple[ModelEntry, ...]:
    modelos = []
    for valores in _extrai_array(dados, "modelos", _CAMPOS_MODELO, _MODELO_OBRIGATORIOS, caminho):
        if valores["pool"] not in POOLS_VALIDOS:
            raise PolicyError(
                f"'modelos.pool' desconhecido: {valores['pool']!r} em {caminho} "
                f"(válidos: {sorted(POOLS_VALIDOS)})"
            )
        for tc in valores["good_for"]:
            if tc not in TASK_CLASSES_VALIDAS:
                raise PolicyError(
                    f"'modelos.good_for' cita classe de tarefa inexistente: {tc!r} em {caminho} "
                    f"(válidas: {sorted(TASK_CLASSES_VALIDAS)})"
                )
        modelos.append(ModelEntry(**valores))
    return tuple(modelos)


def _bloco_hooks(dados: dict, caminho: Path) -> tuple[HookEntry, ...]:
    hooks = [HookEntry(**v) for v in _extrai_array(dados, "hooks", _CAMPOS_HOOK, _HOOK_OBRIGATORIOS, caminho)]
    return tuple(hooks)


_BLOCOS_VALIDOS = frozenset(_TODOS_BLOCOS)


def _valida_topo(dados: dict, caminho: Path) -> None:
    desconhecidas = set(dados) - _BLOCOS_VALIDOS
    if desconhecidas:
        chave = sorted(desconhecidas)[0]
        raise PolicyError(f"chave desconhecida '{chave}' em {caminho}")


def _carrega_toml(caminho: Path) -> dict:
    try:
        with caminho.open("rb") as f:
            return tomllib.load(f)
    except tomllib.TOMLDecodeError as exc:
        raise PolicyError(f"TOML malformado em {caminho}: {exc}") from exc
    except OSError as exc:
        raise PolicyError(f"não foi possível ler o arquivo de política {caminho}: {exc}") from exc


# --------------------------------------------------------------------------
# API pública
# --------------------------------------------------------------------------

def load(cli_path: str | Path | None = None) -> Policy:
    """Carrega a política efetiva, seguindo a cascata de precedência de 5 níveis.

    `cli_path` é o valor de `--policy` na CLI (a Onda 2A é quem o preenche a
    partir de `argparse`). Ausência de arquivo em qualquer nível da cascata
    ambiente (`./fpch.policy.toml`, `~/.fpch/policy.toml`) cai silenciosamente
    para o próximo nível; um caminho explícito (`cli_path` ou `FPCH_POLICY`)
    que não existe é erro — foi pedido por nome, então "não encontrei" é
    informação, não um caso a mascarar.

    Nunca levanta por ausência de arquivo nos níveis ambiente. Sempre levanta
    `PolicyError` para TOML malformado, chave desconhecida, tipo errado, ou
    valor semanticamente inválido (pool ou classe de tarefa inexistente).
    """
    caminho, origem_label, obrigatorio = _resolve_source(cli_path)
    default = _default()

    if caminho is None:
        return default

    if not caminho.exists():
        if obrigatorio:
            raise PolicyError(f"arquivo de política não encontrado: {caminho}")
        return default

    dados = _carrega_toml(caminho)
    _valida_topo(dados, caminho)

    origem: dict[str, str] = {}

    def _origem(nome: str) -> None:
        origem[nome] = origem_label if nome in dados else "default embutido"

    for nome in _TODOS_BLOCOS:
        _origem(nome)

    meta = (
        MetaBlock(**_extrai_tabela(dados, "meta", _CAMPOS_META, caminho))
        if "meta" in dados else default.meta
    )
    escalada = (
        EscaladaBlock(**_extrai_tabela(dados, "escalada", _CAMPOS_ESCALADA, caminho))
        if "escalada" in dados else default.escalada
    )
    backends_bloco = (
        BackendsBlock(**_extrai_tabela(dados, "backends", _CAMPOS_BACKENDS, caminho))
        if "backends" in dados else default.backends
    )
    verificacao = (
        VerificacaoBlock(**_extrai_tabela(dados, "verificacao", _CAMPOS_VERIFICACAO, caminho))
        if "verificacao" in dados else default.verificacao
    )
    falha = (
        FalhaBlock(**_extrai_tabela(dados, "falha", _CAMPOS_FALHA, caminho))
        if "falha" in dados else default.falha
    )
    modelos = _bloco_modelos(dados, caminho) if "modelos" in dados else default.modelos
    hooks = _bloco_hooks(dados, caminho) if "hooks" in dados else default.hooks
    return Policy(
        meta=meta,
        escalada=escalada,
        backends=backends_bloco,
        verificacao=verificacao,
        falha=falha,
        modelos=modelos,
        hooks=hooks,
        causa_raiz=default.causa_raiz,
        origem=origem,
        fonte=origem_label,
    )


def dump(policy: Policy) -> str:
    """Despeja a política efetiva, bloco a bloco, com o valor E a origem de cada um.

    Depois lista, à parte e marcados como imutáveis, os quatro blocos que
    `IMMUTABLE_BLOCKS` documenta — eles nunca aparecem acima porque não fazem
    parte de `Policy`; aparecem aqui só para que `fpch config --dump` explique
    por que não estão configuráveis.
    """
    linhas: list[str] = []
    linhas.append("=== Política efetiva do FPCH ===")
    linhas.append("")

    def bloco(nome: str, campos: dict) -> None:
        linhas.append(f"[{nome}] (origem: {policy.origem.get(nome, 'default embutido')})")
        for chave, valor in campos.items():
            linhas.append(f"  {chave} = {valor!r}")
        linhas.append("")

    bloco("meta", {"versao": policy.meta.versao})
    bloco("escalada", {
        "max_escalations": policy.escalada.max_escalations,
        "ordenar_por": policy.escalada.ordenar_por,
    })
    bloco("backends", {
        "timeout_s": policy.backends.timeout_s,
        "arg_limit": policy.backends.arg_limit,
    })
    bloco("verificacao", {
        "timeout_s": policy.verificacao.timeout_s,
    })
    bloco("falha", {
        "min_chars": policy.falha.min_chars,
        "assinaturas": policy.falha.assinaturas,
    })

    linhas.append(
        f"[modelos] (origem: {policy.origem.get('modelos', 'default embutido')}) "
        f"— {len(policy.modelos)} modelo(s)"
    )
    for m in policy.modelos:
        linhas.append(
            f"  - {m.id} [{m.backend}/{m.pool}] power={m.power} cost={m.cost} "
            f"good_for={m.good_for}"
        )
    linhas.append("")

    linhas.append(
        f"[hooks] (origem: {policy.origem.get('hooks', 'default embutido')}) "
        f"— {len(policy.hooks)} hook(s)"
    )
    for h in policy.hooks:
        linhas.append(f"  - {h.nome}: cmd={list(h.cmd)} quando={h.quando} criterio={h.criterio}")
    linhas.append("")

    linhas.append("=== Blocos não-externalizáveis (imutáveis, fora deste arquivo) ===")
    linhas.append("  [causa_raiz] (fonte: audit.CAUSE_TAXONOMY)")
    linhas.append(f"    categorias = {policy.causa_raiz.categorias!r}")
    linhas.append(f"    subtipos = {policy.causa_raiz.subtipos!r}")
    for b in IMMUTABLE_BLOCKS:
        linhas.append(f"  - {b.nome} ({b.referencia})")
        linhas.append(f"    {b.razao}")
    linhas.append("")

    return "\n".join(linhas)
