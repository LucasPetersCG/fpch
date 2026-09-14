"""Checkpoints declarativos e offline para futuras instalações MCP.

Este módulo valida intenções imutáveis, lê seus arquivos JSON e confere bytes
de um artefato já obtido localmente contra o checkpoint (SHA-256 e nome).
Ele não consulta rede, não baixa nem resolve versões ou URLs, não extrai,
não executa código, não escreve arquivos e não instala ferramentas.

Também faz uma varredura offline e determinística de Unicode oculto em
metadados MCP (descrições de ferramentas, ``tools/list`` capturado, manifestos),
contra o canal de payload escondido em blocos TAG e similares
(arXiv:2607.05744). A varredura é fail-closed: não há allowlist nem exceção
para emoji (bandeiras usam TAG, que é justamente o canal do ataque), e o
conteúdo que uma sequência TAG soletra nunca é decodificado nem reportado.

Membros de arquivos compactados (``.tgz``, ``.zip``) ficam fora deste corte de
propósito: o que o cliente MCP efetivamente vê é o ``tools/list`` em tempo de
execução, e interpretar arquivos compactados em memória acrescenta risco de
bomba de descompressão e de divergência entre parsers.
"""

from __future__ import annotations

import functools
import hashlib
import hmac
import json
import os
import re
import stat
import unicodedata
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Iterator, Literal, NamedTuple
from urllib.parse import urlsplit

FPCH_MCP_CHECKPOINT_MAX_BYTES = 1024 * 1024
FPCH_MCP_VERSION_MAX_LENGTH = 256
FPCH_MCP_ARTIFACT_MAX_BYTES = 256 * 1024 * 1024

_READ_CHUNK_BYTES = 128 * 1024

_JSON_FIELDS = frozenset(("name", "source", "version", "expected_sha256"))
_NAME = re.compile(r"[a-z0-9](?:[a-z0-9._-]{0,126}[a-z0-9])?", re.ASCII)
_COMMIT = re.compile(r"(?:[0-9a-f]{40}|[0-9a-f]{64})", re.ASCII)
_SHA256 = re.compile(r"[0-9a-f]{64}", re.ASCII)
_MUTABLE_MARKERS = frozenset(("latest", "stable", "main", "head", "nightly", "next"))
_SEMVER_IDENTIFIER = r"(?:0|[1-9][0-9]*|[0-9]*[A-Za-z-][0-9A-Za-z-]*)"
_SEMVER = re.compile(
    rf"(?:0|[1-9][0-9]*)\."
    rf"(?:0|[1-9][0-9]*)\."
    rf"(?:0|[1-9][0-9]*)"
    rf"(?:-{_SEMVER_IDENTIFIER}(?:\.{_SEMVER_IDENTIFIER})*)?"
    r"(?:\+[0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*)?",
    re.ASCII,
)


class FpchMcpError(ValueError):
    """Um checkpoint MCP não pôde ser lido ou validado com segurança."""


def _valid_source(value: object, *, name: str, version: str) -> bool:
    if (
        type(value) is not str
        or not value
        or len(value) > 2048
        or not value.isascii()
        or "\\" in value
        or "%" in value
        or "?" in value
        or "#" in value
        or any(ord(character) < 0x20 or ord(character) == 0x7F for character in value)
        or not value.startswith("https://")
    ):
        return False
    try:
        parsed = urlsplit(value)
        # Avaliar ``port`` também recusa portas sintaticamente inválidas.
        parsed.port
    except ValueError:
        return False
    if (
        parsed.scheme != "https"
        or not parsed.netloc
        or not parsed.hostname
        or "@" in parsed.netloc
        or parsed.username is not None
        or parsed.password is not None
        or parsed.path in ("", "/")
        or not parsed.path.startswith("/")
        or parsed.query
        or parsed.fragment
    ):
        return False
    host = parsed.hostname
    if (
        host is None
        or parsed.netloc != host
        or host != host.lower()
        or host.endswith(".")
        or any(character.isspace() for character in host)
    ):
        return False
    decoded_path = parsed.path
    if (
        not decoded_path.isascii()
        or "\\" in decoded_path
        or "//" in decoded_path
        or any(
            character.isspace() or ord(character) < 0x20 or ord(character) == 0x7F
            for character in decoded_path
        )
        or any(segment in (".", "..") for segment in decoded_path.split("/"))
        or re.search(
            rf"(?<![A-Za-z0-9]){re.escape(name)}(?![A-Za-z0-9])",
            decoded_path,
            re.ASCII,
        )
        is None
        or re.search(
            rf"(?<![A-Za-z0-9]){re.escape(version)}(?![A-Za-z0-9])",
            decoded_path,
            re.ASCII,
        )
        is None
    ):
        return False
    path_tokens = set(re.split(r"[^a-z0-9]+", decoded_path.lower()))
    if path_tokens & _MUTABLE_MARKERS:
        return False
    return True


def _validate_fields(
    name: object,
    source: object,
    version: object,
    expected_sha256: object,
) -> None:
    if type(name) is not str or _NAME.fullmatch(name) is None:
        raise FpchMcpError("name de checkpoint MCP inválido")
    if type(version) is not str or len(version) > FPCH_MCP_VERSION_MAX_LENGTH or (
        _SEMVER.fullmatch(version) is None and _COMMIT.fullmatch(version) is None
    ):
        raise FpchMcpError("version de checkpoint MCP inválida")
    if not _valid_source(source, name=name, version=version):
        raise FpchMcpError("source de checkpoint MCP inválida")
    if type(expected_sha256) is not str or _SHA256.fullmatch(expected_sha256) is None:
        raise FpchMcpError("expected_sha256 de checkpoint MCP inválido")


@dataclass(frozen=True)
class FpchMcpInstallCheckpoint:
    """Intenção imutável e verificável de uma futura instalação MCP."""

    name: str
    source: str
    version: str
    expected_sha256: str

    def __post_init__(self) -> None:
        _validate_fields(self.name, self.source, self.version, self.expected_sha256)


