"""Descoberta determinística e somente leitura de sinais de um repositório.

O módulo deliberadamente não tenta inferir arquitetura pelo conteúdo do código:
ele lê apenas nomes/extensões, manifestos conhecidos e caminhos convencionais de
CI/CD.  Não consulta rede e não escreve no repositório inspecionado.
"""

from __future__ import annotations

import json
import os
import re
import stat
import tomllib
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any


_IGNORED_DIRS = frozenset(
    {
        ".git",
        ".hg",
        ".mypy_cache",
        ".nox",
        ".pytest_cache",
        ".ruff_cache",
        ".svn",
        ".tox",
        ".venv",
        "__pycache__",
        "build",
        "dist",
        "node_modules",
        "target",
        "vendor",
        "venv",
    }
)

_LANGUAGE_BY_SUFFIX = {
    ".c": "C",
    ".cc": "C++",
    ".cpp": "C++",
    ".cs": "C#",
    ".go": "Go",
    ".java": "Java",
    ".js": "JavaScript",
    ".jsx": "JavaScript",
    ".kt": "Kotlin",
    ".kts": "Kotlin",
    ".php": "PHP",
    ".py": "Python",
    ".rb": "Ruby",
    ".rs": "Rust",
    ".swift": "Swift",
    ".ts": "TypeScript",
    ".tsx": "TypeScript",
}

_REQUIREMENT_NAME = re.compile(r"^\s*([A-Za-z0-9][A-Za-z0-9._-]*)")

# O limite impede que um arquivo com nome de manifesto domine memória/tempo de
# descoberta. Os demais sinais do repositório continuam sendo reportados.
FPCH_MANIFEST_MAX_BYTES = 1024 * 1024


class FpchDiscoveryError(ValueError):
    """O alvo não pôde ser inspecionado como diretório de repositório."""


@dataclass(frozen=True)
class FpchDependency:
    ecosystem: str
    name: str
    specifier: str
    scope: str
    manifest: str


@dataclass(frozen=True)
class FpchCiCdSignal:
    provider: str
    path: str


@dataclass(frozen=True)
class FpchDiscoveryWarning:
    code: str
    path: str
    message: str


@dataclass(frozen=True)
class FpchDiscoveryReport:
    repo: str
    languages: tuple[str, ...]
    dependencies: tuple[FpchDependency, ...]
    ci_cd: tuple[FpchCiCdSignal, ...]
    warnings: tuple[FpchDiscoveryWarning, ...]

    def as_dict(self) -> dict[str, Any]:
        """Converte o relatório em primitivas prontas para JSON."""
        return {
            "repo": self.repo,
            "languages": list(self.languages),
            "dependencies": [asdict(item) for item in self.dependencies],
            "ci_cd": [asdict(item) for item in self.ci_cd],
            "warnings": [asdict(item) for item in self.warnings],
        }


def _relative(path: Path, root: Path) -> str:
    return path.relative_to(root).as_posix()


def _ignored_dir(name: str) -> bool:
    return name.startswith(".") or name.casefold() in _IGNORED_DIRS


def _is_linklike(path: Path) -> bool:
    """Reconhece links e reparse points sem seguir o destino."""
    try:
        metadata = path.lstat()
    except OSError:
        return False

    if stat.S_ISLNK(metadata.st_mode):
        return True

    # No Windows, junctions e outros reparse points nem sempre aparecem como
    # symlinks para pathlib. Ignorar todo reparse point mantém o contrato simples.
    file_attributes = getattr(metadata, "st_file_attributes", 0)
    reparse_flag = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0)
    if reparse_flag and file_attributes & reparse_flag:
        return True

    isjunction = getattr(os.path, "isjunction", None)
    return bool(isjunction is not None and isjunction(path))


def _is_regular_file(path: Path) -> bool:
    try:
        return not _is_linklike(path) and stat.S_ISREG(path.lstat().st_mode)
    except OSError:
        return False


def _repository_files(
    root: Path, warnings: list[FpchDiscoveryWarning]
) -> list[Path]:
    files: list[Path] = []

    def onerror(error: OSError) -> None:
        filename = Path(error.filename) if error.filename else root
        try:
            path = _relative(filename, root)
        except ValueError:
            path = "."
        warnings.append(
            FpchDiscoveryWarning(
                "path_unreadable",
                path,
                "diretório não pôde ser lido",
            )
        )

    for current, dirnames, filenames in os.walk(
        root, topdown=True, onerror=onerror, followlinks=False
    ):
        base = Path(current)
        dirnames[:] = sorted(
            name
            for name in dirnames
            if not _ignored_dir(name) and not _is_linklike(base / name)
        )
        files.extend(
            path
            for name in sorted(filenames)
            if _is_regular_file(path := base / name)
        )
    return files


