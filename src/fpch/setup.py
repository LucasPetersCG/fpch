"""Planejamento e aplicação transacional do setup local do FPCH.

Esta primeira fatia de C4 é deliberadamente estreita: gera somente arquivos
locais conhecidos, nunca sobrescreve conteúdo e não instala ferramentas ou
integrações externas. O plano é determinístico, autocontido e precisa ser
confirmado pelo seu ``plan_id`` antes de qualquer mutação.

Regime de concorrência: o contrato é *trusted single writer*. O módulo usa lock,
journal com ``fsync``, criação exclusiva, ``O_NOFOLLOW`` quando disponível e
revalida identidades antes/depois. A biblioteca padrão do Python, especialmente
no Windows, não oferece ``openat2``/handles ancorados para toda a árvore; por
isso, uma troca hostil de ancestrais pelo mesmo usuário durante syscalls ainda é
fora do modelo suportado e provoca falha quando detectada.
"""

from __future__ import annotations

import hashlib
import json
import os
import stat
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Literal

from . import audit
from .discovery import (
    FpchCiCdSignal,
    FpchDependency,
    FpchDiscoveryReport,
    FpchDiscoveryWarning,
    discover,
)
from .interview import (
    FpchInterviewError,
    FpchInterviewPreferences,
    loads as load_interview,
)

FPCH_SETUP_SCHEMA_VERSION = 1
FPCH_SETUP_MAX_BYTES = 1024 * 1024
FPCH_SETUP_EXISTING_MAX_BYTES = 1024 * 1024

_ARTIFACT_PATHS = (
    "AGENTS.md",
    "CLAUDE.md",
    ".fpch/setup-manifest.json",
)
_MEDIA_TYPES = {
    "AGENTS.md": "text/markdown",
    "CLAUDE.md": "text/markdown",
    ".fpch/setup-manifest.json": "application/json",
}
_STATES = frozenset(("create", "unchanged", "conflict"))
_CONFLICT_REASONS = frozenset(
    (
        "existing_content_differs",
        "existing_non_regular",
        "existing_too_large",
        "unsafe_path",
    )
)
_HEX_LENGTH = 64
_LOCK_NAME = "setup.lock"
_JOURNAL_NAME = "setup-transaction.json"
_JOURNAL_STATES = frozenset(
    ("prepared", "applying", "auditing", "committed", "recovery_required")
)


class FpchSetupError(ValueError):
    """O setup não pôde ser planejado, validado ou aplicado com segurança."""


@dataclass(frozen=True)
class FpchPlannedArtifact:
    """Um artefato local desejado e seu estado observado durante o plano."""

    path: str
    media_type: str
    content: str
    sha256: str
    state: Literal["create", "unchanged", "conflict"] = "create"


@dataclass(frozen=True)
class FpchSetupConflict:
    """Conflito new-files-only encontrado sem alterar o alvo."""

    path: str
    reason: str
    actual_sha256: str | None


@dataclass(frozen=True)
class FpchRepoIdentity:
    """Identidade física mínima da raiz observada durante o plano."""

    device: int
    inode: int
    mode: int


@dataclass(frozen=True)
class FpchSetupPlan:
    """Plano fechado cujo identificador cobre metadados e bytes desejados."""

    repo: str
    discovery_sha256: str
    interview_sha256: str
    discovery_snapshot: str
    interview_snapshot: str
    repo_identity: FpchRepoIdentity
    artifacts: tuple[FpchPlannedArtifact, ...]
    conflicts: tuple[FpchSetupConflict, ...]
    unresolved_mcps: tuple[str, ...]
    plan_id: str


@dataclass(frozen=True)
class FpchSetupResult:
    """Resultado observável de uma tentativa de aplicação."""

    ok: bool
    plan_id: str
    created: tuple[str, ...]
    unchanged: tuple[str, ...]
    conflict: tuple[str, ...]
    audit_written: bool


def _canonical_json(value: object, *, newline: bool = False) -> str:
    text = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return text + ("\n" if newline else "")


def _sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _sha256_text(value: str) -> str:
    try:
        encoded = value.encode("utf-8")
    except (AttributeError, UnicodeEncodeError) as exc:
        raise FpchSetupError("texto não pode ser codificado como UTF-8") from exc
    return _sha256_bytes(encoded)


def _is_sha256(value: object) -> bool:
    return (
        type(value) is str
        and len(value) == _HEX_LENGTH
        and all(character in "0123456789abcdef" for character in value)
    )


def _is_linklike(path: Path) -> bool:
    """Reconhece links, junctions e reparse points sem seguir o destino."""
    try:
        metadata = path.lstat()
    except OSError:
        return False
    if stat.S_ISLNK(metadata.st_mode):
        return True
    reparse_flag = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
    file_attributes = getattr(metadata, "st_file_attributes", 0)
    if file_attributes & reparse_flag:
        return True
    isjunction = getattr(os.path, "isjunction", None)
    return bool(isjunction is not None and isjunction(path))


def _lstat(path: Path) -> os.stat_result | None:
    try:
        return path.lstat()
    except FileNotFoundError:
        return None
    except OSError as exc:
        raise FpchSetupError(f"não foi possível inspecionar {path}: {exc}") from exc


def _validated_root(repo: str) -> Path:
    if type(repo) is not str or not repo:
        raise FpchSetupError("repo deve ser caminho absoluto não vazio")
    root = Path(repo)
    if not root.is_absolute():
        raise FpchSetupError("repo deve ser caminho absoluto")
    try:
        resolved = root.resolve(strict=True)
    except OSError as exc:
        raise FpchSetupError(f"repositório indisponível: {root}: {exc}") from exc
    if root != resolved:
        raise FpchSetupError("repo deve estar normalizado e não atravessar links")
    metadata = _lstat(root)
    if metadata is None or not stat.S_ISDIR(metadata.st_mode):
        raise FpchSetupError(f"repo não é diretório: {root}")
    if _is_linklike(root):
        raise FpchSetupError(f"repo não pode ser link, junction ou reparse point: {root}")
    return root


def _identity(metadata: os.stat_result) -> FpchRepoIdentity:
    return FpchRepoIdentity(
        device=int(metadata.st_dev),
        inode=int(metadata.st_ino),
        mode=int(stat.S_IMODE(metadata.st_mode)),
    )


def _root_identity(root: Path) -> FpchRepoIdentity:
    metadata = _lstat(root)
    if metadata is None:
        raise FpchSetupError("raiz desapareceu durante o setup")
    return _identity(metadata)


def _assert_root_identity(root: Path, expected: FpchRepoIdentity) -> None:
    if _root_identity(root) != expected:
        raise FpchSetupError("identidade física da raiz mudou desde o plano")