def validate(value: FpchMcpInstallCheckpoint) -> None:
    """Revalida um checkpoint sem realizar I/O."""
    if type(value) is not FpchMcpInstallCheckpoint:
        raise FpchMcpError("checkpoint MCP deve ser FpchMcpInstallCheckpoint")
    _validate_fields(value.name, value.source, value.version, value.expected_sha256)


def dumps(value: FpchMcpInstallCheckpoint) -> str:
    """Serializa um checkpoint validado em JSON canônico."""
    validate(value)
    payload = {
        "expected_sha256": value.expected_sha256,
        "name": value.name,
        "source": value.source,
        "version": value.version,
    }
    encoded = json.dumps(
        payload, ensure_ascii=True, sort_keys=True, separators=(",", ":")
    ) + "\n"
    if len(encoded.encode("utf-8")) > FPCH_MCP_CHECKPOINT_MAX_BYTES:
        raise FpchMcpError("checkpoint MCP excede 1 MiB")
    return encoded


def _closed_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise FpchMcpError("JSON de checkpoint MCP contém chave duplicada")
        result[key] = value
    return result


def loads(raw: str | bytes) -> FpchMcpInstallCheckpoint:
    """Carrega o objeto JSON fechado de um checkpoint MCP."""
    if isinstance(raw, bytes):
        if len(raw) > FPCH_MCP_CHECKPOINT_MAX_BYTES:
            raise FpchMcpError("checkpoint MCP excede 1 MiB")
        try:
            text = raw.decode("utf-8", errors="strict")
        except UnicodeDecodeError as exc:
            raise FpchMcpError("checkpoint MCP não é UTF-8 válido") from exc
    elif type(raw) is str:
        try:
            size = len(raw.encode("utf-8", errors="strict"))
        except UnicodeEncodeError as exc:
            raise FpchMcpError("checkpoint MCP não é UTF-8 válido") from exc
        if size > FPCH_MCP_CHECKPOINT_MAX_BYTES:
            raise FpchMcpError("checkpoint MCP excede 1 MiB")
        text = raw
    else:
        raise FpchMcpError("checkpoint MCP deve ser texto ou bytes UTF-8")
    try:
        payload = json.loads(text, object_pairs_hook=_closed_object)
    except FpchMcpError:
        raise
    except (json.JSONDecodeError, RecursionError) as exc:
        raise FpchMcpError("JSON de checkpoint MCP inválido") from exc
    if not isinstance(payload, dict) or set(payload) != _JSON_FIELDS:
        raise FpchMcpError("campos inválidos no checkpoint MCP")
    return FpchMcpInstallCheckpoint(
        name=payload["name"],
        source=payload["source"],
        version=payload["version"],
        expected_sha256=payload["expected_sha256"],
    )


def _same_file(first: os.stat_result, second: os.stat_result) -> bool:
    try:
        return os.path.samestat(first, second)
    except (AttributeError, OSError):
        return (first.st_dev, first.st_ino) == (second.st_dev, second.st_ino)


def _same_snapshot(first: os.stat_result, second: os.stat_result) -> bool:
    """Compara identidade, modo, tamanho e ``mtime`` de dois snapshots.

    Detecta trocas de arquivo e alterações comuns; não detecta um rewrite
    in-place que preserve tamanho e restaure o ``mtime``.
    """
    return (
        _same_file(first, second)
        and first.st_mode == second.st_mode
        and first.st_size == second.st_size
        and first.st_mtime_ns == second.st_mtime_ns
    )


def _is_linklike(path: Path) -> bool:
    """Detecta link simbólico, junction ou reparse point sem segui-lo.

    Erros de ``lstat`` diferentes de ausência sobem como ``OSError`` (ou
    ``ValueError`` para caminho malformado); quem chama decide a mensagem.
    """
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


def _linklike_or_error(path: Path, label: str) -> bool:
    try:
        return _is_linklike(path)
    except FpchMcpError:
        raise
    except ValueError as exc:
        raise FpchMcpError(f"caminho de {label} inválido") from exc
    except OSError as exc:
        raise FpchMcpError(f"não foi possível inspecionar {label}") from exc


def _reject_linklike_path(path: Path, label: str = "checkpoint MCP") -> None:
    current = path
    while True:
        if _linklike_or_error(current, label):
            raise FpchMcpError(f"{label} não pode atravessar link ou reparse point")
        parent = current.parent
        if parent == current:
            return
        current = parent


def _absolute_path(path: object, label: str) -> Path:
    """Absolutiza lexicalmente (sem ``resolve()``) para vigiar os ancestrais do cwd."""
    if not isinstance(path, (str, Path)):
        raise FpchMcpError(f"caminho de {label} inválido")
    try:
        return Path(os.path.abspath(path))
    except (TypeError, ValueError) as exc:
        raise FpchMcpError(f"caminho de {label} inválido") from exc


def _disk_basename(target: Path, opened: os.stat_result, label: str) -> str:
    """Nome real da entrada de diretório que corresponde ao arquivo aberto.

    Procura no diretório-pai a entrada cujo nome coincide com o digitado sem
    diferenciar maiúsculas/minúsculas e cuja identidade (dispositivo e inode)
    é a do descritor aberto. Sem entrada única, falha fechado.
    """
    wanted = os.path.normcase(target.name).casefold()
    matches: list[str] = []
    try:
        with os.scandir(target.parent) as entries:
            for entry in entries:
                if os.path.normcase(entry.name).casefold() != wanted:
                    continue
                if _same_file(os.lstat(entry.path), opened):
                    matches.append(entry.name)
    except ValueError as exc:
        raise FpchMcpError(f"caminho de {label} inválido") from exc
    except OSError as exc:
        raise FpchMcpError(f"não foi possível inspecionar {label}") from exc
    if len(matches) != 1:
        raise FpchMcpError(
            f"nome em disco do {label} não pôde ser determinado sem ambiguidade"
        )
    return matches[0]


