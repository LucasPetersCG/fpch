"""C4a — contrato da CLI para planejar e aplicar o setup local."""

from __future__ import annotations

import io
from pathlib import Path
from types import SimpleNamespace

import pytest

from fpch import cli, interview, policy, setup


def _without_policy(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setattr(policy, "_cwd", lambda: tmp_path / "sem-politica")
    monkeypatch.setattr(policy, "_home", lambda: tmp_path / "sem-home")
    monkeypatch.delenv("FPCH_POLICY", raising=False)


def _preferences() -> interview.FpchInterviewPreferences:
    return interview.FpchInterviewPreferences(
        skills_on_demand=("pytest",),
        mandatory_linters=("ruff",),
        mandatory_formatters=("black",),
    )


def _answers(path: Path) -> Path:
    path.write_text(interview.dumps(_preferences()), encoding="utf-8")
    return path


def test_setup_plan_json_stdout_e_sem_escrita_implicita(
    tmp_path, monkeypatch, capsys
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    answers = _answers(tmp_path / "answers.json")
    _without_policy(monkeypatch, tmp_path)
    before = tuple(repo.iterdir())

    assert cli.main(
        ["setup", "plan", str(repo), "--answers", str(answers), "--json"]
    ) == 0
    captured = capsys.readouterr()

    parsed = setup.loads(captured.out)
    assert parsed.repo == repo.resolve().as_posix()
    assert captured.out == setup.dumps(parsed)
    assert captured.err == ""
    assert tuple(repo.iterdir()) == before


def test_setup_plan_output_e_create_only(tmp_path, monkeypatch, capsys) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    answers = _answers(tmp_path / "answers.json")
    output = tmp_path / "plan.json"
    _without_policy(monkeypatch, tmp_path)
    argv = [
        "setup",
        "plan",
        str(repo),
        "--answers",
        str(answers),
        "--json",
        "--output",
        str(output),
    ]

    assert cli.main(argv) == 0
    first = capsys.readouterr()
    assert output.read_text(encoding="utf-8") == first.out
    before = output.read_bytes()

    assert cli.main(argv) == 1
    second = capsys.readouterr()
    assert second.out == ""
    assert "já existe" in second.err
    assert output.read_bytes() == before


def test_setup_plan_answers_stdin_bytes_invalidos_sao_erro_de_esquema(
    tmp_path, monkeypatch, capsys
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    _without_policy(monkeypatch, tmp_path)
    monkeypatch.setattr(
        "sys.stdin",
        io.TextIOWrapper(io.BytesIO(b"\xff"), encoding="utf-8"),
    )

    assert cli.main(
        ["setup", "plan", str(repo), "--answers", "-", "--json"]
    ) == cli.EXIT_USO
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "UTF-8" in captured.err
    assert "Traceback" not in captured.err


def test_setup_apply_carrega_plano_confirma_e_repassa_trilha(
    tmp_path, monkeypatch, capsys
) -> None:
    plan_path = tmp_path / "plan.json"
    plan_path.write_text("{}", encoding="utf-8")
    trail = tmp_path / "audit.jsonl"
    parsed = SimpleNamespace(plan_id="fpch-plan-123")
    calls: list[tuple[object, str, Path | None]] = []
    _without_policy(monkeypatch, tmp_path)
    monkeypatch.setattr(setup, "loads", lambda raw: parsed)

    def apply_once(value, *, confirmation, trilha):
        calls.append((value, confirmation, trilha))
        return SimpleNamespace(
            ok=True,
            plan_id=parsed.plan_id,
            created=("AGENTS.md", "CLAUDE.md"),
            unchanged=(),
            conflict=None,
            audit_written=True,
        )

    monkeypatch.setattr(setup, "apply", apply_once)

    assert cli.main(
        [
            "setup",
            "apply",
            str(plan_path),
            "--confirm",
            parsed.plan_id,
            "--trilha",
            str(trail),
        ]
    ) == 0
    captured = capsys.readouterr()
    assert captured.err == ""
    assert "criados: 2" in captured.out
    assert "auditoria registrada: sim" in captured.out
    assert calls == [(parsed, parsed.plan_id, trail)]


def test_setup_apply_confirmacao_divergente_e_erro_de_uso(
    tmp_path, monkeypatch, capsys
) -> None:
    plan_path = tmp_path / "plan.json"
    plan_path.write_text("{}", encoding="utf-8")
    parsed = SimpleNamespace(plan_id="fpch-plan-correto")
    _without_policy(monkeypatch, tmp_path)
    monkeypatch.setattr(setup, "loads", lambda raw: parsed)
    monkeypatch.setattr(
        setup,
        "apply",
        lambda *_args, **_kwargs: pytest.fail("apply não deve ser chamado"),
    )

    assert cli.main(
        [
            "setup",
            "apply",
            str(plan_path),
            "--confirm",
            "fpch-plan-errado",
        ]
    ) == cli.EXIT_USO
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "plan_id" in captured.err


def test_setup_apply_plano_invalido_e_erro_de_esquema(
    tmp_path, monkeypatch, capsys
) -> None:
    plan_path = tmp_path / "plan.json"
    plan_path.write_text("{}", encoding="utf-8")
    _without_policy(monkeypatch, tmp_path)
    monkeypatch.setattr(
        setup,
        "loads",
        lambda _raw: (_ for _ in ()).throw(setup.FpchSetupError("esquema inválido")),
    )

    assert cli.main(
        ["setup", "apply", str(plan_path), "--confirm", "qualquer"]
    ) == cli.EXIT_USO
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "esquema inválido" in captured.err


def test_setup_apply_erro_de_chave_duplicada_nao_emite_controle_literal(
    tmp_path, monkeypatch, capsys
) -> None:
    plan_path = tmp_path / "plan.json"
    plan_path.write_bytes(b'{"campo\\n":1,"campo\\n":2}')
    _without_policy(monkeypatch, tmp_path)

    assert cli.main(
        ["setup", "apply", str(plan_path), "--confirm", "qualquer"]
    ) == cli.EXIT_USO
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "duplicada" in captured.err
    assert "campo" not in captured.err
    assert captured.err.count("\n") == 1


def test_setup_apply_conflito_retorna_um(tmp_path, monkeypatch, capsys) -> None:
    plan_path = tmp_path / "plan.json"
    plan_path.write_text("{}", encoding="utf-8")
    parsed = SimpleNamespace(plan_id="fpch-plan-123")
    _without_policy(monkeypatch, tmp_path)
    monkeypatch.setattr(setup, "loads", lambda _raw: parsed)
    monkeypatch.setattr(
        setup,
        "apply",
        lambda *_args, **_kwargs: SimpleNamespace(
            ok=False,
            plan_id=parsed.plan_id,
            created=(),
            unchanged=(),
            conflict=("AGENTS.md divergiu do plano",),
            audit_written=False,
        ),
    )

    assert cli.main(
        ["setup", "apply", str(plan_path), "--confirm", parsed.plan_id]
    ) == 1
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "AGENTS.md divergiu" in captured.err


def test_setup_apply_falha_operacional_retorna_um(
    tmp_path, monkeypatch, capsys
) -> None:
    plan_path = tmp_path / "plan.json"
    plan_path.write_text("{}", encoding="utf-8")
    parsed = SimpleNamespace(plan_id="fpch-plan-123")
    _without_policy(monkeypatch, tmp_path)
    monkeypatch.setattr(setup, "loads", lambda _raw: parsed)
    monkeypatch.setattr(
        setup,
        "apply",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            setup.FpchSetupError("trilha indisponível")
        ),
    )

    assert cli.main(
        ["setup", "apply", str(plan_path), "--confirm", parsed.plan_id]
    ) == 1
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "trilha indisponível" in captured.err
