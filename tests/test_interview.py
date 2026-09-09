"""C3 — entrevista explícita, fechada e sem efeitos colaterais implícitos."""

from __future__ import annotations

import io
import json
import socket
import subprocess
from pathlib import Path

import pytest

from fpch import cli, discovery, interview, policy


def _preferences() -> interview.FpchInterviewPreferences:
    return interview.FpchInterviewPreferences(
        skills_on_demand=("pytest", "security/audit:v1"),
        mandatory_linters=("ruff",),
        mandatory_formatters=("black+isort",),
    )


def _report(tmp_path: Path) -> discovery.FpchDiscoveryReport:
    repo = tmp_path / "repo"
    repo.mkdir(exist_ok=True)
    return discovery.FpchDiscoveryReport(
        repo=repo.resolve().as_posix(),
        languages=("Python",),
        dependencies=(
            discovery.FpchDependency(
                "python", "pytest", ">=8", "group:test", "pyproject.toml"
            ),
        ),
        ci_cd=(discovery.FpchCiCdSignal("github_actions", ".github/workflows/test.yml"),),
        warnings=(),
    )


def _without_policy(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setattr(policy, "_cwd", lambda: tmp_path / "sem-politica")
    monkeypatch.setattr(policy, "_home", lambda: tmp_path / "sem-home")
    monkeypatch.delenv("FPCH_POLICY", raising=False)


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


def test_round_trip_e_json_canonico() -> None:
    preferences = _preferences()

    encoded = interview.dumps(preferences)

    assert encoded == (
        '{"mandatory_formatters":["black+isort"],'
        '"mandatory_linters":["ruff"],"schema_version":1,'
        '"skills_on_demand":["pytest","security/audit:v1"]}\n'
    )
    assert interview.loads(encoded) == preferences
    assert preferences.schema_version == 1
    assert preferences.as_dict()["skills_on_demand"] == [
        "pytest",
        "security/audit:v1",
    ]


@pytest.mark.parametrize(
    "payload, fragment",
    [
        ({"schema_version": 2}, "campos ausentes"),
        (
            {
                "schema_version": 2,
                "skills_on_demand": [],
                "mandatory_linters": [],
                "mandatory_formatters": [],
            },
            "não suportada",
        ),
        (
            {
                "schema_version": True,
                "skills_on_demand": [],
                "mandatory_linters": [],
                "mandatory_formatters": [],
            },
            "deve ser inteiro",
        ),
        (
            {
                "schema_version": 1,
                "skills_on_demand": "pytest",
                "mandatory_linters": [],
                "mandatory_formatters": [],
            },
            "lista JSON",
        ),
        (
            {
                "schema_version": 1,
                "skills_on_demand": [],
                "mandatory_linters": [],
                "mandatory_formatters": [],
                "surprise": [],
            },
            "campos desconhecidos",
        ),
    ],
)
def test_schema_fechado_rejeita_versao_campos_e_tipos(payload, fragment) -> None:
    with pytest.raises(interview.FpchInterviewError, match=fragment):
        interview.loads(json.dumps(payload))


def test_rejeita_chaves_json_e_identificadores_duplicados() -> None:
    duplicate_key = (
        '{"schema_version":1,"schema_version":1,"skills_on_demand":[],'
        '"mandatory_linters":[],"mandatory_formatters":[]}'
    )
    with pytest.raises(interview.FpchInterviewError, match="chave JSON duplicada"):
        interview.loads(duplicate_key)

    payload = _preferences().as_dict()
    payload["mandatory_linters"] = ["secret-token", "secret-token"]
    with pytest.raises(interview.FpchInterviewError, match="identificador duplicado") as error:
        interview.loads(json.dumps(payload))
    assert "secret-token" not in str(error.value)


@pytest.mark.parametrize(
    "invalid",
    ["", " ruff", "ruff ", "ruff lint", "ruff\nnext", "ruff\x00", "ação", "@scope/pkg", "x" * 129],
)
def test_identificadores_usam_regex_ascii_conservadora(invalid: str) -> None:
    payload = _preferences().as_dict()
    payload["skills_on_demand"] = [invalid]
    with pytest.raises(interview.FpchInterviewError, match="ASCII"):
        interview.loads(json.dumps(payload, ensure_ascii=False))


def test_limites_de_itens_e_bytes() -> None:
    payload = _preferences().as_dict()
    payload["skills_on_demand"] = [f"skill-{index}" for index in range(65)]
    with pytest.raises(interview.FpchInterviewError, match="64 itens"):
        interview.loads(json.dumps(payload))

    oversized = b" " * (interview.FPCH_INTERVIEW_MAX_BYTES + 1)
    with pytest.raises(interview.FpchInterviewError, match="65536 bytes"):
        interview.loads(oversized)
    with pytest.raises(interview.FpchInterviewError, match="65536 bytes"):
        interview.load_answers(io.BytesIO(oversized))


def test_interview_injetada_faz_exatamente_tres_perguntas_e_preserva_ordem(tmp_path) -> None:
    questions: list[str] = []
    output: list[str] = []
    answers = iter(("skill-z,skill-a", "ruff,pylint", "black"))

    def input_fn(question: str) -> str:
        questions.append(question)
        return next(answers)

    preferences = interview.interview(
        _report(tmp_path), input_fn=input_fn, output_fn=output.append
    )

    assert len(questions) == 3
    assert preferences.skills_on_demand == ("skill-z", "skill-a")
    assert preferences.mandatory_linters == ("ruff", "pylint")
    assert preferences.mandatory_formatters == ("black",)
    assert any("apenas contexto" in line for line in output)
    assert any("segredos" in line for line in output)


def test_interview_branco_e_tupla_vazia_sem_defaults_detectados(tmp_path) -> None:
    answers = iter(("", "   ", "\t"))
    preferences = interview.interview(
        _report(tmp_path), input_fn=lambda _question: next(answers), output_fn=lambda _line: None
    )

    assert preferences == interview.FpchInterviewPreferences()


def test_load_answers_erros_de_json_utf8_e_caminho(tmp_path) -> None:
    with pytest.raises(interview.FpchInterviewError, match="JSON"):
        interview.loads("{")
    with pytest.raises(interview.FpchInterviewError, match="UTF-8"):
        interview.loads(b"\xff")
    with pytest.raises(interview.FpchInterviewError, match="não foi possível ler"):
        interview.load_answers(tmp_path / "ausente.json")


def test_save_new_cria_exclusivamente_e_nao_sobrescreve(tmp_path) -> None:
    path = tmp_path / "answers.json"
    preferences = _preferences()

    assert interview.save_new(path, preferences) == path
    assert path.read_text(encoding="utf-8") == interview.dumps(preferences)

    before = path.read_bytes()
    with pytest.raises(interview.FpchInterviewError, match="já existe"):
        interview.save_new(path, interview.FpchInterviewPreferences())
    assert path.read_bytes() == before

    with pytest.raises(interview.FpchInterviewError, match="diretório"):
        interview.save_new(tmp_path, preferences)


def test_save_new_recusa_link_sem_seguir_destino(tmp_path) -> None:
    target = tmp_path / "target.json"
    target.write_text("preservar", encoding="utf-8")
    link = tmp_path / "link.json"
    try:
        link.symlink_to(target)
    except (NotImplementedError, OSError) as exc:
        pytest.skip(f"links simbólicos indisponíveis neste ambiente: {exc}")

    with pytest.raises(interview.FpchInterviewError, match="link, junction ou reparse"):
        interview.save_new(link, _preferences())
    assert target.read_text(encoding="utf-8") == "preservar"


def test_save_new_recusa_ancestral_link_sem_criar_no_destino(tmp_path) -> None:
    target_dir = tmp_path / "target"
    target_dir.mkdir()
    linked_dir = tmp_path / "linked"
    try:
        linked_dir.symlink_to(target_dir, target_is_directory=True)
    except (NotImplementedError, OSError) as exc:
        pytest.skip(f"links simbólicos indisponíveis neste ambiente: {exc}")

    with pytest.raises(interview.FpchInterviewError, match="ancestral da saída"):
        interview.save_new(linked_dir / "answers.json", _preferences())
    assert not (target_dir / "answers.json").exists()


def test_save_new_recusa_ancestral_marcado_como_junction(tmp_path, monkeypatch) -> None:
    parent = tmp_path / "junction"
    parent.mkdir()
    real_isjunction = getattr(interview.os.path, "isjunction", lambda _path: False)
    monkeypatch.setattr(
        interview.os.path,
        "isjunction",
        lambda path: Path(path) == parent or real_isjunction(path),
        raising=False,
    )

    with pytest.raises(interview.FpchInterviewError, match="ancestral da saída"):
        interview.save_new(parent / "answers.json", _preferences())


@pytest.mark.parametrize("failure_stage", ["fdopen", "write"])
def test_save_new_remove_apenas_parcial_proprio_e_permite_retry(
    tmp_path, monkeypatch, failure_stage
) -> None:
    path = tmp_path / "answers.json"
    real_fdopen = interview.os.fdopen

    if failure_stage == "fdopen":
        def broken_fdopen(*_args, **_kwargs):
            raise OSError("falha injetada em fdopen")
    else:
        def broken_fdopen(descriptor, *args, **kwargs):
            real_stream = real_fdopen(descriptor, *args, **kwargs)

            class BrokenWriter:
                def __enter__(self):
                    return self

                def write(self, _payload):
                    raise OSError("falha injetada em write")

                def flush(self):
                    return None

                def __exit__(self, *_exc):
                    real_stream.close()

            return BrokenWriter()

    monkeypatch.setattr(interview.os, "fdopen", broken_fdopen)
    with pytest.raises(interview.FpchInterviewError, match="falha injetada"):
        interview.save_new(path, _preferences())
    assert not path.exists()

    monkeypatch.setattr(interview.os, "fdopen", real_fdopen)
    assert interview.save_new(path, _preferences()) == path


def test_cli_answers_json_tem_stdout_limpo_e_descoberta_e_contexto(
    tmp_path, monkeypatch, capsys
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    answers = tmp_path / "answers.json"
    answers.write_text(interview.dumps(_preferences()), encoding="utf-8")
    _without_policy(monkeypatch, tmp_path)

    real_discover = discovery.discover
    calls: list[Path] = []

    def discover_once(path):
        calls.append(Path(path))
        return real_discover(path)

    monkeypatch.setattr(discovery, "discover", discover_once)
    assert cli.main(
        ["interview", str(repo), "--answers", str(answers), "--json"]
    ) == 0
    captured = capsys.readouterr()

    assert captured.out == interview.dumps(_preferences())
    assert captured.err == ""
    assert calls == [repo]


def test_cli_nao_tty_sem_answers_orienta_e_devolve_uso(tmp_path, monkeypatch, capsys) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    _without_policy(monkeypatch, tmp_path)

    class NonTty(io.StringIO):
        def isatty(self) -> bool:
            return False

    monkeypatch.setattr("sys.stdin", NonTty())
    assert cli.main(["interview", str(repo), "--json"]) == cli.EXIT_USO
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "--answers CAMINHO ou --answers -" in captured.err


def test_cli_answers_stdin_output_explicito_e_exclusivo(
    tmp_path, monkeypatch, capsys
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    output = tmp_path / "saved.json"
    _without_policy(monkeypatch, tmp_path)
    monkeypatch.setattr("sys.stdin", io.StringIO(interview.dumps(_preferences())))

    argv = [
        "interview",
        str(repo),
        "--answers",
        "-",
        "--json",
        "--output",
        str(output),
    ]
    assert cli.main(argv) == 0
    assert capsys.readouterr().out == interview.dumps(_preferences())
    assert output.read_text(encoding="utf-8") == interview.dumps(_preferences())

    monkeypatch.setattr("sys.stdin", io.StringIO(interview.dumps(_preferences())))
    assert cli.main(argv) == cli.EXIT_USO
    assert "já existe" in capsys.readouterr().err


def test_cli_answers_stdin_bytes_invalidos_devolve_uso_sem_traceback(
    tmp_path, monkeypatch, capsys
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    _without_policy(monkeypatch, tmp_path)
    monkeypatch.setattr(
        "sys.stdin",
        io.TextIOWrapper(io.BytesIO(b"\xff"), encoding="utf-8"),
    )

    assert cli.main(["interview", str(repo), "--answers", "-", "--json"]) == cli.EXIT_USO
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "UTF-8" in captured.err
    assert "Traceback" not in captured.err


def test_cli_sem_output_nao_altera_repositorio(tmp_path, monkeypatch, capsys) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "pyproject.toml").write_text(
        '[project]\nname="demo"\ndependencies=["pytest>=8"]\n', encoding="utf-8"
    )
    answers = tmp_path / "answers.json"
    answers.write_text(interview.dumps(_preferences()), encoding="utf-8")
    _without_policy(monkeypatch, tmp_path)
    before = _snapshot(repo)

    assert cli.main(
        ["interview", str(repo), "--answers", str(answers), "--json"]
    ) == 0
    capsys.readouterr()
    assert _snapshot(repo) == before


def test_fluxo_nao_usa_subprocesso_nem_rede(tmp_path, monkeypatch) -> None:
    def forbidden(*_args, **_kwargs):
        raise AssertionError("subprocesso/rede não permitido")

    monkeypatch.setattr(subprocess, "run", forbidden)
    monkeypatch.setattr(subprocess, "Popen", forbidden)
    monkeypatch.setattr(socket, "socket", forbidden)
    answers = iter(("pytest", "ruff", "black"))

    preferences = interview.interview(
        _report(tmp_path),
        input_fn=lambda _question: next(answers),
        output_fn=lambda _line: None,
    )

    assert preferences == _preferences().__class__(
        skills_on_demand=("pytest",),
        mandatory_linters=("ruff",),
        mandatory_formatters=("black",),
    )