class _FpchGuardedRead(NamedTuple):
    size: int
    disk_basename: str | None


def _read_guarded(
    path: str | Path,
    *,
    label: str,
    max_bytes: int,
    limit_text: str,
    consume: Callable[[bytes], None],
    want_disk_basename: bool = False,
) -> _FpchGuardedRead:
    """Lê um arquivo regular em streaming, com limite e sem seguir links.

    Absolutiza o caminho lexicalmente, recusa link, junction ou reparse point
    no caminho e em todos os ancestrais (antes e depois da leitura), abre com
    ``O_NOFOLLOW`` quando disponível e compara snapshots de ``lstat``/``fstat``
    antes e depois da leitura. Cada bloco lido dentro do limite é entregue a
    ``consume``. Qualquer condição insegura levanta ``FpchMcpError`` — nunca há
    sucesso parcial.
    """
    target = _absolute_path(path, label)
    _reject_linklike_path(target, label)
    try:
        before = target.lstat()
    except ValueError as exc:
        raise FpchMcpError(f"caminho de {label} inválido") from exc
    except OSError as exc:
        raise FpchMcpError(f"não foi possível ler {label}") from exc
    if (
        _linklike_or_error(target, label)
        or not stat.S_ISREG(before.st_mode)
        or before.st_size > max_bytes
    ):
        raise FpchMcpError(f"arquivo de {label} inseguro ou grande demais")

    flags = os.O_RDONLY | getattr(os, "O_BINARY", 0)
    flags |= getattr(os, "O_NOFOLLOW", 0)
    descriptor: int | None = None
    try:
        descriptor = os.open(target, flags)
        opened = os.fstat(descriptor)
        current = target.lstat()
        if (
            not stat.S_ISREG(opened.st_mode)
            or _linklike_or_error(target, label)
            or not _same_snapshot(before, opened)
            or not _same_snapshot(opened, current)
        ):
            raise FpchMcpError(f"arquivo de {label} mudou durante abertura")
        size = 0
        while size <= max_bytes:
            chunk = os.read(descriptor, min(_READ_CHUNK_BYTES, max_bytes + 1 - size))
            if not chunk:
                break
            size += len(chunk)
            if size > max_bytes:
                break
            consume(chunk)
        after = os.fstat(descriptor)
        current = target.lstat()
        if (
            size > max_bytes
            or opened.st_size != after.st_size
            or size != after.st_size
            or not _same_snapshot(opened, after)
            or not _same_snapshot(after, current)
            or _linklike_or_error(target, label)
        ):
            raise FpchMcpError(f"arquivo de {label} mudou ou excede {limit_text}")
        disk_basename = (
            _disk_basename(target, opened, label) if want_disk_basename else None
        )
        # Revalida os ancestrais com o descritor ainda aberto: fecha a janela em
        # que um diretório intermediário vira junction/link durante a leitura.
        _reject_linklike_path(target, label)
    except FpchMcpError:
        raise
    except ValueError as exc:
        raise FpchMcpError(f"caminho de {label} inválido") from exc
    except OSError as exc:
        raise FpchMcpError(f"não foi possível ler {label}") from exc
    finally:
        if descriptor is not None:
            try:
                os.close(descriptor)
            except OSError:
                pass
    return _FpchGuardedRead(size=size, disk_basename=disk_basename)


def load(path: str | Path) -> FpchMcpInstallCheckpoint:
    """Lê com limite e sem seguir links/reparse points, depois valida o JSON."""
    chunks: list[bytes] = []
    _read_guarded(
        path,
        label="checkpoint MCP",
        max_bytes=FPCH_MCP_CHECKPOINT_MAX_BYTES,
        limit_text="1 MiB",
        consume=chunks.append,
    )
    return loads(b"".join(chunks))


def _source_basename(source: str) -> str:
    """Último segmento do caminho da ``source``; vazio se terminar em ``/``."""
    return urlsplit(source).path.rsplit("/", 1)[-1]


def _valid_basename(value: object) -> bool:
    return (
        type(value) is str
        and bool(value)
        and value not in (".", "..")
        and not any(character in value for character in "/\\\x00")
    )


def _validate_verification(value: FpchMcpArtifactVerification) -> None:
    """Recalcula cada veredito a partir dos dados; instância forjada falha."""
    _validate_fields(value.name, value.source, value.version, value.expected_sha256)
    if type(value.actual_sha256) is not str or _SHA256.fullmatch(value.actual_sha256) is None:
        raise FpchMcpError("actual_sha256 de verificação MCP inválido")
    if type(value.size_bytes) is not int or value.size_bytes < 0:
        raise FpchMcpError("size_bytes de verificação MCP inválido")
    if not _valid_basename(value.artifact_basename):
        raise FpchMcpError("artifact_basename de verificação MCP inválido")
    if (
        type(value.expected_basename) is not str
        or value.expected_basename != _source_basename(value.source)
    ):
        raise FpchMcpError("expected_basename de verificação MCP inválido")
    for flag in ("digest_matches", "basename_matches", "verified"):
        if type(getattr(value, flag)) is not bool:
            raise FpchMcpError(f"{flag} de verificação MCP deve ser booleano")
    digest_matches = hmac.compare_digest(
        value.actual_sha256.encode("ascii"), value.expected_sha256.encode("ascii")
    )
    basename_matches = bool(value.expected_basename) and (
        value.artifact_basename == value.expected_basename
    )
    if (
        value.digest_matches is not digest_matches
        or value.basename_matches is not basename_matches
        or value.verified is not (digest_matches and basename_matches)
    ):
        raise FpchMcpError("verificação de artefato MCP inconsistente")