def _target(root: Path, relative: str) -> Path:
    if relative not in _ARTIFACT_PATHS:
        raise FpchSetupError(f"artefato fora da lista permitida: {relative!r}")
    parts = relative.split("/")
    if any(part in ("", ".", "..") for part in parts):
        raise FpchSetupError(f"caminho de artefato inválido: {relative!r}")
    return root.joinpath(*parts)


def _path_problem(root: Path, relative: str) -> str | None:
    """Retorna a razão estrutural que torna um alvo inseguro, se houver."""
    target = _target(root, relative)
    current = root
    for part in relative.split("/")[:-1]:
        current = current / part
        metadata = _lstat(current)
        if metadata is None:
            break
        if _is_linklike(current) or not stat.S_ISDIR(metadata.st_mode):
            return "unsafe_path"
    metadata = _lstat(target)
    if metadata is not None and _is_linklike(target):
        return "unsafe_path"
    return None


def _file_sha256(path: Path, *, max_bytes: int | None = None) -> str:
    """Hash por descritor, recusando links e mudança de identidade/tamanho."""
    before = _lstat(path)
    if before is None or _is_linklike(path) or not stat.S_ISREG(before.st_mode):
        raise FpchSetupError(f"alvo não é arquivo regular seguro: {path}")
    if max_bytes is not None and before.st_size > max_bytes:
        raise FpchSetupError(f"arquivo excede limite de leitura: {path}")
    flags = os.O_RDONLY | getattr(os, "O_BINARY", 0)
    flags |= getattr(os, "O_NOFOLLOW", 0)
    descriptor: int | None = None
    try:
        descriptor = os.open(path, flags)
        opened = os.fstat(descriptor)
        if not stat.S_ISREG(opened.st_mode) or not _same_file(before, opened):
            raise FpchSetupError(f"identidade de arquivo mudou durante leitura: {path}")
        if max_bytes is not None and opened.st_size > max_bytes:
            raise FpchSetupError(f"arquivo excede limite de leitura: {path}")
        digest = hashlib.sha256()
        remaining = opened.st_size
        while remaining:
            chunk = os.read(descriptor, min(128 * 1024, remaining))
            if not chunk:
                raise FpchSetupError(f"arquivo encolheu durante leitura: {path}")
            digest.update(chunk)
            remaining -= len(chunk)
        if os.read(descriptor, 1):
            raise FpchSetupError(f"arquivo cresceu durante leitura: {path}")
        after = os.fstat(descriptor)
        if not _same_file(opened, after) or opened.st_size != after.st_size:
            raise FpchSetupError(f"arquivo mudou durante leitura: {path}")
        return digest.hexdigest()
    except OSError as exc:
        raise FpchSetupError(f"não foi possível ler {path}: {exc}") from exc
    finally:
        if descriptor is not None:
            try:
                os.close(descriptor)
            except OSError:
                pass


def _artifact_state(
    root: Path, artifact: FpchPlannedArtifact
) -> tuple[str, FpchSetupConflict | None]:
    problem = _path_problem(root, artifact.path)
    if problem is not None:
        return "conflict", FpchSetupConflict(artifact.path, problem, None)
    target = _target(root, artifact.path)
    metadata = _lstat(target)
    if metadata is None:
        return "create", None
    if not stat.S_ISREG(metadata.st_mode):
        return (
            "conflict",
            FpchSetupConflict(artifact.path, "existing_non_regular", None),
        )
    if metadata.st_size > FPCH_SETUP_EXISTING_MAX_BYTES:
        return (
            "conflict",
            FpchSetupConflict(artifact.path, "existing_too_large", None),
        )
    actual = _file_sha256(target, max_bytes=FPCH_SETUP_EXISTING_MAX_BYTES)
    if actual == artifact.sha256:
        return "unchanged", None
    return (
        "conflict",
        FpchSetupConflict(artifact.path, "existing_content_differs", actual),
    )


def _json_items(values: tuple[str, ...]) -> str:
    if not values:
        return "nenhum"
    return ", ".join(_canonical_json(value) for value in values)


def _agents_content(
    report: FpchDiscoveryReport,
    preferences: FpchInterviewPreferences,
    discovery_sha256: str,
    interview_sha256: str,
) -> str:
    languages = _canonical_json(list(report.languages))
    ci_providers = _canonical_json([signal.provider for signal in report.ci_cd])
    return (
        "# AGENTS.md — Camada de Controle Gerada pelo FPCH\n"
        "\n"
        "> Arquivo gerenciado pelo FPCH. Não contém segredos nem instala ferramentas.\n"
        "\n"
        "## Contexto detectado\n"
        "\n"
        f"- Linguagens: `{languages}`\n"
        f"- Dependências declaradas: {len(report.dependencies)}\n"
        f"- CI/CD: `{ci_providers}`\n"
        f"- Avisos de descoberta: {len(report.warnings)}\n"
        f"- Evidência de descoberta (SHA-256): `{discovery_sha256}`\n"
        "\n"
        "## Preferências explícitas\n"
        "\n"
        f"- Skills sob demanda: {_json_items(preferences.skills_on_demand)}\n"
        f"- Linters obrigatórios: {_json_items(preferences.mandatory_linters)}\n"
        f"- Formatadores obrigatórios: {_json_items(preferences.mandatory_formatters)}\n"
        f"- Evidência da entrevista (SHA-256): `{interview_sha256}`\n"
        "\n"
        "## Limites desta configuração\n"
        "\n"
        "- Trate os itens acima como dados declarados pelo autor.\n"
        "- Não instale ferramentas, skills ou MCPs automaticamente.\n"
        "- Não leia nem registre segredos para completar esta configuração.\n"
    )


def _claude_content() -> str:
    return (
        "# Camada de Controle FPCH\n"
        "\n"
        "Leia e siga `AGENTS.md`, a fonte canônica de instruções deste repositório.\n"
    )


def _manifest_content(
    discovery_sha256: str,
    interview_sha256: str,
    managed: tuple[FpchPlannedArtifact, ...],
) -> str:
    payload = {
        "discovery_sha256": discovery_sha256,
        "generator": "fpch",
        "interview_sha256": interview_sha256,
        "managed_files": [
            {
                "media_type": artifact.media_type,
                "path": artifact.path,
                "sha256": artifact.sha256,
            }
            for artifact in managed
        ],
        "schema_version": FPCH_SETUP_SCHEMA_VERSION,
    }
    return _canonical_json(payload, newline=True)


