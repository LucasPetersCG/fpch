"""Checkpoints declarativos e offline para futuras instalações MCP.

Este módulo valida intenções imutáveis, lê seus arquivos JSON e confere bytes
de um artefato já obtido localmente contra o checkpoint (SHA-256 e nome).
Ele não consulta rede, não baixa nem resolve versões ou URLs, não extrai,
não executa código, não escreve arquivos e não instala ferramentas.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import re
import stat
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, NamedTuple
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