@dataclass(frozen=True)
class FpchMcpArtifactVerification:
    """Resultado da conferência offline de um artefato local contra um checkpoint.

    ``verified`` só é verdadeiro quando o SHA-256 dos bytes lidos coincide com
    ``expected_sha256`` **e** o nome real da entrada em disco coincide
    exatamente (inclusive maiúsculas e minúsculas) com o último segmento da
    ``source``. Verificar não instala, não executa e não autoriza nada: é apenas
    evidência para o autor. Os vereditos são recalculados na construção.
    """

    name: str
    source: str
    version: str
    expected_sha256: str
    actual_sha256: str
    size_bytes: int
    artifact_basename: str
    expected_basename: str
    digest_matches: bool
    basename_matches: bool
    verified: bool

    def __post_init__(self) -> None:
        _validate_verification(self)

    def to_dict(self) -> dict[str, Any]:
        """Representação compatível com JSON, com todos os campos do resultado."""
        return {
            "actual_sha256": self.actual_sha256,
            "artifact_basename": self.artifact_basename,
            "basename_matches": self.basename_matches,
            "digest_matches": self.digest_matches,
            "expected_basename": self.expected_basename,
            "expected_sha256": self.expected_sha256,
            "name": self.name,
            "size_bytes": self.size_bytes,
            "source": self.source,
            "verified": self.verified,
            "version": self.version,
        }


def dumps_verification(value: FpchMcpArtifactVerification) -> str:
    """Revalida e serializa o resultado de verificação em JSON canônico."""
    if type(value) is not FpchMcpArtifactVerification:
        raise FpchMcpError(
            "verificação de artefato MCP deve ser FpchMcpArtifactVerification"
        )
    _validate_verification(value)
    return json.dumps(
        value.to_dict(), ensure_ascii=True, sort_keys=True, separators=(",", ":")
    ) + "\n"


def verify_artifact(
    checkpoint: FpchMcpInstallCheckpoint, path: str | Path
) -> FpchMcpArtifactVerification:
    """Confere bytes locais contra um checkpoint, sem rede, execução ou escrita.

    Caminho inseguro (link ou reparse point no caminho ou em ancestral, arquivo
    não regular, acima de ``FPCH_MCP_ARTIFACT_MAX_BYTES``, alterado durante a
    leitura, nome em disco ambíguo ou erro de sistema) levanta
    ``FpchMcpError``. Divergência de digest ou de nome devolve resultado com
    ``verified=False``. O nome comparado é o da entrada real no diretório, não
    o digitado: em sistema de arquivos que ignora caixa, ``x.tgz`` digitado
    para ``X.TGZ`` gravado não confere.
    """
    validate(checkpoint)
    hasher = hashlib.sha256()
    read = _read_guarded(
        path,
        label="artefato MCP",
        max_bytes=FPCH_MCP_ARTIFACT_MAX_BYTES,
        limit_text="o limite de artefato",
        consume=hasher.update,
        want_disk_basename=True,
    )
    actual_sha256 = hasher.hexdigest()
    artifact_basename = read.disk_basename or ""
    expected_basename = _source_basename(checkpoint.source)
    digest_matches = hmac.compare_digest(
        actual_sha256.encode("ascii"), checkpoint.expected_sha256.encode("ascii")
    )
    basename_matches = bool(expected_basename) and (
        artifact_basename == expected_basename
    )
    return FpchMcpArtifactVerification(
        name=checkpoint.name,
        source=checkpoint.source,
        version=checkpoint.version,
        expected_sha256=checkpoint.expected_sha256,
        actual_sha256=actual_sha256,
        size_bytes=read.size,
        artifact_basename=artifact_basename,
        expected_basename=expected_basename,
        digest_matches=digest_matches,
        basename_matches=basename_matches,
        verified=digest_matches and basename_matches,
    )


# --- Varredura offline de Unicode oculto em metadados MCP --------------------

FPCH_MCP_METADATA_MAX_BYTES = 1024 * 1024
FPCH_MCP_SCAN_MAX_FINDINGS = 100
# Limite do ``pointer`` reportado (em caracteres), incluindo o marcador final.
FPCH_MCP_SCAN_POINTER_MAX_CHARS = 1024
FPCH_MCP_SCAN_POINTER_TRUNCATION_MARKER = "\u2026"
FPCH_MCP_SCAN_POINTER_REPLACEMENT = "\ufffd"
FPCH_MCP_SCAN_RULESET = "fpch-hidden-unicode-1"
FPCH_MCP_SCAN_CLASSES = (
    "tag",
    "bidi",
    "zero_width",
    "variation_selector",
    "control",
    "line_separator",
    "invisible_filler",
    "noncharacter",
    "surrogate",
    "private_use",
    "other_format",
    "unassigned",
)

_SCAN_JSON_MAX_DEPTH = 256
_SCAN_FORMATS = ("json", "text")
_SCAN_CLASS_INDEX = {name: index for index, name in enumerate(FPCH_MCP_SCAN_CLASSES)}
# Pré-filtro: tudo que não é ASCII imprimível, TAB, LF ou CR é candidato.
_SCAN_CANDIDATE = re.compile(r"[^\x09\x0a\x0d\x20-\x7e]")
_JSON_POINTER = re.compile(r"(?:/(?:[^~/]|~[01])*)*", re.DOTALL)
# Escape ASCII estilo JSON/JS no modo texto: ``\uXXXX`` ou ``\u{X..XXXXXX}``.
_TEXT_ESCAPE = re.compile(r"\\u(?:\{([0-9A-Fa-f]{1,6})\}|([0-9A-Fa-f]{4}))")
_TEXT_LOW_SURROGATE = re.compile(r"\\u([dD][c-fC-F][0-9A-Fa-f]{2})")
_SCAN_TEXT_CANDIDATE = re.compile(
    r"[^\x09\x0a\x0d\x20-\x7e]|\\u(?:\{[0-9A-Fa-f]{1,6}\}|[0-9A-Fa-f]{4})"
)
_BIDI = frozenset(
    (0x061C, 0x200E, 0x200F, *range(0x202A, 0x202F), *range(0x2066, 0x206A))
)
_ZERO_WIDTH = frozenset((0x200B, 0x200C, 0x200D, 0x2060, 0xFEFF))
# U+2800 (BRAILLE PATTERN BLANK) é ``So`` e U+3164 é ``Lo``: sem a lista explícita
# passariam limpos. U+2064 (INVISIBLE PLUS) já cai em ``other_format`` (``Cf``);
# U+FFFC (OBJECT REPLACEMENT CHARACTER) é ``So`` com glifo visível e não é marcado.
_INVISIBLE_FILLER = frozenset(
    (0x034F, 0x115F, 0x1160, 0x17B4, 0x17B5, 0x2800, 0x3164, 0xFFA0)
)