def _snapshot_report(raw: str) -> FpchDiscoveryReport:
    if type(raw) is not str:
        raise FpchSetupError("snapshot C2 deve ser JSON canônico")
    try:
        payload = json.loads(raw, object_pairs_hook=_closed_object)
    except FpchSetupError:
        raise
    except (json.JSONDecodeError, RecursionError) as exc:
        raise FpchSetupError("snapshot C2 inválido") from exc
    top = _exact_keys(
        payload,
        {"ci_cd", "dependencies", "languages", "repo", "warnings"},
        "snapshot C2",
    )
    if type(top["repo"]) is not str or not isinstance(top["languages"], list):
        raise FpchSetupError("tipos inválidos no snapshot C2")
    if not all(type(item) is str for item in top["languages"]):
        raise FpchSetupError("linguagens inválidas no snapshot C2")
    if list(top["languages"]) != sorted(set(top["languages"])):
        raise FpchSetupError("linguagens C2 devem ser únicas e ordenadas")
    if not isinstance(top["dependencies"], list):
        raise FpchSetupError("dependencies inválido no snapshot C2")
    dependencies: list[FpchDependency] = []
    for raw_dependency in top["dependencies"]:
        item = _exact_keys(
            raw_dependency,
            {"ecosystem", "manifest", "name", "scope", "specifier"},
            "dependência C2",
        )
        if not all(type(item[key]) is str for key in item):
            raise FpchSetupError("dependência inválida no snapshot C2")
        dependencies.append(FpchDependency(**item))
    if not isinstance(top["ci_cd"], list):
        raise FpchSetupError("ci_cd inválido no snapshot C2")
    ci_cd: list[FpchCiCdSignal] = []
    for raw_signal in top["ci_cd"]:
        item = _exact_keys(raw_signal, {"path", "provider"}, "CI/CD C2")
        if not all(type(item[key]) is str for key in item):
            raise FpchSetupError("CI/CD inválido no snapshot C2")
        ci_cd.append(FpchCiCdSignal(**item))
    if not isinstance(top["warnings"], list):
        raise FpchSetupError("warnings inválido no snapshot C2")
    warnings: list[FpchDiscoveryWarning] = []
    for raw_warning in top["warnings"]:
        item = _exact_keys(raw_warning, {"code", "message", "path"}, "aviso C2")
        if not all(type(item[key]) is str for key in item):
            raise FpchSetupError("aviso inválido no snapshot C2")
        warnings.append(FpchDiscoveryWarning(**item))
    result = FpchDiscoveryReport(
        repo=top["repo"],
        languages=tuple(top["languages"]),
        dependencies=tuple(dependencies),
        ci_cd=tuple(ci_cd),
        warnings=tuple(warnings),
    )
    if raw != _canonical_json(result.as_dict()):
        raise FpchSetupError("snapshot C2 não é canônico")
    return result


def _snapshot_preferences(raw: str) -> FpchInterviewPreferences:
    if type(raw) is not str:
        raise FpchSetupError("snapshot C3 deve ser JSON canônico")
    try:
        result = load_interview(raw)
    except FpchInterviewError as exc:
        raise FpchSetupError("snapshot C3 inválido") from exc
    if raw != _canonical_json(result.as_dict()):
        raise FpchSetupError("snapshot C3 não é canônico")
    return result


def _desired_artifacts(
    report: FpchDiscoveryReport,
    preferences: FpchInterviewPreferences,
    discovery_sha256: str,
    interview_sha256: str,
) -> tuple[FpchPlannedArtifact, ...]:
    agents = _artifact(
        "AGENTS.md",
        _agents_content(
            report,
            preferences,
            discovery_sha256,
            interview_sha256,
        ),
    )
    claude = _artifact("CLAUDE.md", _claude_content())
    manifest = _artifact(
        ".fpch/setup-manifest.json",
        _manifest_content(discovery_sha256, interview_sha256, (agents, claude)),
    )
    return agents, claude, manifest


def _artifact(
    path: str,
    content: str,
    *,
    state: Literal["create", "unchanged", "conflict"] = "create",
) -> FpchPlannedArtifact:
    return FpchPlannedArtifact(
        path=path,
        media_type=_MEDIA_TYPES[path],
        content=content,
        sha256=_sha256_text(content),
        state=state,
    )


def _conflict_dict(conflict: FpchSetupConflict) -> dict[str, object]:
    return {
        "actual_sha256": conflict.actual_sha256,
        "path": conflict.path,
        "reason": conflict.reason,
    }


def _artifact_dict(artifact: FpchPlannedArtifact) -> dict[str, object]:
    return {
        "content": artifact.content,
        "media_type": artifact.media_type,
        "path": artifact.path,
        "sha256": artifact.sha256,
        "state": artifact.state,
    }


def _logical_plan_dict(value: FpchSetupPlan) -> dict[str, object]:
    return {
        "artifacts": [_artifact_dict(item) for item in value.artifacts],
        "conflicts": [_conflict_dict(item) for item in value.conflicts],
        "discovery_snapshot": json.loads(value.discovery_snapshot),
        "discovery_sha256": value.discovery_sha256,
        "interview_snapshot": json.loads(value.interview_snapshot),
        "interview_sha256": value.interview_sha256,
        "repo": value.repo,
        "repo_identity": asdict(value.repo_identity),
        "schema_version": FPCH_SETUP_SCHEMA_VERSION,
        "unresolved_mcps": list(value.unresolved_mcps),
    }


def _computed_plan_id(value: FpchSetupPlan) -> str:
    return _sha256_text(_canonical_json(_logical_plan_dict(value)))