def _read_text(
    path: Path, root: Path, warnings: list[FpchDiscoveryWarning]
) -> str | None:
    if _is_linklike(path):
        return None

    try:
        with path.open("rb") as manifest_file:
            raw = manifest_file.read(FPCH_MANIFEST_MAX_BYTES + 1)
    except OSError:
        warnings.append(
            FpchDiscoveryWarning(
                "manifest_unreadable",
                _relative(path, root),
                "manifesto não pôde ser lido como UTF-8",
            )
        )
        return None

    if len(raw) > FPCH_MANIFEST_MAX_BYTES:
        warnings.append(
            FpchDiscoveryWarning(
                "manifest_too_large",
                _relative(path, root),
                (
                    "manifesto excede o limite de "
                    f"{FPCH_MANIFEST_MAX_BYTES} bytes; conteúdo ignorado"
                ),
            )
        )
        return None

    try:
        return raw.decode("utf-8")
    except UnicodeError:
        warnings.append(
            FpchDiscoveryWarning(
                "manifest_unreadable",
                _relative(path, root),
                "manifesto não pôde ser lido como UTF-8",
            )
        )
        return None


def _warning_malformed(
    path: Path, root: Path, warnings: list[FpchDiscoveryWarning]
) -> None:
    warnings.append(
        FpchDiscoveryWarning(
            "manifest_malformed",
            _relative(path, root),
            "manifesto malformado; dependências deste arquivo foram ignoradas",
        )
    )


def _python_dependency(raw: str, scope: str, manifest: str) -> FpchDependency | None:
    match = _REQUIREMENT_NAME.match(raw)
    if match is None:
        return None
    name = match.group(1)
    return FpchDependency("python", name, raw[len(name):].strip(), scope, manifest)


def _parse_pyproject(
    path: Path, root: Path, warnings: list[FpchDiscoveryWarning]
) -> list[FpchDependency]:
    text = _read_text(path, root, warnings)
    if text is None:
        return []
    try:
        data = tomllib.loads(text)
        project = data.get("project", {})
        if not isinstance(project, dict):
            raise TypeError
        runtime = project.get("dependencies", [])
        optional = project.get("optional-dependencies", {})
        dependency_groups = data.get("dependency-groups", {})
        if not isinstance(runtime, list) or not all(isinstance(item, str) for item in runtime):
            raise TypeError
        if not isinstance(optional, dict):
            raise TypeError
        if not isinstance(dependency_groups, dict):
            raise TypeError
        manifest = _relative(path, root)
        found = [
            dep
            for raw in runtime
            if (dep := _python_dependency(raw, "runtime", manifest)) is not None
        ]
        for group, values in sorted(optional.items()):
            if not isinstance(group, str) or not isinstance(values, list) or not all(
                isinstance(item, str) for item in values
            ):
                raise TypeError
            found.extend(
                dep
                for raw in values
                if (dep := _python_dependency(raw, f"optional:{group}", manifest)) is not None
            )
        for group, values in sorted(dependency_groups.items()):
            if not isinstance(group, str) or not isinstance(values, list):
                raise TypeError
            for entry in values:
                if isinstance(entry, str):
                    dependency = _python_dependency(
                        entry, f"group:{group}", manifest
                    )
                    if dependency is not None:
                        found.append(dependency)
                elif (
                    isinstance(entry, dict)
                    and set(entry) == {"include-group"}
                    and isinstance(entry["include-group"], str)
                ):
                    # Todas as groups são percorridas; a referência não é pacote
                    # e não precisa ser expandida para preservar determinismo.
                    continue
                else:
                    raise TypeError
        return found
    except (tomllib.TOMLDecodeError, TypeError):
        _warning_malformed(path, root, warnings)
        return []


def _parse_requirements(
    path: Path, root: Path, warnings: list[FpchDiscoveryWarning]
) -> list[FpchDependency]:
    text = _read_text(path, root, warnings)
    if text is None:
        return []
    manifest = _relative(path, root)
    found: list[FpchDependency] = []
    for line in text.splitlines():
        raw = line.strip()
        if not raw or raw.startswith(("#", "-")):
            continue
        raw = raw.split(" #", 1)[0].strip()
        dependency = _python_dependency(raw, "requirements", manifest)
        if dependency is not None:
            found.append(dependency)
    return found


def _parse_package_json(
    path: Path, root: Path, warnings: list[FpchDiscoveryWarning]
) -> list[FpchDependency]:
    text = _read_text(path, root, warnings)
    if text is None:
        return []
    try:
        data = json.loads(text)
        if not isinstance(data, dict):
            raise TypeError
        manifest = _relative(path, root)
        found: list[FpchDependency] = []
        sections = (
            ("dependencies", "runtime"),
            ("devDependencies", "development"),
            ("peerDependencies", "peer"),
            ("optionalDependencies", "optional"),
        )
        for section, scope in sections:
            values = data.get(section, {})
            if not isinstance(values, dict) or not all(
                isinstance(name, str) and isinstance(specifier, str)
                for name, specifier in values.items()
            ):
                raise TypeError
            found.extend(
                FpchDependency("node", name, specifier, scope, manifest)
                for name, specifier in sorted(values.items())
            )
        return found
    except (json.JSONDecodeError, TypeError):
        _warning_malformed(path, root, warnings)
        return []


