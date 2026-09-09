"""Entrevista determinística de preferências do FPCH.

As respostas são somente identificadores explícitos escolhidos pelo autor. O
módulo não sugere, instala ou executa ferramentas, não consulta rede e não lê
variáveis de ambiente. Persistência só acontece por chamada explícita a
``save_new``.
"""

from __future__ import annotations

import json
import os
import re
import stat
from dataclasses import dataclass
from pathlib import Path
from typing import Any, BinaryIO, Callable, ClassVar, TextIO

from .discovery import FpchDiscoveryReport

FPCH_INTERVIEW_SCHEMA_VERSION = 1
FPCH_INTERVIEW_MAX_BYTES = 64 * 1024
FPCH_INTERVIEW_MAX_ITEMS = 64

_FIELDS = (
    "skills_on_demand",
    "mandatory_linters",
    "mandatory_formatters",
)
_JSON_FIELDS = frozenset(("schema_version", *_FIELDS))
_IDENTIFIER = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:/+\-]{0,127}", re.ASCII)


class FpchInterviewError(ValueError):
    """As preferências não puderam ser coletadas, validadas ou persistidas."""


def _validate_items(field: str, value: object) -> tuple[str, ...]:
    if not isinstance(value, tuple):
        raise FpchInterviewError(f"{field} deve ser uma tupla de identificadores")
    if len(value) > FPCH_INTERVIEW_MAX_ITEMS:
        raise FpchInterviewError(
            f"{field} excede o limite de {FPCH_INTERVIEW_MAX_ITEMS} itens"
        )

    seen: set[str] = set()
    for item in value:
        if type(item) is not str or _IDENTIFIER.fullmatch(item) is None:
            raise FpchInterviewError(
                f"identificador inválido em {field}; use somente ASCII, sem espaços "
                "ou controles, no formato [A-Za-z0-9][A-Za-z0-9._:/+\\-]{{0,127}}"
            )
        if item in seen:
            raise FpchInterviewError(f"identificador duplicado em {field}")
        seen.add(item)
    return value


@dataclass(frozen=True)
class FpchInterviewPreferences:
    """Preferências explícitas no esquema fechado versão 1."""

    schema_version: ClassVar[int] = FPCH_INTERVIEW_SCHEMA_VERSION
    skills_on_demand: tuple[str, ...] = ()
    mandatory_linters: tuple[str, ...] = ()
    mandatory_formatters: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        for field in _FIELDS:
            _validate_items(field, getattr(self, field))

    def as_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "skills_on_demand": list(self.skills_on_demand),
            "mandatory_linters": list(self.mandatory_linters),
            "mandatory_formatters": list(self.mandatory_formatters),
        }


def dumps(preferences: FpchInterviewPreferences) -> str:
    """Serializa no JSON canônico do esquema, sempre terminado por newline."""
    if not isinstance(preferences, FpchInterviewPreferences):
        raise FpchInterviewError("preferências devem ser FpchInterviewPreferences")
    return json.dumps(
        preferences.as_dict(),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ) + "\n"


def _decode(raw: str | bytes) -> str:
    if isinstance(raw, bytes):
        if len(raw) > FPCH_INTERVIEW_MAX_BYTES:
            raise FpchInterviewError(
                f"respostas excedem o limite de {FPCH_INTERVIEW_MAX_BYTES} bytes"
            )
        try:
            return raw.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise FpchInterviewError("respostas não são UTF-8 válido") from exc
    if not isinstance(raw, str):
        raise FpchInterviewError("respostas devem ser texto ou bytes UTF-8")
    try:
        size = len(raw.encode("utf-8"))
    except UnicodeEncodeError as exc:
        raise FpchInterviewError("respostas não são UTF-8 válido") from exc
    if size > FPCH_INTERVIEW_MAX_BYTES:
        raise FpchInterviewError(
            f"respostas excedem o limite de {FPCH_INTERVIEW_MAX_BYTES} bytes"
        )
    return raw


def _closed_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise FpchInterviewError(f"chave JSON duplicada: {key}")
        result[key] = value
    return result