def _validate_plan(value: FpchSetupPlan) -> None:
    if not isinstance(value, FpchSetupPlan):
        raise FpchSetupError("plan deve ser FpchSetupPlan")
    if type(value.repo) is not str or not value.repo:
        raise FpchSetupError("repo inválido no plano")
    if not _is_sha256(value.discovery_sha256):
        raise FpchSetupError("discovery_sha256 inválido no plano")
    if not _is_sha256(value.interview_sha256):
        raise FpchSetupError("interview_sha256 inválido no plano")
    report = _snapshot_report(value.discovery_snapshot)
    preferences = _snapshot_preferences(value.interview_snapshot)
    if _sha256_text(value.discovery_snapshot) != value.discovery_sha256:
        raise FpchSetupError("discovery_sha256 não corresponde ao snapshot C2")
    if _sha256_text(value.interview_snapshot) != value.interview_sha256:
        raise FpchSetupError("interview_sha256 não corresponde ao snapshot C3")
    if report.repo != value.repo:
        raise FpchSetupError("raiz do snapshot C2 não corresponde ao plano")
    if not isinstance(value.repo_identity, FpchRepoIdentity):
        raise FpchSetupError("identidade de raiz inválida no plano")
    identity_values = (
        value.repo_identity.device,
        value.repo_identity.inode,
        value.repo_identity.mode,
    )
    if not all(type(item) is int and item >= 0 for item in identity_values):
        raise FpchSetupError("campos de identidade de raiz inválidos")
    if type(value.artifacts) is not tuple:
        raise FpchSetupError("artifacts deve ser tupla")
    if not all(isinstance(item, FpchPlannedArtifact) for item in value.artifacts):
        raise FpchSetupError("artefato inválido no plano")
    if tuple(item.path for item in value.artifacts) != _ARTIFACT_PATHS:
        raise FpchSetupError("conjunto ou ordem de artefatos inválido no plano")

    desired = _desired_artifacts(
        report,
        preferences,
        value.discovery_sha256,
        value.interview_sha256,
    )
    conflict_by_path: dict[str, FpchSetupConflict] = {}
    for artifact, regenerated in zip(value.artifacts, desired, strict=True):
        if artifact.media_type != _MEDIA_TYPES[artifact.path]:
            raise FpchSetupError(f"media_type inválido para {artifact.path}")
        if type(artifact.content) is not str:
            raise FpchSetupError(f"conteúdo inválido para {artifact.path}")
        if _sha256_text(artifact.content) != artifact.sha256:
            raise FpchSetupError(f"hash de conteúdo inválido para {artifact.path}")
        if (
            artifact.content != regenerated.content
            or artifact.sha256 != regenerated.sha256
            or artifact.media_type != regenerated.media_type
        ):
            raise FpchSetupError(f"artefato não deriva dos snapshots: {artifact.path}")
        if type(artifact.state) is not str or artifact.state not in _STATES:
            raise FpchSetupError(f"estado inválido para {artifact.path}")

    if type(value.conflicts) is not tuple:
        raise FpchSetupError("conflicts deve ser tupla")
    if not all(isinstance(item, FpchSetupConflict) for item in value.conflicts):
        raise FpchSetupError("conflito inválido no plano")
    for conflict in value.conflicts:
        if conflict.path in conflict_by_path or conflict.path not in _ARTIFACT_PATHS:
            raise FpchSetupError("caminho de conflito inválido ou duplicado")
        if type(conflict.reason) is not str or conflict.reason not in _CONFLICT_REASONS:
            raise FpchSetupError("razão de conflito inválida")
        if conflict.actual_sha256 is not None and not _is_sha256(
            conflict.actual_sha256
        ):
            raise FpchSetupError("hash atual inválido em conflito")
        conflict_by_path[conflict.path] = conflict

    state_conflicts = {
        artifact.path for artifact in value.artifacts if artifact.state == "conflict"
    }
    if state_conflicts != set(conflict_by_path):
        raise FpchSetupError("estados conflict não correspondem à lista de conflitos")
    if type(value.unresolved_mcps) is not tuple or value.unresolved_mcps:
        raise FpchSetupError("C4a não aceita instalações MCP")
    if not _is_sha256(value.plan_id) or value.plan_id != _computed_plan_id(value):
        raise FpchSetupError("plan_id não corresponde ao conteúdo do plano")


def plan(
    report: FpchDiscoveryReport,
    preferences: FpchInterviewPreferences,
) -> FpchSetupPlan:
    """Produz plano determinístico sem escrever ou executar qualquer ferramenta."""
    if not isinstance(report, FpchDiscoveryReport):
        raise FpchSetupError("report deve ser FpchDiscoveryReport")
    if not isinstance(preferences, FpchInterviewPreferences):
        raise FpchSetupError("preferences deve ser FpchInterviewPreferences")
    root = _validated_root(report.repo)
    discovery_snapshot = _canonical_json(report.as_dict())
    interview_snapshot = _canonical_json(preferences.as_dict())
    discovery_sha256 = _sha256_text(discovery_snapshot)
    interview_sha256 = _sha256_text(interview_snapshot)
    desired = _desired_artifacts(
        report,
        preferences,
        discovery_sha256,
        interview_sha256,
    )

    observed: list[FpchPlannedArtifact] = []
    conflicts: list[FpchSetupConflict] = []
    for item in desired:
        state, conflict = _artifact_state(root, item)
        observed.append(
            FpchPlannedArtifact(
                path=item.path,
                media_type=item.media_type,
                content=item.content,
                sha256=item.sha256,
                state=state,  # type: ignore[arg-type]
            )
        )
        if conflict is not None:
            conflicts.append(conflict)

    draft = FpchSetupPlan(
        repo=root.as_posix(),
        discovery_sha256=discovery_sha256,
        interview_sha256=interview_sha256,
        discovery_snapshot=discovery_snapshot,
        interview_snapshot=interview_snapshot,
        repo_identity=_root_identity(root),
        artifacts=tuple(observed),
        conflicts=tuple(conflicts),
        unresolved_mcps=(),
        plan_id="",
    )
    result = FpchSetupPlan(
        repo=draft.repo,
        discovery_sha256=draft.discovery_sha256,
        interview_sha256=draft.interview_sha256,
        discovery_snapshot=draft.discovery_snapshot,
        interview_snapshot=draft.interview_snapshot,
        repo_identity=draft.repo_identity,
        artifacts=draft.artifacts,
        conflicts=draft.conflicts,
        unresolved_mcps=draft.unresolved_mcps,
        plan_id=_computed_plan_id(draft),
    )
    _validate_plan(result)
    return result


def dumps(value: FpchSetupPlan) -> str:
    """Serializa um plano validado em JSON canônico terminado por newline."""
    _validate_plan(value)
    payload = _logical_plan_dict(value)
    payload["plan_id"] = value.plan_id
    return _canonical_json(payload, newline=True)


def _closed_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise FpchSetupError("JSON contém chave duplicada")
        result[key] = value
    return result