@functools.lru_cache(maxsize=4096)
def _classify(cp: int) -> str | None:
    """Classificação ordenada (a primeira regra que casa vence)."""
    if 0x20 <= cp <= 0x7E or cp in (0x09, 0x0A, 0x0D):
        return None
    if 0xE0000 <= cp <= 0xE007F:
        return "tag"
    if cp in _BIDI:
        return "bidi"
    if cp in _ZERO_WIDTH:
        return "zero_width"
    if (
        0xFE00 <= cp <= 0xFE0F
        or 0xE0100 <= cp <= 0xE01EF
        or 0x180B <= cp <= 0x180D
        or cp == 0x180F
    ):
        return "variation_selector"
    if cp < 0x20 or 0x7F <= cp <= 0x9F:
        return "control"
    if cp in (0x2028, 0x2029):
        return "line_separator"
    if cp in _INVISIBLE_FILLER:
        return "invisible_filler"
    if 0xFDD0 <= cp <= 0xFDEF or cp & 0xFFFE == 0xFFFE:
        return "noncharacter"
    if 0xD800 <= cp <= 0xDFFF:
        return "surrogate"
    category = unicodedata.category(chr(cp))
    if category == "Co":
        return "private_use"
    if category == "Cf":
        return "other_format"
    if category == "Cn":
        return "unassigned"
    return None


def classify_code_point(cp: int) -> str | None:
    """Classe de Unicode oculto de um ponto de código, ou ``None`` se limpo."""
    if type(cp) is not int or not 0 <= cp <= 0x10FFFF:
        raise FpchMcpError("ponto de código Unicode inválido")
    return _classify(cp)


def _is_int(value: object, minimum: int) -> bool:
    return type(value) is int and value >= minimum


def _valid_reported_pointer(pointer: object, truncated: object) -> bool:
    """Custo limitado: o tamanho é conferido antes de qualquer regex."""
    if type(pointer) is not str or type(truncated) is not bool:
        return False
    if len(pointer) > FPCH_MCP_SCAN_POINTER_MAX_CHARS:
        return False
    body = pointer
    if truncated:
        if not pointer.endswith(FPCH_MCP_SCAN_POINTER_TRUNCATION_MARKER):
            return False
        body = pointer[: -len(FPCH_MCP_SCAN_POINTER_TRUNCATION_MARKER)]
    if _JSON_POINTER.fullmatch(body) is None:
        return False
    return not any(
        _classify(ord(match.group())) is not None
        for match in _SCAN_CANDIDATE.finditer(body)
    )


def _validate_finding(value: FpchMcpUnicodeFinding) -> None:
    if not _is_int(value.code_point, 0) or value.code_point > 0x10FFFF:
        raise FpchMcpError("code_point de achado Unicode inválido")
    category = _classify(value.code_point)
    if category is None or type(value.category) is not str or value.category != category:
        raise FpchMcpError("category de achado Unicode inconsistente")
    if type(value.escaped) is not bool:
        raise FpchMcpError("escaped de achado Unicode inválido")
    if type(value.pointer_truncated) is not bool:
        raise FpchMcpError("pointer_truncated de achado Unicode inválido")
    text_location = (
        _is_int(value.line, 1)
        and _is_int(value.column, 1)
        and value.pointer is None
        and value.in_key is None
        and value.index is None
        and value.pointer_truncated is False
    )
    json_location = (
        value.line is None
        and value.column is None
        and value.escaped is False
        and _valid_reported_pointer(value.pointer, value.pointer_truncated)
        and type(value.in_key) is bool
        and not (value.in_key and value.pointer == "")
        and _is_int(value.index, 0)
    )
    if not (text_location or json_location):
        raise FpchMcpError("localização de achado Unicode inválida")


@dataclass(frozen=True)
class FpchMcpUnicodeFinding:
    """Um ponto de código oculto e onde ele aparece.

    Formato ``text``: ``line`` e ``column`` (base 1; coluna em pontos de
    código). ``escaped`` é verdadeiro quando o ponto de código veio de um escape
    ASCII ``\\uXXXX`` (par de surrogates combinado) ou ``\\u{...}``; a posição
    é a da barra invertida. A detecção é conservadora: não interpreta aspas nem
    barras invertidas duplicadas.

    Formato ``json``: ``pointer``, ``in_key`` e ``index`` (posição do caractere
    dentro da string decodificada); ``escaped`` é sempre falso, pois o JSON já
    foi decodificado. O ``pointer`` segue a sintaxe RFC 6901, mas é sanitizado:
    todo ponto de código marcado em qualquer segmento (inclusive chaves
    ancestrais) vira U+FFFD e o total é limitado a
    ``FPCH_MCP_SCAN_POINTER_MAX_CHARS`` caracteres; quando cortado, termina em
    ``FPCH_MCP_SCAN_POINTER_TRUNCATION_MARKER`` (U+2026) e ``pointer_truncated``
    fica verdadeiro. Por isso o ``pointer`` reportado nem sempre resolve no
    documento original (U+FFFD literal e substituído são indistinguíveis). Os
    campos do outro formato ficam ``None``.
    """

    code_point: int
    category: str
    line: int | None
    column: int | None
    pointer: str | None
    in_key: bool | None
    index: int | None
    escaped: bool = False
    pointer_truncated: bool = False

    def __post_init__(self) -> None:
        _validate_finding(self)

    def to_dict(self) -> dict[str, Any]:
        """Representação compatível com JSON; ``code_point`` vira ``U+XXXX``."""
        return {
            "category": self.category,
            "code_point": f"U+{self.code_point:04X}",
            "column": self.column,
            "escaped": self.escaped,
            "in_key": self.in_key,
            "index": self.index,
            "line": self.line,
            "pointer": self.pointer,
            "pointer_truncated": self.pointer_truncated,
        }