def loads(raw: str | bytes) -> FpchInterviewPreferences:
    """Carrega e valida o esquema fechado, sem aceitar coerções implícitas."""
    text = _decode(raw)
    try:
        payload = json.loads(text, object_pairs_hook=_closed_object)
    except FpchInterviewError:
        raise
    except (json.JSONDecodeError, RecursionError) as exc:
        raise FpchInterviewError(f"JSON de respostas inválido: {exc}") from exc

    if not isinstance(payload, dict):
        raise FpchInterviewError("JSON de respostas deve ser um objeto")
    keys = set(payload)
    if keys != _JSON_FIELDS:
        unknown = sorted(keys - _JSON_FIELDS)
        missing = sorted(_JSON_FIELDS - keys)
        details: list[str] = []
        if unknown:
            details.append("campos desconhecidos: " + ", ".join(unknown))
        if missing:
            details.append("campos ausentes: " + ", ".join(missing))
        raise FpchInterviewError("esquema de respostas inválido; " + "; ".join(details))
    if type(payload["schema_version"]) is not int:
        raise FpchInterviewError("schema_version deve ser inteiro")
    if payload["schema_version"] != FPCH_INTERVIEW_SCHEMA_VERSION:
        raise FpchInterviewError(
            f"schema_version não suportada: {payload['schema_version']}"
        )

    values: dict[str, tuple[str, ...]] = {}
    for field in _FIELDS:
        raw_items = payload[field]
        if not isinstance(raw_items, list):
            raise FpchInterviewError(f"{field} deve ser uma lista JSON")
        values[field] = tuple(raw_items)
    return FpchInterviewPreferences(**values)


def load_answers(
    source: str | Path | TextIO | BinaryIO,
) -> FpchInterviewPreferences:
    """Lê no máximo 64 KiB de um caminho ou stream e valida as respostas."""
    if isinstance(source, (str, Path)):
        path = Path(source)
        try:
            with path.open("rb") as stream:
                raw = stream.read(FPCH_INTERVIEW_MAX_BYTES + 1)
        except OSError as exc:
            raise FpchInterviewError(f"não foi possível ler respostas de {path}: {exc}") from exc
    else:
        try:
            raw = source.read(FPCH_INTERVIEW_MAX_BYTES + 1)
        except (OSError, UnicodeError, TypeError, AttributeError) as exc:
            raise FpchInterviewError(f"não foi possível ler stream de respostas: {exc}") from exc
    return loads(raw)


def _is_linklike(path: Path) -> bool:
    try:
        metadata = path.lstat()
    except FileNotFoundError:
        return False
    except OSError as exc:
        raise FpchInterviewError(f"não foi possível inspecionar saída {path}: {exc}") from exc

    if stat.S_ISLNK(metadata.st_mode):
        return True
    attributes = getattr(metadata, "st_file_attributes", 0)
    reparse = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0)
    if reparse and attributes & reparse:
        return True
    isjunction = getattr(os.path, "isjunction", None)
    return bool(isjunction is not None and isjunction(path))


def _reject_linklike_path(path: Path) -> None:
    """Recusa o alvo e todo ancestral existente que seja link/reparse point."""
    current = path
    while True:
        if _is_linklike(current):
            role = "saída" if current == path else "ancestral da saída"
            raise FpchInterviewError(
                f"{role} não pode ser link, junction ou reparse point: {current}"
            )
        parent = current.parent
        if parent == current:
            return
        current = parent


def _remove_partial_if_same(path: Path, created: os.stat_result) -> None:
    """Remove somente o diretório-entry que ainda identifica o arquivo criado."""
    try:
        current = path.lstat()
    except (FileNotFoundError, OSError):
        return
    try:
        same = os.path.samestat(created, current)
    except (AttributeError, OSError):
        same = (created.st_dev, created.st_ino) == (current.st_dev, current.st_ino)
    if not same:
        return
    try:
        path.unlink()
    except OSError:
        # A falha original continua sendo a informação acionável. Não arriscar
        # uma segunda tentativa que pudesse atingir um alvo trocado em corrida.
        return