def _exact_keys(value: object, expected: set[str], label: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise FpchSetupError(f"{label} deve ser objeto JSON")
    if set(value) != expected:
        raise FpchSetupError(f"campos inválidos em {label}")
    return value


def loads(raw: str | bytes) -> FpchSetupPlan:
    """Carrega somente o esquema fechado de plano C4a."""
    if isinstance(raw, bytes):
        if len(raw) > FPCH_SETUP_MAX_BYTES:
            raise FpchSetupError("plano excede 1 MiB")
        try:
            text = raw.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise FpchSetupError("plano não é UTF-8 válido") from exc
    elif type(raw) is str:
        try:
            raw_size = len(raw.encode("utf-8"))
        except UnicodeEncodeError as exc:
            raise FpchSetupError("plano contém Unicode inválido") from exc
        if raw_size > FPCH_SETUP_MAX_BYTES:
            raise FpchSetupError("plano excede 1 MiB")
        text = raw
    else:
        raise FpchSetupError("plano deve ser str ou bytes")
    try:
        payload = json.loads(text, object_pairs_hook=_closed_object)
    except FpchSetupError:
        raise
    except (json.JSONDecodeError, RecursionError) as exc:
        raise FpchSetupError(f"JSON de plano inválido: {exc}") from exc

    top = _exact_keys(
        payload,
        {
            "artifacts",
            "conflicts",
            "discovery_snapshot",
            "discovery_sha256",
            "interview_snapshot",
            "interview_sha256",
            "plan_id",
            "repo",
            "repo_identity",
            "schema_version",
            "unresolved_mcps",
        },
        "plano",
    )
    if type(top["schema_version"]) is not int or top["schema_version"] != 1:
        raise FpchSetupError("schema_version de plano não suportada")
    if not isinstance(top["artifacts"], list):
        raise FpchSetupError("artifacts deve ser lista JSON")
    artifacts: list[FpchPlannedArtifact] = []
    for raw_artifact in top["artifacts"]:
        item = _exact_keys(
            raw_artifact,
            {"content", "media_type", "path", "sha256", "state"},
            "artefato",
        )
        artifacts.append(
            FpchPlannedArtifact(
                path=item["path"],
                media_type=item["media_type"],
                content=item["content"],
                sha256=item["sha256"],
                state=item["state"],
            )
        )
    if not isinstance(top["conflicts"], list):
        raise FpchSetupError("conflicts deve ser lista JSON")
    conflicts: list[FpchSetupConflict] = []
    for raw_conflict in top["conflicts"]:
        item = _exact_keys(
            raw_conflict,
            {"actual_sha256", "path", "reason"},
            "conflito",
        )
        conflicts.append(
            FpchSetupConflict(
                path=item["path"],
                reason=item["reason"],
                actual_sha256=item["actual_sha256"],
            )
        )
    if not isinstance(top["unresolved_mcps"], list):
        raise FpchSetupError("unresolved_mcps deve ser lista JSON")
    identity_payload = _exact_keys(
        top["repo_identity"],
        {"device", "inode", "mode"},
        "identidade da raiz",
    )
    result = FpchSetupPlan(
        repo=top["repo"],
        discovery_sha256=top["discovery_sha256"],
        interview_sha256=top["interview_sha256"],
        discovery_snapshot=_canonical_json(top["discovery_snapshot"]),
        interview_snapshot=_canonical_json(top["interview_snapshot"]),
        repo_identity=FpchRepoIdentity(
            device=identity_payload["device"],
            inode=identity_payload["inode"],
            mode=identity_payload["mode"],
        ),
        artifacts=tuple(artifacts),
        conflicts=tuple(conflicts),
        unresolved_mcps=tuple(top["unresolved_mcps"]),
        plan_id=top["plan_id"],
    )
    _validate_plan(result)
    return result


def _same_file(first: os.stat_result, second: os.stat_result) -> bool:
    try:
        return os.path.samestat(first, second)
    except (AttributeError, OSError):
        return (first.st_dev, first.st_ino) == (second.st_dev, second.st_ino)


def _remove_owned(path: Path, created: os.stat_result, sha256: str | None) -> bool:
    try:
        current = path.lstat()
    except OSError:
        return False
    if not _same_file(created, current):
        return False
    if sha256 is not None:
        try:
            if _file_sha256(path) != sha256:
                return False
        except FpchSetupError:
            return False
    try:
        path.unlink()
    except OSError:
        return False
    return True


def _create_exclusive(path: Path, content: str) -> os.stat_result:
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    flags |= getattr(os, "O_BINARY", 0)
    flags |= getattr(os, "O_NOFOLLOW", 0)
    descriptor: int | None = None
    created: os.stat_result | None = None
    try:
        descriptor = os.open(path, flags, 0o600)
        created = os.fstat(descriptor)
        payload = content.encode("utf-8")
        offset = 0
        while offset < len(payload):
            written = os.write(descriptor, payload[offset:])
            if written <= 0:
                raise OSError("escrita não progrediu")
            offset += written
        os.fsync(descriptor)
        os.close(descriptor)
        descriptor = None
        return created
    except (OSError, UnicodeError) as exc:
        if descriptor is not None:
            try:
                os.close(descriptor)
            except OSError:
                pass
        if created is not None:
            _remove_owned(path, created, None)
        raise FpchSetupError(f"não foi possível criar {path}: {exc}") from exc


def _write_all(descriptor: int, payload: bytes) -> None:
    offset = 0
    while offset < len(payload):
        written = os.write(descriptor, payload[offset:])
        if written <= 0:
            raise OSError("escrita não progrediu")
        offset += written


def _journal_payload(
    value: FpchSetupPlan,
    *,
    state: str,
    planned: tuple[FpchPlannedArtifact, ...],
    created: list[dict[str, object]],
    in_progress: str | None,
    created_dirs: list[dict[str, object]],
) -> dict[str, object]:
    return {
        "created": created,
        "created_dirs": created_dirs,
        "in_progress": in_progress,
        "plan_id": value.plan_id,
        "planned": [
            {"path": artifact.path, "sha256": artifact.sha256}
            for artifact in planned
            if artifact.state == "create"
        ],
        "repo": value.repo,
        "repo_identity": asdict(value.repo_identity),
        "schema_version": 1,
        "state": state,
        "transaction_id": value.plan_id,
    }


def _create_journal(
    directory: Path, payload: dict[str, object]
) -> tuple[Path, int, os.stat_result]:
    path = directory / _JOURNAL_NAME
    flags = os.O_RDWR | os.O_CREAT | os.O_EXCL
    flags |= getattr(os, "O_BINARY", 0)
    flags |= getattr(os, "O_NOFOLLOW", 0)
    descriptor: int | None = None
    created: os.stat_result | None = None
    try:
        descriptor = os.open(path, flags, 0o600)
        created = os.fstat(descriptor)
        _write_all(descriptor, _canonical_json(payload, newline=True).encode("utf-8"))
        os.fsync(descriptor)
        return path, descriptor, created
    except OSError as exc:
        if descriptor is not None:
            try:
                os.close(descriptor)
            except OSError:
                pass
        if created is not None:
            _remove_owned(path, created, None)
        raise FpchSetupError(f"não foi possível criar journal durável: {exc}") from exc


def _update_journal(descriptor: int, payload: dict[str, object]) -> None:
    try:
        encoded = _canonical_json(payload, newline=True).encode("utf-8")
        os.lseek(descriptor, 0, os.SEEK_SET)
        os.ftruncate(descriptor, 0)
        _write_all(descriptor, encoded)
        os.fsync(descriptor)
    except OSError as exc:
        raise FpchSetupError(f"não foi possível atualizar journal durável: {exc}") from exc


def _read_control_json(path: Path) -> dict[str, Any]:
    if _is_linklike(path):
        raise FpchSetupError(f"arquivo de controle inseguro: {path}")
    try:
        size = path.lstat().st_size
    except OSError as exc:
        raise FpchSetupError(f"não foi possível inspecionar controle: {path}") from exc
    if size > FPCH_SETUP_MAX_BYTES:
        raise FpchSetupError(f"arquivo de controle excede limite: {path}")
    flags = os.O_RDONLY | getattr(os, "O_BINARY", 0)
    flags |= getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags)
        with os.fdopen(descriptor, "rb") as stream:
            raw = stream.read(FPCH_SETUP_MAX_BYTES + 1)
    except OSError as exc:
        raise FpchSetupError(f"não foi possível ler controle: {path}") from exc
    try:
        value = json.loads(raw.decode("utf-8"), object_pairs_hook=_closed_object)
    except (UnicodeError, json.JSONDecodeError, FpchSetupError) as exc:
        raise FpchSetupError(f"arquivo de controle inválido: {path.name}") from exc
    if not isinstance(value, dict):
        raise FpchSetupError(f"arquivo de controle inválido: {path.name}")
    return value