def _validate_scan(value: FpchMcpMetadataScan) -> None:
    """Recalcula invariantes da varredura; instância forjada ou adulterada falha."""
    if type(value.format) is not str or value.format not in _SCAN_FORMATS:
        raise FpchMcpError("format de varredura MCP inválido")
    if not _is_int(value.size_bytes, 0) or value.size_bytes > FPCH_MCP_METADATA_MAX_BYTES:
        raise FpchMcpError("size_bytes de varredura MCP inválido")
    if type(value.sha256) is not str or _SHA256.fullmatch(value.sha256) is None:
        raise FpchMcpError("sha256 de varredura MCP inválido")
    if type(value.leading_bom) is not bool or (value.leading_bom and value.size_bytes < 3):
        raise FpchMcpError("leading_bom de varredura MCP inválido")
    if type(value.ruleset) is not str or value.ruleset != FPCH_MCP_SCAN_RULESET:
        raise FpchMcpError("ruleset de varredura MCP inválido")
    if (
        type(value.unicode_version) is not str
        or value.unicode_version != unicodedata.unidata_version
    ):
        raise FpchMcpError("unicode_version de varredura MCP inválida")
    if not _is_int(value.findings_total, 0):
        raise FpchMcpError("findings_total de varredura MCP inválido")
    if type(value.counts) is not tuple:
        raise FpchMcpError("counts de varredura MCP inválido")
    counts: dict[str, int] = {}
    previous = -1
    for entry in value.counts:
        if (
            type(entry) is not tuple
            or len(entry) != 2
            or type(entry[0]) is not str
            or entry[0] not in _SCAN_CLASS_INDEX
            or not _is_int(entry[1], 1)
            or _SCAN_CLASS_INDEX[entry[0]] <= previous
        ):
            raise FpchMcpError("counts de varredura MCP inválido")
        previous = _SCAN_CLASS_INDEX[entry[0]]
        counts[entry[0]] = entry[1]
    if sum(counts.values()) != value.findings_total:
        raise FpchMcpError("counts de varredura MCP não somam findings_total")
    if type(value.findings) is not tuple or len(value.findings) != min(
        value.findings_total, FPCH_MCP_SCAN_MAX_FINDINGS
    ):
        raise FpchMcpError("findings de varredura MCP inconsistente")
    detailed: dict[str, int] = {}
    last_position: tuple[int, int] | None = None
    for finding in value.findings:
        if type(finding) is not FpchMcpUnicodeFinding:
            raise FpchMcpError("findings de varredura MCP inconsistente")
        _validate_finding(finding)
        if (finding.pointer is None) is not (value.format == "text"):
            raise FpchMcpError("findings de varredura MCP não correspondem ao format")
        if value.format == "text":
            position = (finding.line, finding.column)
            if last_position is not None and position <= last_position:  # type: ignore[operator]
                raise FpchMcpError("findings de varredura MCP fora de ordem")
            last_position = position  # type: ignore[assignment]
        detailed[finding.category] = detailed.get(finding.category, 0) + 1
    truncated = value.findings_total > FPCH_MCP_SCAN_MAX_FINDINGS
    if any(detailed[name] > counts.get(name, 0) for name in detailed) or (
        not truncated and detailed != counts
    ):
        raise FpchMcpError("findings de varredura MCP não correspondem a counts")
    if type(value.truncated) is not bool or value.truncated is not truncated:
        raise FpchMcpError("truncated de varredura MCP inconsistente")
    if type(value.clean) is not bool or value.clean is not (value.findings_total == 0):
        raise FpchMcpError("clean de varredura MCP inconsistente")


@dataclass(frozen=True)
class FpchMcpMetadataScan:
    """Resultado determinístico da varredura de Unicode oculto em metadados MCP.

    ``findings_total`` e ``counts`` são exatos; ``findings`` guarda só os
    primeiros ``FPCH_MCP_SCAN_MAX_FINDINGS`` em ordem de documento. ``clean``
    só é verdadeiro sem nenhum achado. Não registra o nome do arquivo. Os
    vereditos são recalculados na construção.
    """

    format: str
    size_bytes: int
    sha256: str
    leading_bom: bool
    ruleset: str
    unicode_version: str
    findings_total: int
    counts: tuple[tuple[str, int], ...]
    findings: tuple[FpchMcpUnicodeFinding, ...]
    truncated: bool
    clean: bool

    def __post_init__(self) -> None:
        _validate_scan(self)

    def to_dict(self) -> dict[str, Any]:
        """Representação compatível com JSON, com todos os campos do resultado."""
        return {
            "clean": self.clean,
            "counts": {name: count for name, count in self.counts},
            "findings": [finding.to_dict() for finding in self.findings],
            "findings_total": self.findings_total,
            "format": self.format,
            "leading_bom": self.leading_bom,
            "ruleset": self.ruleset,
            "sha256": self.sha256,
            "size_bytes": self.size_bytes,
            "truncated": self.truncated,
            "unicode_version": self.unicode_version,
        }


def dumps_scan(value: FpchMcpMetadataScan) -> str:
    """Revalida e serializa a varredura em JSON canônico somente ASCII."""
    if type(value) is not FpchMcpMetadataScan:
        raise FpchMcpError("varredura MCP deve ser FpchMcpMetadataScan")
    _validate_scan(value)
    return json.dumps(
        value.to_dict(), ensure_ascii=True, sort_keys=True, separators=(",", ":")
    ) + "\n"


