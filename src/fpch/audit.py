"""Trilha de auditoria do FPCH — eventos por trajetória, encadeados por hash.

Cada evento vira uma linha JSONL. Serve a três propósitos:

1. Operacional — descobrir qual pool está sendo drenado e por quê.
2. Acadêmico — é o dado empírico que o TCC não tem (lacuna nº 5 do inventário).
   Sem isto, qualquer afirmação sobre eficácia do roteamento é opinião.
3. Segurança — reconstruir o que um agente fez, depois do fato.

Três decisões merecem justificativa, porque são o que distingue esta trilha de um
arquivo de log qualquer:

**A unidade é a trajetória, não a chamada.** O `run_id` antigo era `uuid4()` por
linha: um identificador que não identificava nada, porque nunca se repetia. Quem
agrupa agora é `trajectory_id`, comum a todos os eventos de um mesmo roteamento —
inclusive às tentativas de escalada. É isso que torna respondível a pergunta
"quantas tentativas custou resolver esta tarefa", que é o número que o texto pede.

**O encadeamento por hash é o que sustenta a alegação metodológica.** Abrir em
modo `"a"` não impede reescrita, apenas não a facilita. Como aqui o pesquisador é
também o operador da ferramenta que produz o seu próprio dado, o requisito não é
impedir a edição — é torná-la *detectável*. Cada evento carrega o hash do
anterior; alterar um evento no meio quebra a cadeia de todos os posteriores.

**Falha de escrita nunca é silenciosa.** O `except OSError: pass` de antes fazia
a trilha desaparecer sem aviso, que é precisamente o modo de falha que o trabalho
inteiro combate: promessa que falha lida como promessa cumprida. `write()` devolve
`bool`, conta as falhas e avisa uma vez em `stderr` — sem derrubar a chamada que a
originou, porque perder a tarefa por causa do log seria trocar um problema por
outro pior.

**Vocabulário fechado é extensão da mesma doutrina.** `cause_category`,
`cause_subtype`, `fault_side`, `source` e `mark` eram texto livre. Um erro de
digitação não derrubava nada — apenas fazia a categoria sumir de todo agregado,
sem aviso, que é a falha silenciosa contra a qual o módulo inteiro existe. Esses
valores são escritos pelo próprio código do FPCH, nunca por entrada de usuário:
valor inválido é defeito de programação, e o lugar de falhar é na construção do
evento, antes de qualquer trabalho estar em risco. Daí `ValueError` em
`__post_init__` — e nunca em `write()`, que mantém o contrato de não derrubar o
chamador. Na leitura vale o oposto: linha antiga pode trazer vocabulário
anterior, então o desconhecido é agregado sob `"?"` e contado em
`counters()["unknown_vocab"]`, pelo mesmo princípio de `corrupt_lines`.

Append-only. Nunca reescrever histórico: um log que o agente pode editar não é
trilha de auditoria.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import stat
import sys
import threading
from contextlib import contextmanager
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Iterator

SCHEMA_VERSION = 4

#: Desfechos válidos de uma trajetória.
OUTCOMES = ("ok", "failed", "abandoned")

#: Taxonomia de causa raiz adotada de Zhao et al. (2026), Tabela II, p. 6: três
#: categorias mutuamente exclusivas e nove subtipos, derivadas da anotação manual
#: de trajetórias de agentes reais. Os nomes ficam em português porque são
#: vocabulário do FPCH; o termo original em inglês fica no comentário ao lado,
#: porque é ele que torna a adoção rastreável até a fonte. Os percentuais são os
#: da distribuição relatada no artigo — servem de referência de ordem de
#: grandeza, não de expectativa a ser reproduzida por esta trilha.
CAUSE_TAXONOMY: dict[str, tuple[str, ...]] = {
    # epistemic (57,9%): o agente agiu sobre uma leitura errada do mundo
    "epistemica": (
        "premissa_falsa",               # false premise (30,7%)
        "negligencia_de_especificacao",  # specification neglect (14,9%)
        "leitura_errada_de_saida",      # output misreading (4,4%)
        "sinal_ignorado",               # ignored signal (4,1%)
        "acao_prematura",               # premature action (3,7%)
    ),
    # competence (32,8%): o agente sabia o que fazer e não conseguiu fazer
    "competencia": (
        "lacuna_de_conhecimento",       # knowledge gap (24,0%)
        "limite_de_capacidade",         # capability limitation (8,8%)
    ),
    # environment (9,4%): a falha nasceu fora do agente
    "ambiente": (
        "bloqueio_de_ambiente",         # environment blocker (8,8%)
        "outro",                        # other (0,6%)
    ),
}

#: De que lado nasceu a falta. `"infraestrutura"` é o que os hooks já gravam.
FAULT_SIDES = ("modelo", "infraestrutura")

#: Qual parte do harness produziu o efeito registrado. Sem este campo, "o harness
#: ajudou" é anedota: não há como ir do agregado até a peça responsável.
SOURCES = ("nlah", "politica", "hook", "mcp", "memoria", "skill", "catalogo", "usuario")

#: Marcos do processo de falha (Zhao et al., 2026). `t_err` é o erro decisivo,
#: rotulado *retrospectivamente* sobre a trajetória inteira — não é
#: necessariamente o primeiro erro; `t_lock` é o ponto após o qual a trajetória é
#: empiricamente irrecuperável; `t_obs` é o primeiro sinal observável do erro.
MARKS = ("t_err", "t_lock", "t_obs")

#: Proveniência fechada do timeout efetivo de um hook. ``policy`` significa que
#: há um campo de política causalmente responsável; ``explicit`` é override do
#: chamador e, por definição, não declara campo governante.
TIMEOUT_ORIGINS = ("policy", "explicit")

#: Campos de vocabulário fechado cuja validação é uma pertinência simples. O
#: `cause_subtype` fica de fora porque sua validade depende da categoria.
_CLOSED_VOCAB: dict[str, tuple[str, ...]] = {
    "cause_category": tuple(CAUSE_TAXONOMY),
    "fault_side": FAULT_SIDES,
    "source": SOURCES,
    "mark": MARKS,
}


def _now_iso() -> str:
    """ISO-8601 com microssegundos e offset.

    O formato anterior (`%Y-%m-%dT%H:%M:%S%z`) tinha resolução de um segundo, e
    duas tentativas do mesmo segundo ficavam indistinguíveis na ordenação. O `ts`
    serve para leitura humana e correlação externa; a ordem canônica dentro da
    trajetória é o `seq`, nunca o relógio.
    """
    return datetime.now().astimezone().isoformat(timespec="microseconds")


def _default_log_path() -> Path:
    override = os.environ.get("FPCH_AUDIT_LOG")
    if override:
        return Path(override)
    return Path.home() / ".fpch" / "audit.jsonl"


@dataclass
class Event:
    """Um evento da trilha. Campos None = não aplicável a este tipo de evento.

    Campos `None` são omitidos na serialização — um `attempt` não precisa carregar
    as doze chaves nulas de `verify` e `end`. O hash é calculado sobre exatamente o
    dicionário gravado, então a omissão não é cosmética: mudá-la de um lado sem o
    outro invalida a cadeia inteira.
    """

    event: str                 # "start" | "attempt" | "verify" | "end" | "annotate" | "install"
    trajectory_id: str         # chave comum a toda a trajetória
    seq: int                   # ordem dentro da trajetória, começa em 0
    ts: str = field(default_factory=_now_iso)
    schema_version: int = SCHEMA_VERSION

    # contexto (todos os eventos)
    task_class: str | None = None
    label: str | None = None

    # attempt
    backend: str | None = None
    model: str | None = None
    pool: str | None = None
    prompt_chars: int | None = None     # PROXY de tokens de entrada
    output_chars: int | None = None     # PROXY de tokens de saída
    exit_code: int | None = None
    latency_s: float | None = None
    ok: bool | None = None
    error: str | None = None
    escalated_from: str | None = None

    # verify
    component: str | None = None        # qual hook emitiu o veredito
    verdict: str | None = None          # "pass" | "fail" | "absent"
    evidence: str | None = None         # trecho da saída do hook, truncado

    # diagnóstico (vocabulário fechado — ver CAUSE_TAXONOMY e FAULT_SIDES)
    cause_category: str | None = None   # "epistemica" | "competencia" | "ambiente"
    cause_subtype: str | None = None    # só válido dentro da sua categoria
    fault_side: str | None = None       # "modelo" | "infraestrutura"

    # atribuição de fonte: `source` diz que peça do harness produziu o efeito,
    # `source_ref` diz qual instância dela. É o par que permite sair de "veio de
    # hook" e chegar a "veio do hook pytest" — sozinho, nenhum dos dois responde.
    source: str | None = None           # vocabulário fechado (SOURCES)
    source_ref: str | None = None       # identificador concreto, texto livre

    # governança causal de hooks (schema 4)
    timed_out: bool | None = None        # True somente após subprocess.TimeoutExpired
    timeout_s: float | None = None       # timeout efetivamente entregue ao processo
    timeout_origin: str | None = None    # vocabulário fechado (TIMEOUT_ORIGINS)
    governing_field: str | None = None   # hoje, somente "verificacao.timeout_s"

    # annotate: marco retrospectivo sobre um evento já gravado
    mark: str | None = None             # "t_err" | "t_lock" | "t_obs"
    target_seq: int | None = None       # `seq` do evento marcado, mesma trajetória

    # install: como desfazer o que foi instalado
    inverse: str | None = None          # comando ou JSON; formato é de outra fatia

    # end
    outcome: str | None = None          # "ok" | "failed" | "abandoned"
    attempts: int | None = None
    duration_s: float | None = None

    # integridade
    prev_hash: str | None = None
    hash: str = ""

    def __post_init__(self) -> None:
        """Rejeita vocabulário inválido na construção, antes de qualquer escrita.

        Aqui — e não em `write()` — porque quem preenche estes campos é o código
        do FPCH, não o usuário: um valor fora do vocabulário é defeito de
        programação, e defeito de programação deve estourar onde foi cometido. Se
        escapasse para o disco, a linha continuaria legível e o agregado perderia
        a categoria em silêncio, que é o modo de falha que a trilha existe para
        eliminar. `write()` segue sem `raise`: perder a chamada por causa do log
        continua sendo pior do que perder o log.
        """
        for campo, aceitos in _CLOSED_VOCAB.items():
            valor = getattr(self, campo)
            if valor is not None and valor not in aceitos:
                raise ValueError(
                    f"{campo} inválido: {valor!r}. Aceitos: {', '.join(aceitos)}."
                )

        if self.cause_subtype is not None:
            if self.cause_category is None:
                raise ValueError(
                    f"cause_subtype={self.cause_subtype!r} sem cause_category. "
                    "O subtipo só existe dentro de uma categoria; sozinho ele não "
                    f"diz nada. Categorias: {', '.join(CAUSE_TAXONOMY)}."
                )
            aceitos = CAUSE_TAXONOMY[self.cause_category]
            if self.cause_subtype not in aceitos:
                raise ValueError(
                    f"cause_subtype inválido para cause_category="
                    f"{self.cause_category!r}: {self.cause_subtype!r}. "
                    f"Aceitos nesta categoria: {', '.join(aceitos)}."
                )

        timeout_meta = (
            self.timed_out, self.timeout_s, self.timeout_origin, self.governing_field,
        )
        if any(valor is not None for valor in timeout_meta):
            if self.event != "verify" or self.source != "hook":
                raise ValueError(
                    "metadados de timeout só pertencem a evento verify com source='hook'."
                )
            if type(self.timed_out) is not bool:
                raise ValueError("timed_out deve ser booleano quando metadados de timeout existem.")
            if (
                isinstance(self.timeout_s, bool)
                or not isinstance(self.timeout_s, (int, float))
                or not math.isfinite(float(self.timeout_s))
                or self.timeout_s <= 0
            ):
                raise ValueError("timeout_s deve ser número positivo.")
            if self.timeout_origin not in TIMEOUT_ORIGINS:
                raise ValueError(
                    f"timeout_origin inválido: {self.timeout_origin!r}. "
                    f"Aceitos: {', '.join(TIMEOUT_ORIGINS)}."
                )
            if self.timeout_origin == "policy":
                if self.governing_field != "verificacao.timeout_s":
                    raise ValueError(
                        "timeout de origem policy exige governing_field='verificacao.timeout_s'."
                    )
            elif self.governing_field is not None:
                raise ValueError("timeout explicit não pode declarar campo de política governante.")
            if self.timed_out and self.verdict != "fail":
                raise ValueError("timed_out=True exige verdict='fail'.")

        if self.event == "install" and not self.inverse:
            raise ValueError(
                "evento install sem inverse. Instalar sem registrar como "
                "desinstalar é exatamente o que o requisito de reversibilidade "
                "proíbe: o rollback exato precisa da inversa gravada no momento "
                "da ação, não reconstruída depois."
            )


# ---------------------------------------------------------------------------
# Estado de módulo: contadores e ponta da cadeia
# ---------------------------------------------------------------------------

#: Falhas de escrita acumuladas neste processo. Contar é o que impede que a
#: perda de trilha passe despercebida — o aviso em stderr sai uma vez só, mas o
#: contador continua subindo e pode ser inspecionado por quem consome o módulo.
_write_failures = 0
_warned_write = False

#: Linhas ilegíveis encontradas na leitura. Descartar em silêncio esconderia
#: corrupção justamente do relatório que deveria denunciá-la.
_corrupt_lines = 0
_warned_corrupt = False

#: Valores de vocabulário fechado lidos do disco que não pertencem ao vocabulário
#: vigente. Não é erro: a trilha é append-only e o esquema 3 sucede dois outros,
#: então linha antiga com rótulo antigo é o esperado. É contado pelo mesmo motivo
#: que `corrupt_lines` — o que não se entende é contado, não descartado em
#: silêncio. Conta *ocorrências de campo*: uma linha com categoria e fonte
#: desconhecidas soma dois.
_unknown_vocab = 0
_warned_vocab = False

#: Ponta da cadeia por arquivo, para não reler o arquivo inteiro a cada escrita.
_tip_cache: dict[str, str | None] = {}

# ``flock``/``msvcrt.locking`` serializam processos. Locks de thread também são
# necessários porque a semântica de locks de arquivo dentro do mesmo processo
# varia entre plataformas. O sidecar permanece em disco de propósito: apagá-lo
# ao liberar permitiria que um processo ainda esperando no inode antigo e outro
# processo no inode recém-criado entrassem juntos na seção crítica.
_thread_locks: dict[str, threading.Lock] = {}
_thread_locks_guard = threading.Lock()

#: Próximo `seq` por trajetória. Contador de processo, não do arquivo.
_seq_counters: dict[str, int] = {}


def next_seq(trajectory_id: str) -> int:
    """Próximo `seq` da trajetória, começando em 0.

    Existe porque `seq` é ordem *dentro da trajetória*, não dentro da chamada: um
    chamador que amarra dois estágios ao mesmo `trajectory_id` precisa que o
    segundo estágio continue de onde o primeiro parou. Se cada estágio recomeçasse
    do zero, a ordenação por `(trajectory_id, seq)` embaralharia os dois — e a
    trilha diria que a validação aconteceu antes da extração.

    O contador é de processo. Uma trajetória que atravessasse processos exigiria
    lê-lo do arquivo; nesta fatia isso não acontece, e ler a trilha inteira a cada
    evento custaria caro para resolver um caso que não existe.
    """
    atual = _seq_counters.get(trajectory_id, 0)
    _seq_counters[trajectory_id] = atual + 1
    return atual


def counters() -> dict:
    """Contadores observáveis do processo. Zero significa trilha íntegra."""
    return {
        "write_failures": _write_failures,
        "corrupt_lines": _corrupt_lines,
        "unknown_vocab": _unknown_vocab,
    }


def reset_state() -> None:
    """Zera contadores, avisos e cache de cadeia. Existe para os testes."""
    global _write_failures, _warned_write, _corrupt_lines, _warned_corrupt
    global _unknown_vocab, _warned_vocab
    _write_failures = 0
    _warned_write = False
    _corrupt_lines = 0
    _warned_corrupt = False
    _unknown_vocab = 0
    _warned_vocab = False
    _tip_cache.clear()
    _seq_counters.clear()
    with _thread_locks_guard:
        _thread_locks.clear()


# ---------------------------------------------------------------------------
# Hash e encadeamento
# ---------------------------------------------------------------------------

def _payload(ev: Event) -> dict:
    """Dicionário que vai para o disco: campos None omitidos, `hash` ainda vazio."""
    return {k: v for k, v in asdict(ev).items() if v is not None}


def compute_hash(payload: dict) -> str:
    """SHA-256 do JSON canônico do evento, sem o campo `hash`.

    Canônico = chaves ordenadas, sem espaços supérfluos, `ensure_ascii=False`.
    Como a verificação recalcula a partir do dicionário lido de volta do JSON, o
    que precisa ser estável é o *dicionário*, não o texto — e chaves ordenadas
    garantem isso independentemente da ordem em que os campos foram preenchidos.
    """
    material = {k: v for k, v in payload.items() if k != "hash"}
    canonical = json.dumps(material, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _tail_hash(target: Path) -> str | None:
    """Último hash gravado, lendo o fim do arquivo em vez do arquivo inteiro.

    Varre de trás para frente em blocos crescentes até achar uma linha legível
    com `hash`. Linhas de esquema 1 não têm o campo e são puladas: a cadeia
    começa no primeiro evento de esquema 2, sem exigir reescrita do histórico.
    """
    chunk = 8192
    try:
        with target.open("rb") as fh:
            fh.seek(0, os.SEEK_END)
            size = fh.tell()
            if not size:
                return None
            while True:
                start = max(0, size - chunk)
                fh.seek(start)
                lines = fh.read(size - start).split(b"\n")
                if start > 0:
                    # a primeira pode estar cortada ao meio pelo bloco
                    lines = lines[1:]
                for raw in reversed(lines):
                    raw = raw.strip()
                    if not raw:
                        continue
                    try:
                        row = json.loads(raw.decode("utf-8"))
                    except (ValueError, UnicodeDecodeError):
                        continue
                    if isinstance(row, dict) and row.get("hash"):
                        return str(row["hash"])
                if start == 0:
                    return None
                chunk *= 4
    except OSError:
        return None


def _chain_tip(target: Path) -> str | None:
    """Ponta da cadeia, com cache. Arquivo ausente ou vazio reinicia a cadeia."""
    key = str(target)
    try:
        empty = not target.exists() or target.stat().st_size == 0
    except OSError:
        empty = True
    if empty:
        _tip_cache.pop(key, None)
        return None
    if key not in _tip_cache:
        _tip_cache[key] = _tail_hash(target)
    return _tip_cache[key]


# ---------------------------------------------------------------------------
# Escrita
# ---------------------------------------------------------------------------


def _safe_display(value: object, *, limit: int = 500) -> str:
    """Texto de erro sem controles literais nem volume ilimitado."""
    rendered = str(value)
    safe = "".join(
        character
        if character.isprintable() and character not in "\r\n"
        else f"\\u{ord(character):04x}"
        for character in rendered
    )
    return safe if len(safe) <= limit else safe[:limit] + "…"


def _same_file(first: os.stat_result, second: os.stat_result) -> bool:
    try:
        return os.path.samestat(first, second)
    except (AttributeError, OSError):
        return (first.st_dev, first.st_ino) == (second.st_dev, second.st_ino)


def _is_linklike(path: Path) -> bool:
    try:
        metadata = path.lstat()
    except FileNotFoundError:
        return False
    if stat.S_ISLNK(metadata.st_mode):
        return True
    attributes = getattr(metadata, "st_file_attributes", 0)
    reparse = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
    if attributes & reparse:
        return True
    isjunction = getattr(os.path, "isjunction", None)
    return bool(isjunction is not None and isjunction(path))


def _reject_linklike_path(path: Path) -> None:
    current = path
    while True:
        if _is_linklike(current):
            role = "trilha" if current == path else "ancestral da trilha"
            raise OSError(
                f"{role} não pode ser link, junction ou reparse point: "
                f"{_safe_display(current)}"
            )
        parent = current.parent
        if parent == current:
            return
        current = parent


def _open_verified(path: Path, flags: int, mode: int = 0o600) -> tuple[int, os.stat_result]:
    flags |= getattr(os, "O_BINARY", 0)
    flags |= getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(path, flags, mode)
    try:
        opened = os.fstat(descriptor)
        current = path.lstat()
        if _is_linklike(path) or not _same_file(opened, current):
            raise OSError(f"alvo inseguro ou trocado durante abertura: {_safe_display(path)}")
    except Exception:
        os.close(descriptor)
        raise
    return descriptor, opened


def _thread_lock(path: Path) -> threading.Lock:
    key = os.path.abspath(os.fspath(path))
    with _thread_locks_guard:
        return _thread_locks.setdefault(key, threading.Lock())


def _lock_os(descriptor: int) -> None:
    os.lseek(descriptor, 0, os.SEEK_SET)
    if os.name == "nt":
        import msvcrt

        msvcrt.locking(descriptor, msvcrt.LK_LOCK, 1)
    else:
        import fcntl

        fcntl.flock(descriptor, fcntl.LOCK_EX)


def _unlock_os(descriptor: int) -> None:
    try:
        os.lseek(descriptor, 0, os.SEEK_SET)
        if os.name == "nt":
            import msvcrt

            msvcrt.locking(descriptor, msvcrt.LK_UNLCK, 1)
        else:
            import fcntl

            fcntl.flock(descriptor, fcntl.LOCK_UN)
    except OSError:
        # Fechar o descritor também libera o lock. A falha de unlock não pode
        # impedir esse cleanup mais forte no finally externo.
        pass


@contextmanager
def _trail_lock(target: Path) -> Iterator[None]:
    """Serializa escritores por processo e entre processos."""
    lock_path = target.with_name(target.name + ".lock")
    local_lock = _thread_lock(lock_path)
    with local_lock:
        descriptor: int | None = None
        locked = False
        try:
            _reject_linklike_path(target)
            _reject_linklike_path(lock_path)
            target.parent.mkdir(parents=True, exist_ok=True)
            _reject_linklike_path(target)
            _reject_linklike_path(lock_path)
            descriptor, metadata = _open_verified(
                lock_path,
                os.O_RDWR | os.O_CREAT,
            )
            if metadata.st_size == 0:
                os.write(descriptor, b"0")
            _lock_os(descriptor)
            locked = True

            # A verificação após adquirir o lock reduz a janela entre validar
            # os caminhos e entrar na seção crítica.
            _reject_linklike_path(target)
            current_lock = lock_path.lstat()
            if not _same_file(os.fstat(descriptor), current_lock):
                raise OSError("arquivo de lock foi trocado durante a aquisição")
            yield
        finally:
            if descriptor is not None:
                if locked:
                    _unlock_os(descriptor)
                try:
                    os.close(descriptor)
                except OSError:
                    pass


def _tail_hash_from_descriptor(descriptor: int) -> str | None:
    """Último hash a partir do mesmo inode aberto para append."""
    duplicate = os.dup(descriptor)
    with os.fdopen(duplicate, "rb") as stream:
        stream.seek(0, os.SEEK_END)
        size = stream.tell()
        if not size:
            return None
        chunk = 8192
        while True:
            start = max(0, size - chunk)
            stream.seek(start)
            lines = stream.read(size - start).split(b"\n")
            if start > 0:
                lines = lines[1:]
            for raw in reversed(lines):
                raw = raw.strip()
                if not raw:
                    continue
                try:
                    row = json.loads(raw.decode("utf-8"))
                except (ValueError, UnicodeDecodeError):
                    continue
                if isinstance(row, dict) and row.get("hash"):
                    return str(row["hash"])
            if start == 0:
                return None
            chunk *= 4


def _write_all(descriptor: int, data: bytes) -> None:
    view = memoryview(data)
    while view:
        written = os.write(descriptor, view)
        if written <= 0:
            raise OSError("append da trilha não avançou")
        view = view[written:]


def _fsync_directory(directory: Path) -> None:
    """Persiste a entrada do arquivo em sistemas que aceitam fsync de diretório."""
    if os.name == "nt":
        return
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
    descriptor = os.open(directory, flags)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _record_write_failure(target: Path, exc: BaseException) -> bool:
    global _write_failures, _warned_write
    _write_failures += 1
    if not _warned_write:
        _warned_write = True
        print(
            "fpch: AVISO — falha ao gravar a trilha de auditoria em "
            f"{_safe_display(target)}: {_safe_display(exc)}. "
            "A execução continua, mas esta sessão não é auditável.",
            file=sys.stderr,
        )
    return False


def write(ev: Event, path: Path | None = None) -> bool:
    """Grava um evento encadeado. Devolve se conseguiu.

    Falha de trilha não derruba a chamada que a originou — mas também não some:
    conta, avisa uma vez em stderr, e devolve False para quem quiser reagir. O
    hash anterior é calculado sob lock interprocesso; ``True`` só é devolvido
    depois de flush equivalente no descritor, ``fsync`` do arquivo e, quando o
    sistema permite, ``fsync`` do diretório.
    """
    target = Path(os.path.abspath(os.fspath(path or _default_log_path())))
    descriptor: int | None = None
    original_size: int | None = None
    try:
        base_payload = _payload(ev)
        base_payload.pop("prev_hash", None)
        base_payload.pop("hash", None)
        with _trail_lock(target):
            try:
                descriptor, opened = _open_verified(
                    target,
                    os.O_RDWR | os.O_APPEND | os.O_CREAT,
                )
                original_size = os.lseek(descriptor, 0, os.SEEK_END)
                previous = _tail_hash_from_descriptor(descriptor)
                payload = dict(base_payload)
                if previous is not None:
                    payload["prev_hash"] = previous
                current_hash = compute_hash(payload)
                payload["hash"] = current_hash
                line = json.dumps(
                    payload,
                    sort_keys=True,
                    ensure_ascii=False,
                    separators=(",", ":"),
                ).encode("utf-8") + b"\n"

                _write_all(descriptor, line)
                os.fsync(descriptor)
                _fsync_directory(target.parent)
                if not _same_file(opened, target.lstat()):
                    raise OSError("trilha foi trocada durante o append")

                ev.prev_hash = previous
                ev.hash = current_hash
                _tip_cache[str(target)] = current_hash
                return True
            except (OSError, ValueError, UnicodeError, TypeError):
                # Rollback ainda sob o mesmo lock: truncar depois de liberar
                # poderia apagar o append válido do próximo escritor.
                if descriptor is not None and original_size is not None:
                    try:
                        os.ftruncate(descriptor, original_size)
                        os.fsync(descriptor)
                    except OSError:
                        pass
                raise
            finally:
                if descriptor is not None:
                    try:
                        os.close(descriptor)
                    except OSError:
                        pass
                    descriptor = None
    except (OSError, ValueError, UnicodeError, TypeError) as exc:
        return _record_write_failure(target, exc)


# ---------------------------------------------------------------------------
# Leitura
# ---------------------------------------------------------------------------

def _synthetic_trajectory(row: dict, index: int) -> str:
    """Trajetória sintética para linha de esquema 1, derivada do label quando houver.

    A trilha antiga não tem como ser reagrupada de verdade — cada linha era uma
    chamada solta. O identificador sintético é honesto quanto a isso: prefixo
    `v1-` para que ninguém confunda agrupamento reconstruído com agrupamento
    observado.
    """
    label = row.get("label")
    if label:
        return "v1-" + "".join(c if c.isalnum() else "-" for c in str(label))[:40]
    run_id = row.get("run_id")
    if run_id:
        return f"v1-{run_id}"
    return f"v1-linha{index}"


def _normalize(row: dict, index: int) -> dict:
    """Converte linha de esquema 1 em evento `attempt` órfão, em memória.

    Nada é reescrito no disco: a conversão existe só para que `summary()` e
    `trajectories()` consigam ler a trilha histórica do autor sem quebrar.
    """
    if row.get("schema_version"):
        return row
    up = dict(row)
    up["schema_version"] = 1
    up["event"] = "attempt"
    up["seq"] = 0
    up["trajectory_id"] = _synthetic_trajectory(row, index)
    return up


def _unknown_fields(row: dict) -> int:
    """Quantos campos de vocabulário fechado desta linha estão fora do vocabulário.

    Só se aplica à leitura. Na escrita o mesmo caso é `ValueError`; aqui não pode
    ser, porque a linha já está no disco e condená-la não a conserta — o que
    resta é contá-la e agregá-la sob `"?"`, para que ninguém leia um agregado
    incompleto como se fosse completo.
    """
    fora = 0
    for campo, aceitos in _CLOSED_VOCAB.items():
        valor = row.get(campo)
        if valor is not None and valor not in aceitos:
            fora += 1
    subtipo = row.get("cause_subtype")
    if subtipo is not None:
        categoria = row.get("cause_category")
        if categoria not in CAUSE_TAXONOMY or subtipo not in CAUSE_TAXONOMY[categoria]:
            fora += 1
    return fora


def _cause_keys(row: dict) -> tuple[str, str | None] | None:
    """Chaves de agregação de causa desta linha, ou `None` se ela não diagnostica.

    Fora do vocabulário vigente vira `"?"` em vez de sumir: o balde do
    desconhecido é o que impede que uma soma parcial se apresente como total.
    """
    categoria = row.get("cause_category")
    subtipo = row.get("cause_subtype")
    if categoria is None and subtipo is None:
        return None
    if categoria in CAUSE_TAXONOMY:
        chave = str(categoria)
        if subtipo is None:
            return chave, None
        return chave, (str(subtipo) if subtipo in CAUSE_TAXONOMY[chave] else "?")
    return "?", ("?" if subtipo is not None else None)


def _read_raw(target: Path) -> tuple[list[dict], int]:
    """Linhas na ordem do arquivo, normalizadas, mais a contagem de corrompidas."""
    global _corrupt_lines, _warned_corrupt, _unknown_vocab, _warned_vocab
    if not target.exists():
        return [], 0
    rows: list[dict] = []
    bad = 0
    estranhos = 0
    with target.open(encoding="utf-8") as fh:
        for index, line in enumerate(fh):
            line = line.strip()
            if not line:
                continue
            try:
                parsed = json.loads(line)
            except json.JSONDecodeError:
                bad += 1
                continue
            if not isinstance(parsed, dict):
                bad += 1
                continue
            row = _normalize(parsed, index)
            estranhos += _unknown_fields(row)
            rows.append(row)
    if estranhos:
        _unknown_vocab += estranhos
        if not _warned_vocab:
            _warned_vocab = True
            print(
                f"fpch: AVISO — {estranhos} valor(es) fora do vocabulário vigente na "
                f"trilha {target}. Foram agregados sob '?', não descartados: um "
                "agregado que perde categorias em silêncio mente por omissão.",
                file=sys.stderr,
            )
    if bad:
        _corrupt_lines += bad
        if not _warned_corrupt:
            _warned_corrupt = True
            print(
                f"fpch: AVISO — {bad} linha(s) ilegível(is) na trilha {target}. "
                "Foram contadas, não descartadas em silêncio: a trilha pode estar corrompida.",
                file=sys.stderr,
            )
    return rows, bad


def read_all(path: Path | None = None) -> list[dict]:
    """Todos os eventos, ordenados por `(trajectory_id, seq)`.

    A ordenação é estável, então eventos empatados mantêm a ordem do arquivo —
    o que preserva o encadeamento aparente das linhas de esquema 1, que têm todas
    `seq = 0`. Para verificar a cadeia use `verify_chain`, que lê na ordem física.
    """
    rows, _ = _read_raw(path or _default_log_path())
    return sorted(rows, key=lambda r: (str(r.get("trajectory_id") or ""), r.get("seq") or 0))


def trajectories(path: Path | None = None) -> dict[str, list[dict]]:
    """Eventos agrupados por trajetória, cada grupo ordenado por `seq`."""
    grouped: dict[str, list[dict]] = {}
    for row in read_all(path):
        grouped.setdefault(str(row.get("trajectory_id") or "?"), []).append(row)
    return grouped


def verify_chain(path: Path | None = None) -> tuple[bool, int | None]:
    """Confere o encadeamento. Devolve `(íntegra, índice do primeiro defeito)`.

    O índice conta linhas não-vazias do arquivo, a partir de zero, na ordem
    física — é a posição que o autor encontra abrindo o arquivo, e não a posição
    lógica dentro da trajetória.

    Linhas de esquema 1 são puladas: são anteriores ao encadeamento e a
    compatibilidade exige não condená-las por não terem um campo que não existia.
    Linha ilegível, por outro lado, quebra: não é possível afirmar integridade
    sobre um trecho que não se consegue ler.
    """
    target = path or _default_log_path()
    if not target.exists():
        return True, None

    expected: str | None = None
    index = -1
    with target.open(encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            index += 1
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                return False, index
            if not isinstance(row, dict):
                return False, index
            if not row.get("schema_version"):
                continue  # esquema 1: fora da cadeia, por construção
            recorded = row.get("hash")
            if not recorded or compute_hash(row) != recorded:
                return False, index
            if row.get("prev_hash") != expected:
                return False, index
            expected = recorded
    return True, None


# ---------------------------------------------------------------------------
# Processo de falha
# ---------------------------------------------------------------------------

def failure_process(path: Path | None = None) -> dict:
    """Marcos do processo de falha por trajetória, com as duas derivadas.

    Devolve, para cada trajetória que tenha ao menos um evento `annotate`::

        {"t_err": seq|None, "t_lock": seq|None, "t_obs": seq|None,
         "fix_window": int|None, "observability_lag": int|None,
         "remarcado": bool}

    `fix_window = t_lock − t_err` é o quanto de trajetória ainda havia entre o
    erro decisivo e o ponto de não-retorno; `observability_lag = t_obs − t_lock`
    é o quanto o sistema levou para *mostrar* o erro depois que já era tarde.
    Marco não anotado devolve `None` em vez de zero — ausência de anotação não é
    janela nula, e confundir as duas coisas inventaria dado.

    O marco é retrospectivo: só se sabe qual erro foi decisivo olhando a
    trajetória inteira, depois do desfecho. Como a trilha é append-only, marcar
    não reescreve o evento marcado — grava um evento `annotate` novo, que entra na
    cadeia de hash como qualquer outro. Anotar é, ele próprio, auditável: ninguém
    marca retroativamente sem deixar rastro. Marco anotado duas vezes na mesma
    trajetória vale pelo último, e a trajetória vem com `remarcado: True` — mudar
    de opinião sobre um rótulo retrospectivo é legítimo, escondê-lo não é.

    **Limite de granularidade.** As derivadas são medidas **em eventos da
    trilha**, não em turnos internos do agente. Zhao et al. (2026) medem passos de
    raciocínio-e-ação dentro de uma execução; o FPCH registra uma linha por
    chamada de backend. A adoção aqui é da **estrutura conceitual** — três marcos
    e duas derivadas —, não da unidade de medida. Comparar os números produzidos
    por esta função com os números do artigo seria erro de leitura.
    """
    resultado: dict[str, dict] = {}
    for tid, eventos in trajectories(path).items():
        marcos: dict[str, int] = {}
        remarcado = False
        anotada = False
        for ev in eventos:
            if ev.get("event") != "annotate":
                continue
            anotada = True
            marca = ev.get("mark")
            alvo = ev.get("target_seq")
            if marca not in MARKS or alvo is None:
                continue  # anotação ilegível como marco: contada em unknown_vocab
            if marca in marcos:
                remarcado = True
            marcos[str(marca)] = int(alvo)
        if not anotada:
            continue

        t_err = marcos.get("t_err")
        t_lock = marcos.get("t_lock")
        t_obs = marcos.get("t_obs")
        resultado[tid] = {
            "t_err": t_err,
            "t_lock": t_lock,
            "t_obs": t_obs,
            "fix_window": None if t_err is None or t_lock is None else t_lock - t_err,
            "observability_lag": None if t_lock is None or t_obs is None else t_obs - t_lock,
            "remarcado": remarcado,
        }
    return resultado


# ---------------------------------------------------------------------------
# Agregação
# ---------------------------------------------------------------------------

def summary(path: Path | None = None) -> dict:
    """Agrega por reserva de cota e por trajetória.

    Por pool responde "onde a cota está indo e o que está falhando"; por
    trajetória responde "quanto custou resolver uma tarefa", que é a pergunta que
    a agregação por chamada não conseguia responder — e é dela que sai o número
    citável no texto.
    """
    rows = read_all(path)

    by_pool: dict[str, dict] = {}
    detail: dict[str, dict] = {}
    # Causa e fonte são contadas sobre *todos* os eventos que carregam o campo,
    # não só sobre `attempt`: quem diagnostica no FPCH é o hook (evento `verify`),
    # e restringir a agregação a `attempt` zeraria justamente a coluna que
    # interessa.
    by_cause: dict[str, dict] = {}
    by_source: dict[str, dict] = {}

    for r in rows:
        tid = str(r.get("trajectory_id") or "?")
        traj = detail.setdefault(
            tid,
            {
                "attempts": 0,
                "outcome": None,
                "latency_s": 0.0,
                "duration_s": None,
                "task_class": r.get("task_class"),
                "label": r.get("label"),
            },
        )
        if traj["task_class"] is None:
            traj["task_class"] = r.get("task_class")
        if traj["label"] is None:
            traj["label"] = r.get("label")

        chaves = _cause_keys(r)
        if chaves is not None:
            categoria, subtipo = chaves
            balde = by_cause.setdefault(categoria, {"total": 0, "subtypes": {}})
            balde["total"] += 1
            if subtipo is not None:
                balde["subtypes"][subtipo] = balde["subtypes"].get(subtipo, 0) + 1

        origem = r.get("source")
        if origem is not None:
            chave = str(origem) if origem in SOURCES else "?"
            balde = by_source.setdefault(chave, {"total": 0, "refs": {}})
            balde["total"] += 1
            ref = r.get("source_ref")
            if ref is not None:
                balde["refs"][str(ref)] = balde["refs"].get(str(ref), 0) + 1

        kind = r.get("event")
        if kind == "attempt":
            traj["attempts"] += 1
            traj["latency_s"] += r.get("latency_s") or 0.0
            p = r.get("pool") or "?"
            agg = by_pool.setdefault(
                p, {"calls": 0, "fail": 0, "latency_total": 0.0, "chars_out": 0}
            )
            agg["calls"] += 1
            if r.get("ok") is False:
                agg["fail"] += 1
            agg["latency_total"] += r.get("latency_s") or 0.0
            agg["chars_out"] += r.get("output_chars") or 0
        elif kind == "end":
            traj["outcome"] = r.get("outcome")
            traj["duration_s"] = r.get("duration_s")
            if r.get("attempts") is not None:
                traj["attempts"] = r["attempts"]

    for agg in by_pool.values():
        agg["latency_avg"] = round(agg["latency_total"] / agg["calls"], 2) if agg["calls"] else 0.0
        del agg["latency_total"]

    for traj in detail.values():
        traj["latency_s"] = round(traj["latency_s"], 3)

    by_outcome = {o: 0 for o in OUTCOMES}
    # Trajetória sem `end` é incompleta, não é fracasso: contá-la como falha
    # inventaria um resultado que a trilha não registra.
    by_outcome["sem_fim"] = 0
    for traj in detail.values():
        key = traj["outcome"] if traj["outcome"] in OUTCOMES else "sem_fim"
        by_outcome[key] += 1

    total = len(detail)
    concluded = total - by_outcome["sem_fim"]
    attempts_total = sum(t["attempts"] for t in detail.values())
    latency_total = sum(t["latency_s"] for t in detail.values())

    return {
        # `total_calls` continua sendo o nº de invocações de backend, não de
        # eventos — a CLI e o texto já usam esse nome com esse sentido.
        "total_calls": sum(1 for r in rows if r.get("event") == "attempt"),
        "total_events": len(rows),
        "by_pool": by_pool,
        "by_cause": by_cause,
        "by_source": by_source,
        "trajectories": {
            "total": total,
            "by_outcome": by_outcome,
            "success_rate": round(by_outcome["ok"] / concluded, 3) if concluded else 0.0,
            "attempts_total": attempts_total,
            "attempts_avg": round(attempts_total / total, 2) if total else 0.0,
            "latency_avg_s": round(latency_total / total, 2) if total else 0.0,
            "detail": detail,
        },
        "corrupt_lines": _corrupt_lines,
    }