def _pid_alive(pid: object) -> bool:
    if type(pid) is not int or pid <= 0:
        return False
    if pid == os.getpid():
        return True
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False
    return True


def _rename_then_unlink(path: Path, destination: Path) -> None:
    if _lstat(destination) is not None:
        raise FpchSetupError(f"quarentena já existe: {destination}")
    try:
        os.rename(path, destination)
        destination.unlink()
    except OSError as exc:
        raise FpchSetupError(f"não foi possível remover controle obsoleto: {path}") from exc


def _ensure_fpch_dir(root: Path) -> tuple[Path, os.stat_result | None]:
    directory = root / ".fpch"
    metadata = _lstat(directory)
    created: os.stat_result | None = None
    if metadata is None:
        try:
            os.mkdir(directory, 0o700)
            created = directory.lstat()
            metadata = created
        except FileExistsError:
            metadata = _lstat(directory)
        except OSError as exc:
            raise FpchSetupError(f"não foi possível criar {directory}: {exc}") from exc
    if metadata is None or _is_linklike(directory) or not stat.S_ISDIR(
        metadata.st_mode
    ):
        raise FpchSetupError(f"diretório de controle inseguro: {directory}")
    return directory, created


def _acquire_lock(directory: Path, plan_id: str) -> tuple[Path, os.stat_result]:
    path = directory / _LOCK_NAME
    content = _canonical_json(
        {"pid": os.getpid(), "plan_id": plan_id, "schema_version": 1},
        newline=True,
    )
    try:
        created = _create_exclusive(path, content)
    except FpchSetupError as first_error:
        metadata = _lstat(path)
        if metadata is None or _is_linklike(path) or not stat.S_ISREG(metadata.st_mode):
            raise FpchSetupError(
                "lock existente é inseguro; recovery explícito exigido"
            ) from first_error
        try:
            lock = _read_control_json(path)
        except FpchSetupError:
            lock = {}
        if _pid_alive(lock.get("pid")):
            raise FpchSetupError("outro setup está ativo") from first_error
        stale = directory / f".{_LOCK_NAME}.stale-{plan_id[:16]}"
        _rename_then_unlink(path, stale)
        try:
            created = _create_exclusive(path, content)
        except FpchSetupError as exc:
            raise FpchSetupError("não foi possível substituir lock órfão") from exc
    return path, created


def _inverse(
    value: FpchSetupPlan,
    created: list[tuple[FpchPlannedArtifact, Path, os.stat_result]],
    directory_created: os.stat_result | None,
) -> str:
    return _canonical_json(
        {
            "files": [
                {
                    "mode": stat.S_IMODE(metadata.st_mode),
                    "order": order,
                    "path": artifact.path,
                    "sha256": artifact.sha256,
                }
                for order, (artifact, _path, metadata) in enumerate(
                    reversed(created)
                )
            ],
            "created_dirs": (
                [{"mode": stat.S_IMODE(directory_created.st_mode), "path": ".fpch"}]
                if directory_created is not None
                else []
            ),
            "operation": "delete_created_files",
            "plan_id": value.plan_id,
            "repo": value.repo,
            "repo_identity": asdict(value.repo_identity),
            "schema_version": 1,
            "transaction_id": value.plan_id,
        }
    )


def _rollback(
    created: list[tuple[FpchPlannedArtifact, Path, os.stat_result]],
    directory: Path,
    transaction_id: str,
) -> tuple[str, ...]:
    failures: list[str] = []
    quarantine = directory / f".rollback-{transaction_id[:16]}"
    try:
        os.mkdir(quarantine, 0o700)
    except OSError:
        return tuple(artifact.path for artifact, _path, _metadata in reversed(created))
    quarantined: list[Path] = []
    for index, (artifact, path, metadata) in enumerate(reversed(created)):
        destination = quarantine / f"{index:04d}.file"
        try:
            os.rename(path, destination)
            moved = destination.lstat()
            if not _same_file(metadata, moved):
                raise FpchSetupError("identidade mudou antes da quarentena")
            if _file_sha256(
                destination, max_bytes=FPCH_SETUP_EXISTING_MAX_BYTES
            ) != artifact.sha256:
                raise FpchSetupError("conteúdo mudou antes da quarentena")
            quarantined.append(destination)
        except (OSError, FpchSetupError):
            if _lstat(destination) is not None and _lstat(path) is None:
                try:
                    os.rename(destination, path)
                except OSError:
                    pass
            failures.append(artifact.path)
    for path in quarantined:
        try:
            path.unlink()
        except OSError:
            failures.append(path.name)
    try:
        quarantine.rmdir()
    except OSError:
        failures.append(quarantine.name)
    return tuple(failures)


def _created_record(
    artifact: FpchPlannedArtifact,
    metadata: os.stat_result,
    order: int,
) -> dict[str, object]:
    return {
        "device": int(metadata.st_dev),
        "inode": int(metadata.st_ino),
        "mode": int(stat.S_IMODE(metadata.st_mode)),
        "order": order,
        "path": artifact.path,
        "sha256": artifact.sha256,
    }


def _validate_journal(
    raw: dict[str, Any], root: Path
) -> tuple[str, list[dict[str, Any]], dict[str, str], str | None]:
    value = _exact_keys(
        raw,
        {
            "created",
            "created_dirs",
            "in_progress",
            "plan_id",
            "planned",
            "repo",
            "repo_identity",
            "schema_version",
            "state",
            "transaction_id",
        },
        "journal",
    )
    if type(value["schema_version"]) is not int or value["schema_version"] != 1:
        raise FpchSetupError("journal usa schema não suportado")
    if value["state"] not in _JOURNAL_STATES:
        raise FpchSetupError("journal tem estado inválido")
    if value["repo"] != root.as_posix():
        raise FpchSetupError("journal pertence a outro repositório")
    raw_identity = _exact_keys(
        value["repo_identity"], {"device", "inode", "mode"}, "raiz do journal"
    )
    journal_identity = FpchRepoIdentity(
        raw_identity["device"], raw_identity["inode"], raw_identity["mode"]
    )
    if journal_identity != _root_identity(root):
        raise FpchSetupError("journal pertence a outra identidade de raiz")
    if not _is_sha256(value["plan_id"]) or value["transaction_id"] != value["plan_id"]:
        raise FpchSetupError("identidade de transação inválida no journal")
    if not isinstance(value["planned"], list):
        raise FpchSetupError("planned inválido no journal")
    planned: dict[str, str] = {}
    for raw_item in value["planned"]:
        item = _exact_keys(raw_item, {"path", "sha256"}, "planned do journal")
        if item["path"] not in _ARTIFACT_PATHS or not _is_sha256(item["sha256"]):
            raise FpchSetupError("artefato planejado inválido no journal")
        if item["path"] in planned:
            raise FpchSetupError("artefato duplicado no journal")
        planned[item["path"]] = item["sha256"]
    if not isinstance(value["created"], list):
        raise FpchSetupError("created inválido no journal")
    created: list[dict[str, Any]] = []
    seen: set[str] = set()
    for raw_item in value["created"]:
        item = _exact_keys(
            raw_item,
            {"device", "inode", "mode", "order", "path", "sha256"},
            "created do journal",
        )
        if (
            item["path"] not in planned
            or item["path"] in seen
            or item["sha256"] != planned[item["path"]]
            or not all(
                type(item[key]) is int and item[key] >= 0
                for key in ("device", "inode", "mode", "order")
            )
        ):
            raise FpchSetupError("artefato criado inválido no journal")
        seen.add(item["path"])
        created.append(item)
    if not isinstance(value["created_dirs"], list):
        raise FpchSetupError("created_dirs inválido no journal")
    for raw_item in value["created_dirs"]:
        item = _exact_keys(raw_item, {"mode", "path"}, "diretório do journal")
        if item["path"] != ".fpch" or type(item["mode"]) is not int:
            raise FpchSetupError("diretório criado inválido no journal")
    in_progress = value["in_progress"]
    if in_progress is not None and in_progress not in planned:
        raise FpchSetupError("in_progress inválido no journal")
    return value["state"], created, planned, in_progress


