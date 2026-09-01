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

Append-only. Nunca reescrever histórico: um log que o agente pode editar não é
trilha de auditoria.
"""

from __future__ import annotations

import hashlib
import json
import os
import sys
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path

SCHEMA_VERSION = 2

#: Desfechos válidos de uma trajetória.
OUTCOMES = ("ok", "failed", "abandoned")


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

    event: str                 # "start" | "attempt" | "verify" | "end"
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

    # diagnóstico
    cause_category: str | None = None   # "epistemica" | "competencia" | "ambiente"
    cause_subtype: str | None = None
    fault_side: str | None = None       # "modelo" | "infraestrutura"

    # end
    outcome: str | None = None          # "ok" | "failed" | "abandoned"
    attempts: int | None = None
    duration_s: float | None = None

    # integridade
    prev_hash: str | None = None
    hash: str = ""


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

#: Ponta da cadeia por arquivo, para não reler o arquivo inteiro a cada escrita.
_tip_cache: dict[str, str | None] = {}

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
    return {"write_failures": _write_failures, "corrupt_lines": _corrupt_lines}


def reset_state() -> None:
    """Zera contadores, avisos e cache de cadeia. Existe para os testes."""
    global _write_failures, _warned_write, _corrupt_lines, _warned_corrupt
    _write_failures = 0
    _warned_write = False
    _corrupt_lines = 0
    _warned_corrupt = False
    _tip_cache.clear()
    _seq_counters.clear()


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

def write(ev: Event, path: Path | None = None) -> bool:
    """Grava um evento encadeado. Devolve se conseguiu.

    Falha de trilha não derruba a chamada que a originou — mas também não some:
    conta, avisa uma vez em stderr, e devolve False para quem quiser reagir.
    """
    global _write_failures, _warned_write
    target = path or _default_log_path()

    ev.prev_hash = _chain_tip(target)
    payload = _payload(ev)
    payload["hash"] = ev.hash = compute_hash(payload)
    line = json.dumps(payload, sort_keys=True, ensure_ascii=False, separators=(",", ":"))

    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        with target.open("a", encoding="utf-8") as fh:
            fh.write(line + "\n")
    except OSError as exc:
        _write_failures += 1
        if not _warned_write:
            _warned_write = True
            print(
                f"fpch: AVISO — falha ao gravar a trilha de auditoria em {target}: {exc}. "
                "A execução continua, mas esta sessão não é auditável.",
                file=sys.stderr,
            )
        return False

    _tip_cache[str(target)] = ev.hash
    return True


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


def _read_raw(target: Path) -> tuple[list[dict], int]:
    """Linhas na ordem do arquivo, normalizadas, mais a contagem de corrompidas."""
    global _corrupt_lines, _warned_corrupt
    if not target.exists():
        return [], 0
    rows: list[dict] = []
    bad = 0
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
            rows.append(_normalize(parsed, index))
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