class _FpchScanCollector:
    """Acumula contagens exatas e os primeiros achados detalhados."""

    __slots__ = ("cap", "counts", "findings", "total")

    def __init__(self) -> None:
        self.cap = FPCH_MCP_SCAN_MAX_FINDINGS
        self.counts = dict.fromkeys(FPCH_MCP_SCAN_CLASSES, 0)
        self.findings: list[FpchMcpUnicodeFinding] = []
        self.total = 0


def _text_escape_code_point(text: str, match: re.Match[str]) -> tuple[int, int]:
    """Decodifica um escape ASCII; retorna ``(ponto de código, fim do escape)``.

    ``\\uD8xx`` seguido de ``\\uDCxx`` combina em um único ponto suplementar;
    surrogate isolado é reportado como tal. ``\\u{...}`` acima de U+10FFFF é
    ignorado (ponto de código ``-1``).
    """
    braced, short = match.group(1), match.group(2)
    end = match.end()
    if braced is not None:
        value = int(braced, 16)
        return (value if value <= 0x10FFFF else -1), end
    value = int(short, 16)
    if 0xD800 <= value <= 0xDBFF:
        low = _TEXT_LOW_SURROGATE.match(text, end)
        if low is not None:
            low_value = int(low.group(1), 16)
            combined = 0x10000 + ((value - 0xD800) << 10) + (low_value - 0xDC00)
            return combined, low.end()
    return value, end


def _scan_text(text: str, collector: _FpchScanCollector) -> None:
    """Só ``\\n`` avança a linha (CRLF conta uma quebra); coluna em pontos de código.

    Além dos pontos de código crus, reporta escapes ASCII estilo JSON/JS
    (``\\uXXXX``, pares de surrogates, ``\\u{...}``) cujo ponto decodificado é
    marcado, com ``escaped=True``.
    """
    line = 1
    line_start = 0
    last = 0
    cursor = 0
    while True:
        match = _SCAN_TEXT_CANDIDATE.search(text, cursor)
        if match is None:
            break
        cursor = match.end()
        escaped = len(match.group()) > 1
        if escaped:
            escape = _TEXT_ESCAPE.match(text, match.start())
            if escape is None:  # pragma: no cover - regexes equivalentes
                continue
            code_point, cursor = _text_escape_code_point(text, escape)
            if code_point < 0:
                continue
        else:
            code_point = ord(match.group())
        category = _classify(code_point)
        if category is None:
            continue
        collector.total += 1
        collector.counts[category] += 1
        if len(collector.findings) >= collector.cap:
            continue
        position = match.start()
        breaks = text.count("\n", last, position)
        if breaks:
            line += breaks
            line_start = text.rfind("\n", last, position) + 1
        last = position
        collector.findings.append(
            FpchMcpUnicodeFinding(
                code_point=code_point,
                category=category,
                line=line,
                column=position - line_start + 1,
                pointer=None,
                in_key=None,
                index=None,
                escaped=escaped,
            )
        )


def _scan_json_string(
    value: str, tokens: list[str], in_key: bool, collector: _FpchScanCollector
) -> None:
    pointer: str | None = None
    truncated = False
    for match in _SCAN_CANDIDATE.finditer(value):
        code_point = ord(match.group())
        category = _classify(code_point)
        if category is None:
            continue
        collector.total += 1
        collector.counts[category] += 1
        if len(collector.findings) >= collector.cap:
            continue
        if pointer is None:
            pointer, truncated = _reported_pointer(tokens)
        collector.findings.append(
            FpchMcpUnicodeFinding(
                code_point=code_point,
                category=category,
                line=None,
                column=None,
                pointer=pointer,
                in_key=in_key,
                index=match.start(),
                pointer_truncated=truncated,
            )
        )


def _sanitize_pointer_text(text: str) -> str:
    return _SCAN_CANDIDATE.sub(
        lambda match: FPCH_MCP_SCAN_POINTER_REPLACEMENT
        if _classify(ord(match.group())) is not None
        else match.group(),
        text,
    )


def _reported_pointer(tokens: list[str]) -> tuple[str, bool]:
    """Pointer sanitizado e limitado; custo O(profundidade + limite), não O(chave)."""
    cap = FPCH_MCP_SCAN_POINTER_MAX_CHARS
    parts: list[str] = []
    size = 0
    for token in tokens:
        # Só o prefixo necessário de cada segmento é sanitizado.
        piece = "/" + _sanitize_pointer_text(token[:cap])
        parts.append(piece)
        size += len(piece) if len(token) <= cap else cap + 2
        if size > cap:
            break
    if size <= cap:
        return "".join(parts), False
    marker = FPCH_MCP_SCAN_POINTER_TRUNCATION_MARKER
    body = "".join(parts)[: cap - len(marker)]
    if body.endswith("~"):
        # Não corta no meio de ``~0``/``~1``.
        body = body[:-1]
    return body + marker, True


def _pointer_token(key: str) -> str:
    return key.replace("~", "~0").replace("/", "~1")


def _closed_metadata_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise FpchMcpError("JSON de metadados MCP contém chave duplicada")
        result[key] = value
    return result


def _reject_json_constant(_name: str) -> Any:
    raise FpchMcpError("JSON de metadados MCP contém constante não finita")


def _children(
    value: dict[str, Any] | list[Any],
) -> Iterator[tuple[str, str | None, Any]]:
    if isinstance(value, dict):
        return ((_pointer_token(key), key, child) for key, child in value.items())
    return ((str(index), None, child) for index, child in enumerate(value))


def _load_metadata_json(text: str) -> Any:
    """Loader estrito: rejeita chave duplicada, NaN/Infinity e JSON inválido."""
    try:
        return json.loads(
            text,
            object_pairs_hook=_closed_metadata_object,
            parse_constant=_reject_json_constant,
        )
    except FpchMcpError:
        raise
    except (ValueError, RecursionError) as exc:
        raise FpchMcpError("JSON de metadados MCP inválido") from exc