def _recover_existing_journal(root: Path, directory: Path) -> None:
    journal = directory / _JOURNAL_NAME
    if _lstat(journal) is None:
        return
    raw = _read_control_json(journal)
    state, records, planned, in_progress = _validate_journal(raw, root)
    if state in ("auditing", "recovery_required"):
        raise FpchSetupError(
            "journal exige recovery explícito: resultado de auditoria pode ser ambíguo"
        )
    if state == "committed":
        for record in records:
            path = _target(root, record["path"])
            metadata = _lstat(path)
            expected_identity = FpchRepoIdentity(
                record["device"], record["inode"], record["mode"]
            )
            if (
                metadata is None
                or _identity(metadata) != expected_identity
                or _file_sha256(
                    path, max_bytes=FPCH_SETUP_EXISTING_MAX_BYTES
                )
                != record["sha256"]
            ):
                raise FpchSetupError("transação committed sofreu drift; recovery explícito")
        _rename_then_unlink(
            journal, directory / f".{_JOURNAL_NAME}.done-{raw['plan_id'][:16]}"
        )
        return

    recovered: list[tuple[FpchPlannedArtifact, Path, os.stat_result]] = []
    for record in sorted(records, key=lambda item: item["order"]):
        path = _target(root, record["path"])
        metadata = _lstat(path)
        if metadata is None:
            continue
        expected_identity = FpchRepoIdentity(
            record["device"], record["inode"], record["mode"]
        )
        if _identity(metadata) != expected_identity:
            raise FpchSetupError("arquivo de recovery mudou de identidade")
        recovered.append(
            (
                FpchPlannedArtifact(
                    record["path"],
                    _MEDIA_TYPES[record["path"]],
                    "",
                    record["sha256"],
                ),
                path,
                metadata,
            )
        )
    if in_progress is not None and in_progress not in {item[0].path for item in recovered}:
        path = _target(root, in_progress)
        metadata = _lstat(path)
        if metadata is not None:
            if _file_sha256(path, max_bytes=FPCH_SETUP_EXISTING_MAX_BYTES) != planned[
                in_progress
            ]:
                raise FpchSetupError("arquivo in_progress sofreu drift; recovery explícito")
            recovered.append(
                (
                    FpchPlannedArtifact(
                        in_progress,
                        _MEDIA_TYPES[in_progress],
                        "",
                        planned[in_progress],
                    ),
                    path,
                    metadata,
                )
            )
    failures = _rollback(recovered, directory, raw["transaction_id"])
    if failures:
        raise FpchSetupError("recovery automático ficou incompleto")
    _rename_then_unlink(
        journal, directory / f".{_JOURNAL_NAME}.recovered-{raw['plan_id'][:16]}"
    )


def _current_states(
    root: Path, value: FpchSetupPlan
) -> tuple[tuple[str, ...], tuple[str, ...], tuple[str, ...]]:
    created: list[str] = []
    unchanged: list[str] = []
    conflicts: list[str] = []
    for artifact in value.artifacts:
        state, _conflict = _artifact_state(root, artifact)
        if state != artifact.state:
            raise FpchSetupError(
                f"plano obsoleto: estado de {artifact.path} mudou "
                f"de {artifact.state} para {state}"
            )
        if state == "create":
            created.append(artifact.path)
        elif state == "unchanged":
            unchanged.append(artifact.path)
        else:
            conflicts.append(artifact.path)
    return tuple(created), tuple(unchanged), tuple(conflicts)


def _assert_discovery_current(root: Path, value: FpchSetupPlan) -> None:
    _assert_root_identity(root, value.repo_identity)
    current = discover(root)
    if _canonical_json(current.as_dict()) != value.discovery_snapshot:
        raise FpchSetupError("plano obsoleto: descoberta C2 atual diverge do snapshot")


def _ancestor_identities(root: Path, relative: str) -> tuple[tuple[Path, os.stat_result], ...]:
    result: list[tuple[Path, os.stat_result]] = []
    current = root
    for part in relative.split("/")[:-1]:
        metadata = _lstat(current)
        if metadata is None or _is_linklike(current) or not stat.S_ISDIR(metadata.st_mode):
            raise FpchSetupError(f"ancestral inseguro para {relative}")
        result.append((current, metadata))
        current = current / part
    metadata = _lstat(current)
    if metadata is None or _is_linklike(current) or not stat.S_ISDIR(metadata.st_mode):
        raise FpchSetupError(f"ancestral inseguro para {relative}")
    result.append((current, metadata))
    return tuple(result)


def _assert_ancestors_unchanged(
    snapshot: tuple[tuple[Path, os.stat_result], ...]
) -> None:
    for path, expected in snapshot:
        current = _lstat(path)
        if (
            current is None
            or _is_linklike(path)
            or not stat.S_ISDIR(current.st_mode)
            or not _same_file(expected, current)
        ):
            raise FpchSetupError(f"ancestral mudou durante a criação: {path}")


def _verify_installed(
    root: Path,
    value: FpchSetupPlan,
    created: list[tuple[FpchPlannedArtifact, Path, os.stat_result]],
) -> None:
    _assert_discovery_current(root, value)
    created_by_path = {artifact.path: metadata for artifact, _path, metadata in created}
    for artifact in value.artifacts:
        state, _conflict = _artifact_state(root, artifact)
        if state != "unchanged":
            raise FpchSetupError(f"artefato sofreu drift após criação: {artifact.path}")
        if artifact.path in created_by_path:
            metadata = _lstat(_target(root, artifact.path))
            if metadata is None or not _same_file(created_by_path[artifact.path], metadata):
                raise FpchSetupError(
                    f"identidade do artefato mudou após criação: {artifact.path}"
                )


