"""C2 — descoberta somente leitura de sinais explícitos de repositório."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from fpch import cli, discovery, policy


def _write(path: Path, text: str = "") -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def _snapshot(root: Path) -> dict[str, tuple[int, int, int, bytes | None]]:
    paths = [root, *sorted(root.rglob("*"), key=lambda item: item.as_posix())]
    return {
        "." if path == root else path.relative_to(root).as_posix(): (
            path.stat().st_mode,
            path.stat().st_size,
            path.stat().st_mtime_ns,
            path.read_bytes() if path.is_file() else None,
        )
        for path in paths
    }


def test_discover_detecta_manifestos_linguagens_e_ci_cd(tmp_path):
    repo = tmp_path / "repo"
    _write(
        repo / "pyproject.toml",
        '[project]\nname = "demo"\ndependencies = ["requests>=2"]\n'
        '[project.optional-dependencies]\ndev = ["pytest>=8"]\n',
    )
    _write(repo / "requirements-dev.txt", "ruff==0.6\n")
    _write(
        repo / "package.json",
        '{"dependencies":{"react":"^18"},"devDependencies":{"vitest":"^2"}}',
    )
    _write(repo / "Cargo.toml", '[package]\nname="demo"\nversion="0.1.0"\n'
           '[dependencies]\nserde = "1"\n')
    _write(repo / "go.mod", "module example.test/demo\nrequire example.test/lib v1.2.3\n")
    _write(repo / "src" / "main.py", "pass\n")
    _write(repo / "web" / "app.ts", "export {}\n")
    _write(repo / ".github" / "workflows" / "test.yml", "name: test\n")
    _write(repo / ".gitlab-ci.yml", "test:\n  script: true\n")
    _write(repo / "Jenkinsfile", "pipeline {}\n")
    _write(repo / "azure-pipelines.yaml", "steps: []\n")

    report = discovery.discover(repo).as_dict()

    assert report["repo"] == repo.resolve().as_posix()
    assert report["languages"] == ["Go", "JavaScript", "Python", "Rust", "TypeScript"]
    assert {(item["ecosystem"], item["name"]) for item in report["dependencies"]} == {
        ("python", "requests"),
        ("python", "pytest"),
        ("python", "ruff"),
        ("node", "react"),
        ("node", "vitest"),
        ("rust", "serde"),
        ("go", "example.test/lib"),
    }
    assert {item["provider"] for item in report["ci_cd"]} == {
        "github_actions",
        "gitlab_ci",
        "jenkins",
        "azure_pipelines",
    }
    assert report["warnings"] == []


def test_discover_ignora_diretorios_ocultos_e_vendorizados(tmp_path):
    repo = tmp_path / "repo"
    _write(repo / "src" / "main.py", "pass\n")
    _write(repo / ".segredo" / "Hidden.java", "class Hidden {}\n")
    _write(repo / "vendor" / "vendored.rs", "fn main() {}\n")
    _write(repo / "node_modules" / "pkg" / "index.js", "module.exports = {}\n")

    report = discovery.discover(repo)

    assert report.languages == ("Python",)
    assert report.dependencies == ()


def test_discover_ignora_links_simbolicos_para_fora_da_raiz(tmp_path):
    repo = tmp_path / "repo"
    outside = tmp_path / "outside"
    _write(repo / "src" / "main.py", "pass\n")
    _write(outside / "requirements.txt", "outside-only==1\n")
    _write(outside / "secret.ts", "export {}\n")
    _write(outside / "workflows" / "escape.yml", "name: escape\n")

    try:
        (repo / "requirements-linked.txt").symlink_to(
            outside / "requirements.txt"
        )
        (repo / "linked-dir").symlink_to(outside, target_is_directory=True)
        (repo / ".github").mkdir()
        (repo / ".github" / "workflows").symlink_to(
            outside / "workflows", target_is_directory=True
        )
    except (NotImplementedError, OSError) as error:
        pytest.skip(f"links simbólicos indisponíveis neste ambiente: {error}")

    report = discovery.discover(repo)

    assert report.languages == ("Python",)
    assert report.dependencies == ()
    assert report.ci_cd == ()


def test_manifestos_malformados_viram_warnings_estruturados(tmp_path):
    repo = tmp_path / "repo"
    _write(repo / "pyproject.toml", "[project\n")
    _write(repo / "package.json", "{")
    _write(repo / "Cargo.toml", "[dependencies\n")
    _write(repo / "requirements.txt", "click>=8\n")

    report = discovery.discover(repo).as_dict()

    assert [warning["path"] for warning in report["warnings"]] == [
        "Cargo.toml",
        "package.json",
        "pyproject.toml",
    ]
    assert {warning["code"] for warning in report["warnings"]} == {"manifest_malformed"}
    assert any(item["name"] == "click" for item in report["dependencies"])


def test_pyproject_dependency_groups_pep735_sao_descobertos_deterministicamente(
    tmp_path,
):
    repo = tmp_path / "repo"
    _write(
        repo / "pyproject.toml",
        '[project]\nname = "demo"\n'
        '[dependency-groups]\n'
        'lint = ["ruff>=0.6", {include-group = "dev"}]\n'
        'dev = ["pytest>=8"]\n',
    )

    first = discovery.discover(repo)
    second = discovery.discover(repo)

    assert first == second
    assert [
        (item.name, item.specifier, item.scope)
        for item in first.dependencies
    ] == [
        ("pytest", ">=8", "group:dev"),
        ("ruff", ">=0.6", "group:lint"),
    ]
    assert first.warnings == ()


def test_manifesto_acima_do_limite_gera_warning_e_resultado_parcial(tmp_path):
    repo = tmp_path / "repo"
    oversized = '{"dependencies":{"ignored":"' + (
        "x" * discovery.FPCH_MANIFEST_MAX_BYTES
    ) + '"}}'
    _write(repo / "package.json", oversized)
    _write(repo / "requirements.txt", "click>=8\n")

    report = discovery.discover(repo).as_dict()

    assert report["languages"] == ["JavaScript", "Python"]
    assert [item["name"] for item in report["dependencies"]] == ["click"]
    assert report["warnings"] == [
        {
            "code": "manifest_too_large",
            "path": "package.json",
            "message": (
                "manifesto excede o limite de "
                f"{discovery.FPCH_MANIFEST_MAX_BYTES} bytes; conteúdo ignorado"
            ),
        }
    ]


def test_discover_repositorio_vazio(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()

    report = discovery.discover(repo).as_dict()

    assert report == {
        "repo": repo.resolve().as_posix(),
        "languages": [],
        "dependencies": [],
        "ci_cd": [],
        "warnings": [],
    }


def test_discover_nao_altera_caminhos_conteudos_ou_metadados_estaveis(tmp_path):
    repo = tmp_path / "repo"
    _write(
        repo / "pyproject.toml",
        '[project]\nname = "demo"\ndependencies = ["requests>=2"]\n',
    )
    _write(repo / "src" / "main.py", "pass\n")
    _write(repo / ".github" / "workflows" / "test.yml", "name: test\n")
    before = _snapshot(repo)

    discovery.discover(repo)

    assert _snapshot(repo) == before


def test_relatorio_e_json_sao_deterministicos(tmp_path, monkeypatch, capsys):
    repo = tmp_path / "repo"
    _write(repo / "package.json", '{"dependencies":{"z":"2","a":"1"}}')
    monkeypatch.setattr(policy, "_cwd", lambda: tmp_path / "sem-politica")
    monkeypatch.setattr(policy, "_home", lambda: tmp_path / "sem-home")
    monkeypatch.delenv("FPCH_POLICY", raising=False)

    assert cli.main(["discover", str(repo), "--json"]) == 0
    first_capture = capsys.readouterr()
    assert first_capture.err == ""
    assert cli.main(["discover", str(repo), "--json"]) == 0
    second_capture = capsys.readouterr()
    assert second_capture.err == ""

    assert first_capture.out == second_capture.out
    payload = json.loads(first_capture.out)
    assert set(payload) == {"repo", "languages", "dependencies", "ci_cd", "warnings"}
    assert [item["name"] for item in payload["dependencies"]] == ["a", "z"]


def test_cli_devolve_codigo_2_para_caminho_ausente_ou_arquivo(
    tmp_path, monkeypatch, capsys
):
    arquivo = tmp_path / "arquivo.txt"
    arquivo.write_text("x", encoding="utf-8")
    monkeypatch.setattr(policy, "_cwd", lambda: tmp_path / "sem-politica")
    monkeypatch.setattr(policy, "_home", lambda: tmp_path / "sem-home")
    monkeypatch.delenv("FPCH_POLICY", raising=False)

    for path in (tmp_path / "ausente", arquivo):
        assert cli.main(["discover", str(path), "--json"]) == cli.EXIT_USO
        assert "erro de descoberta" in capsys.readouterr().err