def _scan_json(text: str, collector: _FpchScanCollector) -> None:
    """Percorre iterativamente, em ordem de documento, chaves e strings decodificadas."""
    root = _load_metadata_json(text)
    if isinstance(root, str):
        _scan_json_string(root, [], False, collector)
        return
    if not isinstance(root, (dict, list)):
        return
    tokens: list[str] = []
    stack: list[Iterator[tuple[str, str | None, Any]]] = [_children(root)]
    while stack:
        item = next(stack[-1], None)
        if item is None:
            stack.pop()
            if tokens:
                tokens.pop()
            continue
        token, key, child = item
        tokens.append(token)
        if key is not None:
            _scan_json_string(key, tokens, True, collector)
        if isinstance(child, str):
            _scan_json_string(child, tokens, False, collector)
        elif isinstance(child, (dict, list)):
            if len(stack) + 1 > _SCAN_JSON_MAX_DEPTH:
                raise FpchMcpError(
                    f"JSON de metadados MCP excede profundidade {_SCAN_JSON_MAX_DEPTH}"
                )
            stack.append(_children(child))
            continue
        tokens.pop()


def _validate_format(format: object, allowed: tuple[str, ...]) -> str:
    if type(format) is not str or format not in allowed:
        raise FpchMcpError("format de varredura MCP inválido")
    return format


def _decode_metadata(raw: bytes) -> tuple[str, bool]:
    try:
        text = raw.decode("utf-8", errors="strict")
    except UnicodeDecodeError as exc:
        raise FpchMcpError("metadados MCP não são UTF-8 válido") from exc
    leading_bom = text.startswith("﻿")
    if leading_bom:
        text = text[1:]
    return text, leading_bom


def _scan(raw: bytes, format: str, sha256: str) -> FpchMcpMetadataScan:
    text, leading_bom = _decode_metadata(raw)
    collector = _FpchScanCollector()
    if format == "json":
        _scan_json(text, collector)
    else:
        _scan_text(text, collector)
    return FpchMcpMetadataScan(
        format=format,
        size_bytes=len(raw),
        sha256=sha256,
        leading_bom=leading_bom,
        ruleset=FPCH_MCP_SCAN_RULESET,
        unicode_version=unicodedata.unidata_version,
        findings_total=collector.total,
        counts=tuple(
            (name, collector.counts[name])
            for name in FPCH_MCP_SCAN_CLASSES
            if collector.counts[name]
        ),
        findings=tuple(collector.findings),
        truncated=collector.total > collector.cap,
        clean=collector.total == 0,
    )


def scan_bytes(raw: bytes, *, format: Literal["json", "text"]) -> FpchMcpMetadataScan:
    """Varre bytes UTF-8 de metadados MCP em memória, sem I/O."""
    if not isinstance(raw, bytes):
        raise FpchMcpError("metadados MCP devem ser bytes UTF-8")
    chosen = _validate_format(format, _SCAN_FORMATS)
    if len(raw) > FPCH_MCP_METADATA_MAX_BYTES:
        raise FpchMcpError("metadados MCP excedem 1 MiB")
    raw = bytes(raw)
    return _scan(raw, chosen, hashlib.sha256(raw).hexdigest())


def scan_metadata(
    path: str | Path, *, format: Literal["auto", "json", "text"] = "auto"
) -> FpchMcpMetadataScan:
    """Lê metadados MCP com limite e sem seguir links, depois varre Unicode oculto.

    Com ``format="auto"``, sufixo ``.json`` (sem diferenciar caixa) usa o modo
    JSON; qualquer outro, o modo texto. O modo texto também reporta escapes
    ASCII ``\\uXXXX``/``\\u{...}`` de pontos marcados, mas não conhece a
    gramática do arquivo; quem precisa de garantia deve exigir ``format="json"``. Caminho inseguro, arquivo acima de
    ``FPCH_MCP_METADATA_MAX_BYTES``, UTF-8 inválido ou JSON inválido levantam
    ``FpchMcpError``. Não usa rede, não executa nada e não escreve arquivos.
    """
    chosen = _validate_format(format, ("auto", *_SCAN_FORMATS))
    target = _absolute_path(path, "metadados MCP")
    if chosen == "auto":
        chosen = "json" if target.suffix.lower() == ".json" else "text"
    raw, sha256 = _read_metadata_bytes(path)
    return _scan(raw, chosen, sha256)


def read_metadata_json(path: str | Path) -> tuple[str, Any]:
    """Lê metadados MCP com as mesmas travas da varredura e devolve ``(sha256, raiz)``.

    Usa o mesmo loader estrito do modo JSON de ``scan_metadata`` (UTF-8
    estrito, BOM inicial ignorado, sem chave duplicada nem constante não
    finita). O ``sha256`` permite ao chamador amarrar a raiz aos bytes
    varridos. Não varre Unicode oculto, não usa rede e não escreve arquivos.
    """
    _absolute_path(path, "metadados MCP")
    raw, sha256 = _read_metadata_bytes(path)
    text, _leading_bom = _decode_metadata(raw)
    return sha256, _load_metadata_json(text)


def _read_metadata_bytes(path: str | Path) -> tuple[bytes, str]:
    hasher = hashlib.sha256()
    chunks: list[bytes] = []

    def consume(chunk: bytes) -> None:
        hasher.update(chunk)
        chunks.append(chunk)

    read = _read_guarded(
        path,
        label="metadados MCP",
        max_bytes=FPCH_MCP_METADATA_MAX_BYTES,
        limit_text="1 MiB",
        consume=consume,
    )
    raw = b"".join(chunks)
    if len(raw) != read.size:
        raise FpchMcpError("arquivo de metadados MCP mudou durante leitura")
    return raw, hasher.hexdigest()