def apply(
    value: FpchSetupPlan,
    confirmation: str,
    trilha: str | Path | None = None,
) -> FpchSetupResult:
    """Aplica um plano create-only após confirmação literal do ``plan_id``.

    Todos os estados são revalidados sob lock. Em qualquer falha anterior à
    auditoria, somente arquivos ainda idênticos aos criados nesta transação são
    removidos, em ordem LIFO. Falha ao gravar o evento ``install`` também provoca
    rollback e torna a operação um erro explícito.
    """
    _validate_plan(value)
    if type(confirmation) is not str or confirmation != value.plan_id:
        raise FpchSetupError("confirmação deve ser exatamente o plan_id")
    root = _validated_root(value.repo)
    _assert_discovery_current(root, value)

    if value.conflicts:
        conflict_paths = tuple(item.path for item in value.conflicts)
        return FpchSetupResult(
            ok=False,
            plan_id=value.plan_id,
            created=(),
            unchanged=tuple(
                item.path for item in value.artifacts if item.state == "unchanged"
            ),
            conflict=conflict_paths,
            audit_written=False,
        )

    directory: Path | None = None
    directory_created: os.stat_result | None = None
    lock_path: Path | None = None
    lock_created: os.stat_result | None = None
    journal_path: Path | None = None
    journal_descriptor: int | None = None
    journal_payload: dict[str, object] | None = None
    created: list[tuple[FpchPlannedArtifact, Path, os.stat_result]] = []
    success = False
    try:
        directory, directory_created = _ensure_fpch_dir(root)
        lock_path, lock_created = _acquire_lock(directory, value.plan_id)
        _recover_existing_journal(root, directory)
        _assert_discovery_current(root, value)
        create_paths, unchanged_paths, conflict_paths = _current_states(root, value)
        if conflict_paths:
            raise FpchSetupError("plano contém conflito não declarado")

        if create_paths:
            created_dirs = (
                [
                    {
                        "mode": stat.S_IMODE(directory_created.st_mode),
                        "path": ".fpch",
                    }
                ]
                if directory_created is not None
                else []
            )
            journal_payload = _journal_payload(
                value,
                state="prepared",
                planned=value.artifacts,
                created=[],
                in_progress=None,
                created_dirs=created_dirs,
            )
            journal_path, journal_descriptor, _journal_metadata = _create_journal(
                directory, journal_payload
            )

        for artifact in value.artifacts:
            if artifact.path not in create_paths:
                continue
            target = _target(root, artifact.path)
            if _path_problem(root, artifact.path) is not None:
                raise FpchSetupError(f"alvo tornou-se inseguro: {artifact.path}")
            if journal_payload is None or journal_descriptor is None:
                raise FpchSetupError("journal ausente antes da primeira criação")
            journal_payload["state"] = "applying"
            journal_payload["in_progress"] = artifact.path
            _update_journal(journal_descriptor, journal_payload)
            ancestors = _ancestor_identities(root, artifact.path)
            metadata = _create_exclusive(target, artifact.content)
            created.append((artifact, target, metadata))
            _assert_ancestors_unchanged(ancestors)
            journal_payload["created"] = [
                _created_record(item, item_metadata, order)
                for order, (item, _path, item_metadata) in enumerate(created)
            ]
            journal_payload["in_progress"] = None
            _update_journal(journal_descriptor, journal_payload)

        audit_written = False
        if created:
            _verify_installed(root, value, created)
            if journal_payload is None or journal_descriptor is None:
                raise FpchSetupError("journal desapareceu antes da auditoria")
            journal_payload["state"] = "auditing"
            _update_journal(journal_descriptor, journal_payload)
            try:
                audit_written = audit.write(
                    audit.Event(
                        event="install",
                        trajectory_id=value.plan_id,
                        seq=0,
                        label="fpch setup apply",
                        source="nlah",
                        source_ref=value.plan_id,
                        inverse=_inverse(value, created, directory_created),
                    ),
                    None if trilha is None else Path(trilha),
                )
            except Exception as exc:
                raise FpchSetupError(
                    f"falha ao gravar auditoria de instalação: {exc}"
                ) from exc
            if not audit_written:
                raise FpchSetupError("falha ao gravar auditoria de instalação")
            _verify_installed(root, value, created)
            journal_payload["state"] = "committed"
            _update_journal(journal_descriptor, journal_payload)
            os.close(journal_descriptor)
            journal_descriptor = None
            if journal_path is None:
                raise FpchSetupError("journal desapareceu após commit")
            _rename_then_unlink(
                journal_path,
                directory / f".{_JOURNAL_NAME}.done-{value.plan_id[:16]}",
            )
            journal_path = None

        success = True
        return FpchSetupResult(
            ok=True,
            plan_id=value.plan_id,
            created=tuple(item.path for item, _path, _metadata in created),
            unchanged=unchanged_paths,
            conflict=(),
            audit_written=audit_written,
        )
    except Exception as exc:
        rollback_failures: tuple[str, ...] = ()
        if created and directory is not None:
            rollback_failures = _rollback(created, directory, value.plan_id)
        if journal_payload is not None and journal_descriptor is not None:
            journal_payload["in_progress"] = None
            journal_payload["state"] = (
                "recovery_required" if rollback_failures else "applying"
            )
            try:
                _update_journal(journal_descriptor, journal_payload)
            except FpchSetupError:
                rollback_failures = (*rollback_failures, _JOURNAL_NAME)
            try:
                os.close(journal_descriptor)
            except OSError:
                rollback_failures = (*rollback_failures, _JOURNAL_NAME)
            journal_descriptor = None
        if not rollback_failures and journal_path is not None and directory is not None:
            try:
                _rename_then_unlink(
                    journal_path,
                    directory / f".{_JOURNAL_NAME}.aborted-{value.plan_id[:16]}",
                )
                journal_path = None
            except FpchSetupError:
                rollback_failures = (_JOURNAL_NAME,)
        if rollback_failures:
            names = ", ".join(rollback_failures)
            raise FpchSetupError(
                f"setup falhou e rollback ficou incompleto: {names}"
            ) from exc
        if isinstance(exc, FpchSetupError):
            raise
        raise FpchSetupError(f"setup falhou: {exc}") from exc
    finally:
        if journal_descriptor is not None:
            try:
                os.close(journal_descriptor)
            except OSError:
                pass
        if lock_path is not None and lock_created is not None:
            _remove_owned(lock_path, lock_created, None)
        if not success and directory is not None and directory_created is not None:
            try:
                current = directory.lstat()
                if _same_file(directory_created, current):
                    directory.rmdir()
            except OSError:
                pass
