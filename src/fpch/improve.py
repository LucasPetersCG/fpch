"""O laço de auto-aprimoramento do FPCH — Camada 5, a que propõe sem poder decidir.

Implementa a seção 8 de `docs/decisoes/fatia-funcional-2026-08-31.md`. É a última
onda da fatia funcional, e a única em que o artefato escreve sobre si mesmo — daí
ser também a que mais precisa declarar o que **não** faz.

**Propor e autorizar são operações distintas.** `propor()` lê a trilha, analisa e
grava um arquivo JSON em `~/.fpch/propostas/<id>.json`. Nunca toca a política.
`aplicar()` é outra chamada, com outro comando, e exige que o humano nomeie o
arquivo de política que será editado. Essa separação é o critério de aceitação que
a `TCC_v4` deriva de Xu et al. (ECLoop) e é o núcleo da contribuição do trabalho:
um laço em que o agente propõe e também autoriza não é um laço com portão — é um
agente com permissão de escrita e uma cerimônia em volta.

Uma proposta só nasce quando as **três** condições se cumprem, e a ausência de
qualquer uma é reportada com a razão, nunca contornada:

1. **Evidência auditável.** Existe evento `verify` com `verdict == "fail"` e
   `evidence` não vazia, numa trajetória cuja cadeia de hash está íntegra. A
   integridade é conferida com `audit.verify_chain()`, não presumida: a trilha é
   produzida pelo mesmo operador que a lê, e confiar nela sem conferir seria
   exatamente a promessa vazia que o trabalho inteiro combate.
2. **Lado da falta na infraestrutura.** `fault_side == "infraestrutura"`. Falha do
   lado do modelo não se conserta editando documento de política; tentar produz a
   regra inócua que Wang et al. descrevem — proteção fantasma, que consome
   inspeção humana sem mudar comportamento nenhum. **Recusar por este motivo é
   resultado correto, não erro.**
3. **Caminho de remoção previsto.** Toda proposta declara o que adiciona **e** o
   que a tornaria removível. Sem isso a política cresce monotonicamente até deixar
   de ser inspecionável — que era a sua única vantagem sobre a regra embutida no
   código. Aqui essa condição não é um campo de texto livre a preencher: uma
   proposta só existe se casar com uma `REGRAS` abaixo, e cada regra carrega o seu
   critério de remoção **conferível a partir da própria trilha**.

Duas decisões de projeto que merecem registro:

**A recusa é gravada na trilha, e é gravada como `verify`.** Não se inventou um
tipo de evento novo: a decisão de aplicar (ou não) *é* uma verificação, com
componente, veredito e evidência, e reusar o vocabulário existente faz a recusa
aparecer em `audit.summary()` e ser coberta por `verify_chain()` sem código novo.

**Os eventos deste módulo nascem com `fault_side` vazio, de propósito.** Marcá-los
como `"infraestrutura"` faria a recusa de uma proposta virar evidência admissível
para a proposta seguinte — um laço que se alimenta do próprio ruído. Por segurança
dupla, `_candidatos()` também ignora todo evento cujo `component` comece com
`improve.`.

Nenhuma dependência externa: TOML entra por `tomllib` (leitura, via `policy`) e sai
por um escritor mínimo aqui dentro, restrito aos tipos que uma proposta pode
carregar. Nada de `shell=True`, nada de rede. Toda `evidence` lida da trilha é
**dado**: é casada contra uma lista fechada de gatilhos, jamais interpretada.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import uuid
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path

from . import audit, policy as policy_mod
from .policy import IMMUTABLE_BLOCKS, Policy

#: Versão do formato do arquivo de proposta. Proposta gravada por uma versão
#: futura é recusada em vez de reinterpretada — adivinhar o formato de um arquivo
#: que autoriza escrita na política seria trocar o portão por um palpite.
PROPOSTA_SCHEMA = 1

#: Blocos que a seção 5.2 declara externalizáveis. Espelha à mão os campos de
#: `policy.Policy` (menos `origem` e `fonte`, que são metadados de carga, não
#: política). `tests/test_improve.py::test_blocos_externalizaveis_espelham_policy`
#: é quem impede este espelho de divergir.
BLOCOS_EXTERNALIZAVEIS = ("meta", "escalada", "backends", "falha", "modelos", "hooks", "causa_raiz")

#: Nomes dos blocos que a seção 5.3 proíbe externalizar. Vêm de `policy`, não de
#: uma cópia: se a lista de lá crescer, o portão daqui cresce junto.
BLOCOS_IMUTAVEIS = tuple(b.nome for b in IMMUTABLE_BLOCKS)

#: Teto do timeout que uma proposta pode pedir. Existe porque "dobrar o timeout"
#: sem teto é um caminho de crescimento monotônico como qualquer outro — só que
#: medido em segundos de espera do autor.
TETO_TIMEOUT_S = 3600

#: Componentes cujos eventos nunca são candidatos: os do próprio laço.
PREFIXO_PROPRIO = "improve."

COMPONENTE_PROPOR = "improve.propose"
COMPONENTE_APLICAR = "improve.apply"

#: Razões de recusa. Códigos estáveis porque são o que a trilha guarda — texto de
#: mensagem pode ser reescrito, código não.
RECUSA_SEM_EVIDENCIA = "sem_evidencia_auditavel"        # condição (a)
RECUSA_CADEIA_QUEBRADA = "cadeia_quebrada"              # condição (a)
RECUSA_LADO_MODELO = "falta_do_lado_do_modelo"          # condição (b)
RECUSA_SEM_REMOCAO = "sem_caminho_de_remocao"           # condição (c)
RECUSA_SEM_MUDANCA = "sem_mudanca_a_propor"
RECUSA_PROPOSTA_DESCONHECIDA = "proposta_desconhecida"
RECUSA_SCHEMA = "schema_de_proposta_desconhecido"
RECUSA_BLOCO_IMUTAVEL = "bloco_imutavel"
RECUSA_BLOCO_INEXISTENTE = "bloco_nao_externalizavel"
RECUSA_TRAJETORIA_AUSENTE = "trajetoria_ausente"
RECUSA_VALOR_DIVERGENTE = "valor_atual_divergente"
RECUSA_POLITICA_INVALIDA = "politica_invalida"


def _agora_iso() -> str:
    return datetime.now().astimezone().isoformat(timespec="microseconds")


def _propostas_dir_padrao() -> Path:
    override = os.environ.get("FPCH_PROPOSTAS_DIR")
    if override:
        return Path(override)
    return Path.home() / ".fpch" / "propostas"


# ---------------------------------------------------------------------------
# Regras de proposta — a condição (c) em forma executável
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class Regra:
    """Um par (evidência reconhecível → alteração com remoção declarada).

    A lista `REGRAS` é fechada de propósito. A alternativa — deixar o laço
    inventar a alteração a partir do texto da evidência — daria ao agente a
    liberdade de propor qualquer coisa sobre qualquer bloco, e é justamente essa
    liberdade que a condição (c) existe para negar: sem um caminho de remoção
    conhecido *de antemão*, a proposta não pode declarar como sairia da política.
    """

    nome: str
    bloco: str
    campo: str
    gatilhos: tuple[str, ...]


REGRA_TIMEOUT = Regra(
    nome="ampliar_timeout",
    bloco="backends",
    campo="timeout_s",
    gatilhos=("timeout", "estourou o tempo", "timed out"),
)

REGRA_ASSINATURA = Regra(
    nome="reconhecer_assinatura_de_falha",
    bloco="falha",
    campo="assinaturas",
    gatilhos=(
        "no output produced",
        "auto-denied",
        "quota exceeded",
        "rate limit exceeded",
        "ineligibletiererror",
    ),
)

REGRAS: tuple[Regra, ...] = (REGRA_TIMEOUT, REGRA_ASSINATURA)


def _casa_regra(evidence: str) -> tuple[Regra, str] | None:
    """Primeira regra cujo gatilho aparece na evidência. Devolve `(regra, gatilho)`.

    Comparação por substring em minúsculas, sem regex sobre dado externo: a
    evidência é saída de terceiro e entra aqui como texto a ser *casado*, nunca
    como padrão a ser compilado.
    """
    baixa = evidence.lower()
    for regra in REGRAS:
        for gatilho in regra.gatilhos:
            if gatilho in baixa:
                return regra, gatilho
    return None


# ---------------------------------------------------------------------------
# Integridade por trajetória
# ---------------------------------------------------------------------------

def trajetorias_integras(trilha: Path | None = None) -> tuple[set[str], int | None]:
    """Trajetórias cuja cadeia de hash está íntegra, e o índice do primeiro defeito.

    `audit.verify_chain()` responde sobre o arquivo inteiro; a condição (a) fala de
    *uma trajetória*. A tradução adotada é a conservadora: a cadeia é sequencial,
    então uma quebra no índice físico *i* condena tudo que está em *i* ou depois.
    Uma trajetória é íntegra se, e somente se, **todos** os seus eventos estiverem
    antes da quebra. Trajetória que atravessa a quebra é tratada como adulterada,
    ainda que algum evento seu seja anterior a ela — na dúvida, a evidência não é
    admissível, que é o único lado seguro deste erro.
    """
    alvo = trilha or audit._default_log_path()
    ok, quebra = audit.verify_chain(alvo)
    if not alvo.exists():
        return set(), quebra

    vistas: set[str] = set()
    contaminadas: set[str] = set()
    indice = -1
    with alvo.open(encoding="utf-8") as fh:
        for linha in fh:
            linha = linha.strip()
            if not linha:
                continue
            indice += 1
            try:
                row = json.loads(linha)
            except json.JSONDecodeError:
                continue
            if not isinstance(row, dict):
                continue
            tid = str(row.get("trajectory_id") or "")
            if not tid:
                continue
            vistas.add(tid)
            if not ok and quebra is not None and indice >= quebra:
                contaminadas.add(tid)
    if ok:
        return vistas, None
    return vistas - contaminadas, quebra


# ---------------------------------------------------------------------------
# Proposta e recusa
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class Recusa:
    """Uma proposta que não nasceu, com a razão em código estável e em texto."""

    codigo: str
    mensagem: str
    trajectory_id: str = ""
    component: str = ""

    def __str__(self) -> str:
        onde = f" [{self.trajectory_id}/{self.component}]" if self.trajectory_id else ""
        return f"RECUSA {self.codigo}{onde}: {self.mensagem}"


@dataclass(frozen=True)
class Proposta:
    """Alteração proposta a **um** campo de **um** bloco externalizável.

    Um campo por proposta, sem exceção: proposta que altera três coisas de uma vez
    obriga o humano a aprovar as três juntas ou rejeitar as três juntas, e o portão
    passa a valer menos do que aparenta.
    """

    id: str
    criado_em: str
    trajectory_id: str
    component: str
    evidence: str
    regra: str
    gatilho: str
    bloco: str
    campo: str
    valor_atual: object
    valor_proposto: object
    justificativa: str
    remocao: str
    fault_side: str = "infraestrutura"
    cause_category: str | None = None
    schema_version: int = PROPOSTA_SCHEMA
    status: str = "proposta"
    aplicado_em: str | None = None

    def to_json(self) -> dict:
        d = asdict(self)
        for chave in ("valor_atual", "valor_proposto"):
            if isinstance(d[chave], tuple):
                d[chave] = list(d[chave])
        return d


@dataclass(frozen=True)
class Analise:
    """Resultado de `propor()`: no máximo uma proposta, mais todas as recusas."""

    proposta: Proposta | None = None
    caminho: Path | None = None
    recusas: tuple[Recusa, ...] = ()
    trilha: Path | None = None
    quebra: int | None = None

    @property
    def exit_code(self) -> int:
        """0 só quando a proposta existe.

        Sair com 0 sem ter proposto nada faria um laço automatizado ler "houve
        aprimoramento" onde houve recusa — a mesma confusão entre "aprovado" e
        "não verificado" que `hooks.overall()` recusa para o conjunto vazio.
        """
        return 0 if self.proposta is not None else 1


@dataclass(frozen=True)
class ResultadoAplicacao:
    """Resultado de `aplicar()`. `ok=False` sempre traz `recusa` preenchida."""

    ok: bool
    proposta_id: str
    recusa: Recusa | None = None
    policy_path: Path | None = None
    aplicado: str = ""
    trilha_ok: bool = True
    #: `True` quando a recusa é de uso (id inexistente, formato desconhecido) e
    #: não um veredito sobre a proposta — ver os códigos de saída em `cli.py`.
    uso_incorreto: bool = False

    @property
    def exit_code(self) -> int:
        if self.ok:
            return 0
        return 2 if self.uso_incorreto else 1


# ---------------------------------------------------------------------------
# Leitura da trilha e seleção de candidatos
# ---------------------------------------------------------------------------

def _candidatos(trilha: Path | None) -> list[dict]:
    """Eventos `verify` com `verdict == "fail"`, do mais recente para o mais antigo.

    Eventos emitidos pelo próprio laço (`component` começando com `improve.`) são
    excluídos: a recusa de uma proposta é uma falha *da proposta*, não do projeto,
    e admiti-la como evidência faria o laço se alimentar do próprio ruído.
    """
    linhas = audit.read_all(trilha)
    vistos = [
        r for r in linhas
        if r.get("event") == "verify"
        and r.get("verdict") == "fail"
        and not str(r.get("component") or "").startswith(PREFIXO_PROPRIO)
    ]
    # `read_all` ordena por (trajectory_id, seq); a ordem que interessa aqui é a
    # temporal, e `ts` é o único campo comparável entre trajetórias diferentes.
    vistos.sort(key=lambda r: str(r.get("ts") or ""), reverse=True)
    return vistos


def _valor_atual(pol: Policy, regra: Regra) -> object:
    bloco = getattr(pol, regra.bloco)
    return getattr(bloco, regra.campo)


def _proposta_de(regra: Regra, gatilho: str, ev: dict, pol: Policy) -> tuple[Proposta | None, Recusa | None]:
    """Materializa a alteração de uma regra, ou explica por que não há alteração."""
    tid = str(ev.get("trajectory_id") or "")
    comp = str(ev.get("component") or "?")
    evidence = str(ev.get("evidence") or "")
    atual = _valor_atual(pol, regra)

    if regra is REGRA_TIMEOUT:
        if not isinstance(atual, int) or atual >= TETO_TIMEOUT_S:
            return None, Recusa(
                RECUSA_SEM_MUDANCA,
                f"[backends].timeout_s já está em {atual}s, no teto de {TETO_TIMEOUT_S}s que este "
                "laço pode pedir. Um teto maior é decisão do autor, não do laço.",
                tid, comp,
            )
        proposto = min(atual * 2, TETO_TIMEOUT_S)
        justificativa = (
            f"O hook {comp!r} reprovou por estouro de tempo (gatilho {gatilho!r} na evidência), com "
            f"`fault_side=infraestrutura`. O teto vigente de {atual}s é política externalizável "
            "([backends].timeout_s, seção 5.2), então esta é uma falta que um documento de política "
            "de fato pode corrigir."
        )
        remocao = (
            f"Reverter [backends].timeout_s para {atual} assim que 5 execuções consecutivas de "
            f"{comp!r} concluírem abaixo de {atual}s. O critério é conferível na própria trilha: "
            "todo evento carrega `latency_s`, e nenhuma instrumentação nova é necessária para "
            "decidir a remoção."
        )
    else:  # REGRA_ASSINATURA
        atual_tupla = tuple(atual) if isinstance(atual, (tuple, list)) else ()
        if gatilho in {s.lower() for s in atual_tupla}:
            return None, Recusa(
                RECUSA_SEM_MUDANCA,
                f"a assinatura {gatilho!r} já consta de [falha].assinaturas; a política já reconhece "
                "esta condição como falha e não há o que acrescentar.",
                tid, comp,
            )
        proposto = tuple(atual_tupla) + (gatilho,)
        atual = atual_tupla
        justificativa = (
            f"A evidência de {comp!r} contém {gatilho!r}, uma condição que o backend devolve com "
            "código de saída zero — o `exit == 0` que não significa sucesso, documentado em "
            "backends.py. Reconhecê-la em [falha].assinaturas (seção 5.2) é o que impede a promessa "
            "que falhou de ser lida como promessa cumprida."
        )
        remocao = (
            f"Remover {gatilho!r} de [falha].assinaturas assim que o backend passar a sinalizar esta "
            "condição no código de saída. A heurística de texto existe apenas enquanto `exit == 0` "
            "mentir; quando parar de mentir, ela vira peso morto e sai."
        )

    ident = "prop-" + hashlib.sha256(
        "|".join([tid, comp, regra.nome, gatilho, evidence]).encode("utf-8")
    ).hexdigest()[:12]

    return Proposta(
        id=ident,
        criado_em=_agora_iso(),
        trajectory_id=tid,
        component=comp,
        evidence=evidence,
        regra=regra.nome,
        gatilho=gatilho,
        bloco=regra.bloco,
        campo=regra.campo,
        valor_atual=atual,
        valor_proposto=proposto,
        justificativa=justificativa,
        remocao=remocao,
        fault_side=str(ev.get("fault_side") or ""),
        cause_category=ev.get("cause_category"),
    ), None


def _diagnostica(ev: dict, pol: Policy, integras: set[str], quebra: int | None
                 ) -> tuple[Proposta | None, Recusa | None]:
    """Aplica as três condições, nesta ordem, a um candidato."""
    tid = str(ev.get("trajectory_id") or "")
    comp = str(ev.get("component") or "?")
    evidence = str(ev.get("evidence") or "").strip()

    # (a) evidência auditável — parte 1: a evidência existe.
    if not evidence:
        return None, Recusa(
            RECUSA_SEM_EVIDENCIA,
            "condição (a) ausente: o evento `verify` reprovou mas não deixou `evidence`. "
            "Veredito sem evidência não é evidência auditável — é a palavra do verificador.",
            tid, comp,
        )

    # (a) parte 2: a cadeia da trajetória está íntegra.
    if tid not in integras:
        return None, Recusa(
            RECUSA_CADEIA_QUEBRADA,
            "condição (a) ausente: a cadeia de hash desta trajetória não confere "
            + (f"(primeira quebra no índice físico {quebra} da trilha). " if quebra is not None else ". ")
            + "Evidência de uma trilha adulterada não sustenta proposta nenhuma.",
            tid, comp,
        )

    # (b) lado da falta.
    lado = ev.get("fault_side")
    if lado != "infraestrutura":
        return None, Recusa(
            RECUSA_LADO_MODELO,
            f"condição (b) ausente: `fault_side={lado!r}`, não 'infraestrutura'. Falha do lado do "
            "modelo não se conserta editando documento de política — a regra que sairia daí seria "
            "inócua, consumindo inspeção humana sem mudar comportamento. Esta recusa é o resultado "
            "correto, não um erro do laço.",
            tid, comp,
        )

    # (c) caminho de remoção previsto.
    casada = _casa_regra(evidence)
    if casada is None:
        return None, Recusa(
            RECUSA_SEM_REMOCAO,
            "condição (c) ausente: nenhuma regra conhecida casa com esta evidência, logo não há como "
            "declarar o que tornaria a alteração removível. Propor mesmo assim faria a política "
            "crescer sem critério de saída, perdendo a inspecionabilidade que era a sua única "
            f"vantagem. Regras conhecidas: {', '.join(r.nome for r in REGRAS)}.",
            tid, comp,
        )

    regra, gatilho = casada
    return _proposta_de(regra, gatilho, ev, pol)


# ---------------------------------------------------------------------------
# Registro na trilha
# ---------------------------------------------------------------------------

def _registra(componente: str, verdict: str, evidence: str, *,
              trajectory_id: str, trilha: Path | None, label: str | None = None) -> bool:
    """Grava a decisão do portão como evento `verify`.

    `fault_side` fica vazio de propósito — ver o docstring do módulo.
    """
    tid = trajectory_id or f"improve-{uuid.uuid4().hex[:12]}"
    return audit.write(
        audit.Event(
            event="verify",
            trajectory_id=tid,
            seq=audit.next_seq(tid),
            label=label,
            component=componente,
            verdict=verdict,
            evidence=evidence[:500],
        ),
        path=trilha,
    )


# ---------------------------------------------------------------------------
# API pública — propor
# ---------------------------------------------------------------------------

def salvar(proposta: Proposta, *, propostas_dir: Path | None = None) -> Path:
    """Grava a proposta em `<propostas_dir>/<id>.json`.

    O `id` é derivado por hash do que a sustenta (trajetória, componente, regra,
    evidência), e não de um `uuid4()`: rodar `fpch improve` duas vezes sobre a
    mesma trilha reescreve o mesmo arquivo em vez de encher o diretório de cópias
    da mesma proposta com nomes diferentes.
    """
    destino = propostas_dir or _propostas_dir_padrao()
    destino.mkdir(parents=True, exist_ok=True)
    caminho = destino / f"{proposta.id}.json"
    caminho.write_text(
        json.dumps(proposta.to_json(), ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return caminho


def carregar(proposta_id: str, *, propostas_dir: Path | None = None) -> dict | None:
    destino = propostas_dir or _propostas_dir_padrao()
    caminho = destino / f"{proposta_id}.json"
    if not caminho.exists():
        return None
    try:
        dados = json.loads(caminho.read_text(encoding="utf-8"))
    except (ValueError, OSError):
        return None
    return dados if isinstance(dados, dict) else None


def propor(pol: Policy, *, trilha: Path | None = None, propostas_dir: Path | None = None,
           label: str | None = None) -> Analise:
    """Lê a trilha, analisa e — no máximo — grava **uma** proposta. Nunca edita a política.

    Percorre os candidatos do mais recente para o mais antigo e para no primeiro
    que cumpre as três condições. Os candidatos anteriores viram recusas com razão,
    e as recusas são gravadas na trilha (seção 3 do contrato: proposta sem
    evidência auditável é recusada, **com a recusa registrada**).
    """
    integras, quebra = trajetorias_integras(trilha)
    candidatos = _candidatos(trilha)

    if not candidatos:
        recusa = Recusa(
            RECUSA_SEM_EVIDENCIA,
            "condição (a) ausente: a trilha não tem nenhum evento `verify` com `verdict=\"fail\"`. "
            "Sem falha observada não há o que propor — e inventar uma seria propor sem evidência.",
        )
        _registra(COMPONENTE_PROPOR, "fail", str(recusa), trajectory_id="", trilha=trilha, label=label)
        return Analise(recusas=(recusa,), trilha=trilha, quebra=quebra)

    recusas: list[Recusa] = []
    for ev in candidatos:
        proposta, recusa = _diagnostica(ev, pol, integras, quebra)
        if proposta is not None:
            caminho = salvar(proposta, propostas_dir=propostas_dir)
            return Analise(
                proposta=proposta,
                caminho=caminho,
                recusas=tuple(recusas),
                trilha=trilha,
                quebra=quebra,
            )
        if recusa is not None:
            recusas.append(recusa)

    for r in recusas:
        _registra(COMPONENTE_PROPOR, "fail", str(r), trajectory_id=r.trajectory_id,
                  trilha=trilha, label=label)
    return Analise(recusas=tuple(recusas), trilha=trilha, quebra=quebra)


# ---------------------------------------------------------------------------
# Escritor TOML mínimo — só o que uma proposta pode carregar
# ---------------------------------------------------------------------------

class TomlNaoSuportado(RuntimeError):
    """Valor que este escritor mínimo se recusa a serializar."""


def _toml_valor(valor: object) -> str:
    """Serializa inteiro, texto e lista de textos. Qualquer outra coisa é recusada.

    Recusar é deliberado: um escritor genérico convidaria propostas a mexer em
    estruturas que ninguém revisou o suficiente para deixar um agente reescrever.
    Aspas e escapes coincidem com JSON para o conteúdo admitido aqui (sem
    caracteres de controle), então `json.dumps` serve — e é da biblioteca padrão.
    """
    if isinstance(valor, bool):
        raise TomlNaoSuportado("booleano não é valor proposável nesta fatia")
    if isinstance(valor, int):
        return str(valor)
    if isinstance(valor, str):
        return json.dumps(valor, ensure_ascii=False)
    if isinstance(valor, (list, tuple)):
        itens = []
        for x in valor:
            if not isinstance(x, str):
                raise TomlNaoSuportado(f"lista só pode conter textos, recebido {type(x).__name__}")
            itens.append(json.dumps(x, ensure_ascii=False))
        return "[" + ", ".join(itens) + "]"
    raise TomlNaoSuportado(f"tipo não serializável: {type(valor).__name__}")


def _e_cabecalho(linha: str) -> bool:
    s = linha.strip()
    return s.startswith("[") and s.endswith("]")


def escreve_chave(texto: str, bloco: str, campo: str, valor: object) -> str:
    """Devolve o TOML com `bloco.campo` valendo `valor`, preservando o resto do arquivo.

    Edição cirúrgica, e não regravação do `Policy` inteiro, por uma razão: regravar
    materializaria no arquivo do autor todos os defaults embutidos que ele
    deliberadamente não escreveu, transformando uma proposta de uma linha numa
    reescrita de política inteira que ninguém aprovou.
    """
    rendido = f"{campo} = {_toml_valor(valor)}"
    linhas = texto.splitlines()

    inicio = None
    for i, ln in enumerate(linhas):
        if ln.strip() == f"[{bloco}]":
            inicio = i
            break

    if inicio is None:
        base = "\n".join(linhas).rstrip("\n")
        prefixo = base + "\n\n" if base else ""
        return f"{prefixo}[{bloco}]\n{rendido}\n"

    fim = len(linhas)
    for j in range(inicio + 1, len(linhas)):
        if _e_cabecalho(linhas[j]):
            fim = j
            break

    padrao = re.compile(rf"^\s*{re.escape(campo)}\s*=")
    for j in range(inicio + 1, fim):
        if padrao.match(linhas[j]):
            # consome a continuação de um array multilinha
            k = j
            saldo = linhas[j].count("[") - linhas[j].count("]")
            while saldo > 0 and k + 1 < fim:
                k += 1
                saldo += linhas[k].count("[") - linhas[k].count("]")
            return "\n".join(linhas[:j] + [rendido] + linhas[k + 1:]).rstrip("\n") + "\n"

    return "\n".join(linhas[:fim] + [rendido] + linhas[fim:]).rstrip("\n") + "\n"


# ---------------------------------------------------------------------------
# API pública — aplicar
# ---------------------------------------------------------------------------

def _recusa_aplicacao(codigo: str, mensagem: str, prop_id: str, tid: str, comp: str,
                      *, trilha: Path | None, uso: bool = False) -> ResultadoAplicacao:
    recusa = Recusa(codigo, mensagem, tid, comp)
    ok_trilha = _registra(
        COMPONENTE_APLICAR, "fail",
        f"proposta {prop_id}: {recusa}",
        trajectory_id=tid, trilha=trilha,
    )
    return ResultadoAplicacao(
        ok=False, proposta_id=prop_id, recusa=recusa, trilha_ok=ok_trilha, uso_incorreto=uso,
    )


def aplicar(proposta_id: str, *, policy_path: Path, trilha: Path | None = None,
            propostas_dir: Path | None = None) -> ResultadoAplicacao:
    """Aplica uma proposta a um arquivo de política **nomeado pelo humano**.

    `policy_path` é obrigatório e não é resolvido pela cascata de propósito: a
    cascata escolhe qual política *vale*; deixá-la escolher qual arquivo é
    *editado* faria o laço escrever num arquivo que ninguém apontou.

    Reconfere, nesta ordem: formato da proposta; bloco alvo externalizável e não
    imutável; existência da trajetória citada na trilha; integridade da cadeia; e
    coincidência entre o `valor_atual` da proposta e o que a política diz hoje.
    **Toda recusa vira evento `verify` na trilha, com a razão.** Se a política
    resultante não carregar, o arquivo é restaurado byte a byte e a aplicação é
    recusada — nunca se deixa para trás uma política quebrada.
    """
    dados = carregar(proposta_id, propostas_dir=propostas_dir)
    if dados is None:
        return _recusa_aplicacao(
            RECUSA_PROPOSTA_DESCONHECIDA,
            f"não há proposta com id {proposta_id!r} em "
            f"{propostas_dir or _propostas_dir_padrao()}.",
            proposta_id, "", "", trilha=trilha, uso=True,
        )

    tid = str(dados.get("trajectory_id") or "")
    comp = str(dados.get("component") or "")

    if dados.get("schema_version") != PROPOSTA_SCHEMA:
        return _recusa_aplicacao(
            RECUSA_SCHEMA,
            f"proposta com schema_version={dados.get('schema_version')!r}; este laço só aplica "
            f"{PROPOSTA_SCHEMA}. Adivinhar o formato de um arquivo que autoriza escrita na política "
            "seria trocar o portão por um palpite.",
            proposta_id, tid, comp, trilha=trilha, uso=True,
        )

    bloco = str(dados.get("bloco") or "")
    campo = str(dados.get("campo") or "")

    if bloco in BLOCOS_IMUTAVEIS:
        return _recusa_aplicacao(
            RECUSA_BLOCO_IMUTAVEL,
            f"o bloco {bloco!r} está em IMMUTABLE_BLOCKS (seção 5.3): é ele próprio a defesa contra "
            "conteúdo hostil, e por isso não é externalizável. Aplicar aqui entregaria a defesa ao "
            "mesmo laço automatizado que ela existe para conter.",
            proposta_id, tid, comp, trilha=trilha,
        )

    if bloco not in BLOCOS_EXTERNALIZAVEIS or not campo:
        return _recusa_aplicacao(
            RECUSA_BLOCO_INEXISTENTE,
            f"bloco {bloco!r} / campo {campo!r} não é bloco externalizável da seção 5.2 "
            f"(válidos: {', '.join(BLOCOS_EXTERNALIZAVEIS)}).",
            proposta_id, tid, comp, trilha=trilha,
        )

    eventos = audit.read_all(trilha)
    if not tid or tid not in {str(r.get("trajectory_id") or "") for r in eventos}:
        return _recusa_aplicacao(
            RECUSA_TRAJETORIA_AUSENTE,
            f"a trajetória {tid!r} citada pela proposta não existe na trilha. Proposta que aponta "
            "para evidência inexistente não é proposta, é alegação.",
            proposta_id, tid, comp, trilha=trilha,
        )

    integras, quebra = trajetorias_integras(trilha)
    if tid not in integras:
        return _recusa_aplicacao(
            RECUSA_CADEIA_QUEBRADA,
            f"a cadeia de hash da trilha não confere mais"
            + (f" (primeira quebra no índice físico {quebra})" if quebra is not None else "")
            + f"; a trajetória {tid!r} deixou de ser auditável entre a proposta e a aplicação. "
            "A evidência foi conferida na proposta e é reconferida aqui exatamente porque o "
            "arquivo pode ter mudado nesse intervalo.",
            proposta_id, tid, comp, trilha=trilha,
        )

    try:
        pol_atual = policy_mod.load(policy_path) if policy_path.exists() else policy_mod.load(None)
    except policy_mod.PolicyError as exc:
        return _recusa_aplicacao(
            RECUSA_POLITICA_INVALIDA,
            f"a política em {policy_path} não carrega hoje: {exc}. Aplicar sobre um arquivo já "
            "quebrado esconderia o defeito original.",
            proposta_id, tid, comp, trilha=trilha, uso=True,
        )

    try:
        vigente = getattr(getattr(pol_atual, bloco), campo)
    except AttributeError:
        return _recusa_aplicacao(
            RECUSA_BLOCO_INEXISTENTE,
            f"a política não tem o campo {bloco}.{campo}.",
            proposta_id, tid, comp, trilha=trilha,
        )

    esperado = dados.get("valor_atual")
    if _normaliza(vigente) != _normaliza(esperado):
        return _recusa_aplicacao(
            RECUSA_VALOR_DIVERGENTE,
            f"{bloco}.{campo} vale {vigente!r} hoje, mas a proposta foi calculada sobre "
            f"{esperado!r}. A política mudou desde a análise; refaça `fpch improve`.",
            proposta_id, tid, comp, trilha=trilha,
        )

    original = policy_path.read_text(encoding="utf-8") if policy_path.exists() else ""
    try:
        novo = escreve_chave(original, bloco, campo, dados.get("valor_proposto"))
    except TomlNaoSuportado as exc:
        return _recusa_aplicacao(
            RECUSA_BLOCO_INEXISTENTE,
            f"valor proposto não é serializável nesta fatia: {exc}.",
            proposta_id, tid, comp, trilha=trilha,
        )

    policy_path.parent.mkdir(parents=True, exist_ok=True)
    policy_path.write_text(novo, encoding="utf-8")
    try:
        policy_mod.load(policy_path)
    except policy_mod.PolicyError as exc:
        if original:
            policy_path.write_text(original, encoding="utf-8")
        else:
            policy_path.unlink(missing_ok=True)
        return _recusa_aplicacao(
            RECUSA_POLITICA_INVALIDA,
            f"a política resultante não carregaria ({exc}); o arquivo foi restaurado ao estado "
            "anterior. Nunca se deixa para trás uma política quebrada.",
            proposta_id, tid, comp, trilha=trilha,
        )

    aplicado = f"{bloco}.{campo}: {dados.get('valor_atual')!r} → {dados.get('valor_proposto')!r}"
    trilha_ok = _registra(
        COMPONENTE_APLICAR, "pass",
        f"proposta {proposta_id} aplicada em {policy_path}: {aplicado}. "
        f"Remoção prevista: {dados.get('remocao')}",
        trajectory_id=tid, trilha=trilha,
    )

    marcado = dict(dados)
    marcado["status"] = "aplicada"
    marcado["aplicado_em"] = _agora_iso()
    destino = (propostas_dir or _propostas_dir_padrao()) / f"{proposta_id}.json"
    destino.write_text(
        json.dumps(marcado, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )

    return ResultadoAplicacao(
        ok=True, proposta_id=proposta_id, policy_path=policy_path,
        aplicado=aplicado, trilha_ok=trilha_ok,
    )


def _normaliza(valor: object) -> object:
    """Tupla e lista com o mesmo conteúdo são o mesmo valor — o JSON perde a distinção."""
    if isinstance(valor, (list, tuple)):
        return tuple(valor)
    return valor


# ---------------------------------------------------------------------------
# Formatação
# ---------------------------------------------------------------------------

def format_analise(an: Analise) -> str:
    linhas = ["=== fpch improve ==="]
    if an.quebra is not None:
        linhas.append(
            f"  AVISO: cadeia da trilha quebrada a partir do índice físico {an.quebra}. "
            "Trajetórias a partir dali não são evidência admissível."
        )
    if an.proposta is not None:
        p = an.proposta
        linhas.append(f"  proposta {p.id} (regra {p.regra})")
        linhas.append(f"    trajetória: {p.trajectory_id}   componente: {p.component}")
        linhas.append(f"    altera:     [{p.bloco}].{p.campo}: {p.valor_atual!r} → {p.valor_proposto!r}")
        linhas.append(f"    porque:     {p.justificativa}")
        linhas.append(f"    remoção:    {p.remocao}")
        linhas.append(f"    arquivo:    {an.caminho}")
        linhas.append("")
        linhas.append("  NADA foi alterado na política. Para autorizar:")
        linhas.append(f"    fpch --policy <arquivo.toml> improve --apply {p.id}")
    else:
        linhas.append("  nenhuma proposta gerada.")
    for r in an.recusas:
        linhas.append(f"  {r}")
    return "\n".join(linhas)


def format_resultado(res: ResultadoAplicacao) -> str:
    if res.ok:
        saida = [
            f"proposta {res.proposta_id} aplicada em {res.policy_path}",
            f"  {res.aplicado}",
        ]
    else:
        saida = [f"proposta {res.proposta_id} NÃO aplicada.", f"  {res.recusa}"]
    if not res.trilha_ok:
        saida.append("  AVISO: a decisão NÃO foi registrada na trilha de auditoria.")
    return "\n".join(saida)
