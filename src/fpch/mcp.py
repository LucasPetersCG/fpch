"""Checkpoints declarativos e offline para futuras instalações MCP.

Este módulo somente valida intenções imutáveis e lê seus arquivos JSON.
Ele não consulta rede, resolve versões, executa código ou instala ferramentas.
"""

from __future__ import annotations

import json
import os
import re
import stat
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

FPCH_MCP_CHECKPOINT_MAX_BYTES = 1024 * 1024
FPCH_MCP_VERSION_MAX_LENGTH = 256

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
    """Compara identidade e metadados que denunciam rewrite in-place."""
    return (
        _same_file(first, second)
        and first.st_mode == second.st_mode
        and first.st_size == second.st_size
        and first.st_mtime_ns == second.st_mtime_ns
    )


def _is_linklike(path: Path) -> bool:
    try:
        metadata = path.lstat()
    except FileNotFoundError:
        return False
    except OSError as exc:
        raise FpchMcpError("não foi possível inspecionar checkpoint MCP") from exc
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
            raise FpchMcpError("checkpoint MCP não pode atravessar link ou reparse point")
        parent = current.parent
        if parent == current:
            return
        current = parent


def load(path: str | Path) -> FpchMcpInstallCheckpoint:
    """Lê com limite e sem seguir links/reparse points, depois valida o JSON."""
    if not isinstance(path, (str, Path)):
        raise FpchMcpError("caminho de checkpoint MCP inválido")
    target = Path(path)
    _reject_linklike_path(target)
    try:
        before = target.lstat()
    except OSError as exc:
        raise FpchMcpError("não foi possível ler checkpoint MCP") from exc
    if (
        _is_linklike(target)
        or not stat.S_ISREG(before.st_mode)
        or before.st_size > FPCH_MCP_CHECKPOINT_MAX_BYTES
    ):
        raise FpchMcpError("arquivo de checkpoint MCP inseguro ou grande demais")

    flags = os.O_RDONLY | getattr(os, "O_BINARY", 0)
    flags |= getattr(os, "O_NOFOLLOW", 0)
    descriptor: int | None = None
    try:
        descriptor = os.open(target, flags)
        opened = os.fstat(descriptor)
        current = target.lstat()
        if (
            not stat.S_ISREG(opened.st_mode)
            or _is_linklike(target)
            or not _same_snapshot(before, opened)
            or not _same_snapshot(opened, current)
        ):
            raise FpchMcpError("arquivo de checkpoint MCP mudou durante abertura")
        chunks: list[bytes] = []
        size = 0
        while size <= FPCH_MCP_CHECKPOINT_MAX_BYTES:
            chunk = os.read(
                descriptor,
                min(128 * 1024, FPCH_MCP_CHECKPOINT_MAX_BYTES + 1 - size),
            )
            if not chunk:
                break
            chunks.append(chunk)
            size += len(chunk)
        after = os.fstat(descriptor)
        current = target.lstat()
        if (
            size > FPCH_MCP_CHECKPOINT_MAX_BYTES
            or opened.st_size != after.st_size
            or size != after.st_size
            or not _same_snapshot(opened, after)
            or not _same_snapshot(after, current)
            or _is_linklike(target)
        ):
            raise FpchMcpError("arquivo de checkpoint MCP mudou ou excede 1 MiB")
        raw = b"".join(chunks)
    except FpchMcpError:
        raise
    except OSError as exc:
        raise FpchMcpError("não foi possível ler checkpoint MCP") from exc
    finally:
        if descriptor is not None:
            try:
                os.close(descriptor)
            except OSError:
                pass
    return loads(raw)