def save_new(path: str | Path, preferences: FpchInterviewPreferences) -> Path:
    """Cria um arquivo novo exclusivamente; nunca substitui qualquer alvo.

    A inspeção dos ancestrais, ``O_EXCL`` e ``O_NOFOLLOW`` (quando disponível)
    reduzem a superfície de corrida. Eles não eliminam toda TOCTOU envolvendo a
    troca concorrente de um diretório ancestral, pois a API portátil usada aqui
    não ancora cada componente do caminho em descritores já abertos.
    """
    target = Path(path)
    _reject_linklike_path(target)
    if target.exists():
        kind = "diretório" if target.is_dir() else "arquivo"
        raise FpchInterviewError(f"saída já existe ({kind}): {target}")

    payload = dumps(preferences)
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    flags |= getattr(os, "O_BINARY", 0)
    flags |= getattr(os, "O_NOFOLLOW", 0)
    descriptor: int | None = None
    created: os.stat_result | None = None
    try:
        descriptor = os.open(target, flags, 0o600)
        created = os.fstat(descriptor)
        stream = os.fdopen(descriptor, "w", encoding="utf-8", newline="\n")
        descriptor = None  # ``stream`` passa a ser o único dono do descritor.
        with stream:
            stream.write(payload)
            stream.flush()
    except FileExistsError as exc:
        raise FpchInterviewError(f"saída já existe: {target}") from exc
    except (OSError, UnicodeError) as exc:
        if descriptor is not None:
            try:
                os.close(descriptor)
            except OSError:
                pass
        if created is not None:
            _remove_partial_if_same(target, created)
        raise FpchInterviewError(f"não foi possível criar saída {target}: {exc}") from exc
    return target


def _interactive_items(raw: object, field: str) -> tuple[str, ...]:
    if type(raw) is not str:
        raise FpchInterviewError(f"resposta de {field} deve ser texto")
    if raw.strip() == "":
        return ()
    if any(ord(character) < 32 or ord(character) == 127 for character in raw):
        raise FpchInterviewError(f"resposta de {field} contém caractere de controle")
    parts = tuple(item.strip() for item in raw.split(","))
    return _validate_items(field, parts)


def interview(
    report: FpchDiscoveryReport,
    *,
    input_fn: Callable[[str], str] | None = None,
    output_fn: Callable[[str], object] | None = None,
) -> FpchInterviewPreferences:
    """Faz exatamente três perguntas, sem converter descobertas em escolhas."""
    if not isinstance(report, FpchDiscoveryReport):
        raise FpchInterviewError("report deve ser FpchDiscoveryReport")

    if input_fn is None:
        input_fn = input
    if output_fn is None:
        output_fn = print

    output_fn(f"Contexto do repositório: {report.repo}")
    output_fn(
        "Linguagens detectadas: " + (", ".join(report.languages) or "nenhuma")
    )
    output_fn(
        f"Dependências declaradas: {len(report.dependencies)}; "
        f"sinais de CI/CD: {len(report.ci_cd)}; avisos: {len(report.warnings)}."
    )
    output_fn(
        "As descobertas são apenas contexto e não selecionam nada automaticamente."
    )
    output_fn(
        "Não informe segredos, tokens, credenciais ou texto livre: somente IDs "
        "separados por vírgula."
    )

    try:
        answers = (
            _interactive_items(
                input_fn("Skills sob demanda (IDs; Enter = nenhum): "),
                "skills_on_demand",
            ),
            _interactive_items(
                input_fn("Linters obrigatórios (IDs; Enter = nenhum): "),
                "mandatory_linters",
            ),
            _interactive_items(
                input_fn("Formatadores obrigatórios (IDs; Enter = nenhum): "),
                "mandatory_formatters",
            ),
        )
    except (EOFError, KeyboardInterrupt) as exc:
        raise FpchInterviewError("entrevista interrompida antes das três respostas") from exc

    return FpchInterviewPreferences(
        skills_on_demand=answers[0],
        mandatory_linters=answers[1],
        mandatory_formatters=answers[2],
    )