def _parse_cargo(
    path: Path, root: Path, warnings: list[FpchDiscoveryWarning]
) -> list[FpchDependency]:
    text = _read_text(path, root, warnings)
    if text is None:
        return []
    try:
        data = tomllib.loads(text)
        if not isinstance(data, dict):
            raise TypeError
        manifest = _relative(path, root)
        found: list[FpchDependency] = []
        sections = (
            ("dependencies", "runtime"),
            ("dev-dependencies", "development"),
            ("build-dependencies", "build"),
        )
        for section, scope in sections:
            values = data.get(section, {})
            if not isinstance(values, dict):
                raise TypeError
            for name, value in sorted(values.items()):
                if not isinstance(name, str) or not isinstance(value, (str, dict)):
                    raise TypeError
                if isinstance(value, str):
                    specifier = value
                else:
                    version = value.get("version", "")
                    if not isinstance(version, str):
                        raise TypeError
                    specifier = version
                found.append(FpchDependency("rust", name, specifier, scope, manifest))
        return found
    except (tomllib.TOMLDecodeError, TypeError):
        _warning_malformed(path, root, warnings)
        return []


def _parse_go_mod(
    path: Path, root: Path, warnings: list[FpchDiscoveryWarning]
) -> list[FpchDependency]:
    text = _read_text(path, root, warnings)
    if text is None:
        return []
    manifest = _relative(path, root)
    found: list[FpchDependency] = []
    in_require = False
    for line in text.splitlines():
        raw = line.split("//", 1)[0].strip()
        if not raw:
            continue
        if raw == "require (":
            in_require = True
            continue
        if in_require and raw == ")":
            in_require = False
            continue
        if raw.startswith("require "):
            raw = raw.removeprefix("require ").strip()
        elif not in_require:
            continue
        parts = raw.split()
        if len(parts) >= 2:
            found.append(FpchDependency("go", parts[0], parts[1], "runtime", manifest))
    return found


def _ci_cd_signals(root: Path) -> list[FpchCiCdSignal]:
    found: list[FpchCiCdSignal] = []
    workflows = root / ".github" / "workflows"
    github_dir = root / ".github"
    if (
        not _is_linklike(github_dir)
        and not _is_linklike(workflows)
        and workflows.is_dir()
    ):
        for path in sorted(workflows.glob("*")):
            if _is_regular_file(path) and path.suffix.casefold() in {".yml", ".yaml"}:
                found.append(FpchCiCdSignal("github_actions", _relative(path, root)))

    root_signals = (
        (".gitlab-ci.yml", "gitlab_ci"),
        ("Jenkinsfile", "jenkins"),
        ("azure-pipelines.yml", "azure_pipelines"),
        ("azure-pipelines.yaml", "azure_pipelines"),
    )
    for name, provider in root_signals:
        path = root / name
        if _is_regular_file(path):
            found.append(FpchCiCdSignal(provider, name))
    return sorted(found, key=lambda item: (item.provider, item.path))


def discover(repo: str | Path) -> FpchDiscoveryReport:
    """Inspeciona sinais estáticos conhecidos sem modificar ``repo``."""
    requested = Path(repo).expanduser()
    if not requested.exists():
        raise FpchDiscoveryError(f"caminho não existe: {requested}")
    if not requested.is_dir():
        raise FpchDiscoveryError(f"caminho não é diretório: {requested}")

    root = requested.resolve()
    warnings: list[FpchDiscoveryWarning] = []
    files = _repository_files(root, warnings)
    languages = {
        language
        for path in files
        if (language := _LANGUAGE_BY_SUFFIX.get(path.suffix.casefold())) is not None
    }
    dependencies: list[FpchDependency] = []

    for path in files:
        name = path.name.casefold()
        if name == "pyproject.toml":
            languages.add("Python")
            dependencies.extend(_parse_pyproject(path, root, warnings))
        elif name.startswith("requirements") and name.endswith(".txt"):
            languages.add("Python")
            dependencies.extend(_parse_requirements(path, root, warnings))
        elif name == "package.json":
            languages.add("JavaScript")
            dependencies.extend(_parse_package_json(path, root, warnings))
        elif name == "cargo.toml":
            languages.add("Rust")
            dependencies.extend(_parse_cargo(path, root, warnings))
        elif name == "go.mod":
            languages.add("Go")
            dependencies.extend(_parse_go_mod(path, root, warnings))

    dependency_order = lambda item: (
        item.ecosystem,
        item.manifest,
        item.scope,
        item.name.casefold(),
        item.specifier,
    )
    warning_order = lambda item: (item.path, item.code, item.message)
    return FpchDiscoveryReport(
        repo=root.as_posix(),
        languages=tuple(sorted(languages)),
        dependencies=tuple(sorted(set(dependencies), key=dependency_order)),
        ci_cd=tuple(_ci_cd_signals(root)),
        warnings=tuple(sorted(warnings, key=warning_order)),
    )
