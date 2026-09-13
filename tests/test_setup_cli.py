"""C4a — contrato da CLI para planejar e aplicar o setup local."""

from __future__ import annotations

import io
from pathlib import Path
from types import SimpleNamespace

import pytest

from fpch import cli, interview, mcp, policy, setup


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


def _checkpoint(
    path: Path,
    name: str,
    *,
    expected_sha256: str,
) -> Path:
    value = mcp.FpchMcpInstallCheckpoint(
        name=name,
        source=f"https://packages.example.test/mcp/{name}/1.2.3.tgz",
        version="1.2.3",
        expected_sha256=expected_sha256,
    )
    path.write_text(mcp.dumps(value), encoding="utf-8")
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


def test_setup_plan_accepts_repeatable_mcp_checkpoints_but_stays_blocked(
    tmp_path, monkeypatch, capsys
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    answers = _answers(tmp_path / "answers.json")
    second = _checkpoint(
        tmp_path / "zeta.json", "server-zeta", expected_sha256="b" * 64
    )
    first = _checkpoint(
        tmp_path / "alpha.json", "server-alpha", expected_sha256="a" * 64
    )
    _without_policy(monkeypatch, tmp_path)
    before = tuple(repo.iterdir())

    result = cli.main(
        [
            "setup",
            "plan",
            str(repo),
            "--answers",
            str(answers),
            "--mcp-checkpoint",
            str(second),
            "--mcp-checkpoint",
            str(first),
            "--json",
        ]
    )
    captured = capsys.readouterr()

    assert result == 1
    parsed = setup.loads(captured.out)
    assert [item.name for item in parsed.mcp_install_checkpoints] == [
        "server-alpha",
        "server-zeta",
    ]
    assert captured.out == setup.dumps(parsed)
    assert captured.err == ""
    assert tuple(repo.iterdir()) == before


def test_setup_plan_output_then_apply_with_mcp_stays_fail_closed(
    tmp_path, monkeypatch, capsys
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    answers = _answers(tmp_path / "answers.json")
    checkpoint = _checkpoint(
        tmp_path / "checkpoint.json", "server-filesystem", expected_sha256="a" * 64
    )
    output = tmp_path / "plan.json"
    trail = tmp_path / "audit.jsonl"
    _without_policy(monkeypatch, tmp_path)
    monkeypatch.setattr(
        setup.audit,
        "write",
        lambda *_args, **_kwargs: pytest.fail("plano MCP não pode escrever auditoria"),
    )

    assert cli.main(
        [
            "setup", "plan", str(repo), "--answers", str(answers),
            "--mcp-checkpoint", str(checkpoint), "--output", str(output),
        ]
    ) == 1
    capsys.readouterr()
    planned = setup.loads(output.read_bytes())

    assert cli.main(
        ["setup", "apply", str(output), "--confirm", planned.plan_id, "--trilha", str(trail)]
    ) == 1
    captured = capsys.readouterr()
    assert "permanece bloqueada" in captured.err
    assert tuple(repo.iterdir()) == ()
    assert not trail.exists()


def test_setup_plan_rejects_duplicate_mcp_checkpoint_names(
    tmp_path, monkeypatch, capsys
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    answers = _answers(tmp_path / "answers.json")
    checkpoint = _checkpoint(
        tmp_path / "checkpoint.json", "server-filesystem", expected_sha256="a" * 64
    )
    output = tmp_path / "plan.json"
    _without_policy(monkeypatch, tmp_path)

    assert cli.main(
        [
            "setup", "plan", str(repo), "--answers", str(answers),
            "--mcp-checkpoint", str(checkpoint),
            "--mcp-checkpoint", str(checkpoint), "--output", str(output),
        ]
    ) == cli.EXIT_USO
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "únicos" in captured.err
    assert not output.exists()
    assert tuple(repo.iterdir()) == ()


def test_setup_plan_invalid_mcp_checkpoint_is_safe_usage_error(
    tmp_path, monkeypatch, capsys
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    answers = _answers(tmp_path / "answers.json")
    checkpoint = tmp_path / "checkpoint.json"
    checkpoint.write_text(
        '{"name":"server","source":"https://example.test/a?token=secret",'
        '"version":"1.2.3","expected_sha256":"' + "a" * 64 + '"}\n',
        encoding="utf-8",
    )
    _without_policy(monkeypatch, tmp_path)

    result = cli.main(
        [
            "setup",
            "plan",
            str(repo),
            "--answers",
            str(answers),
            "--mcp-checkpoint",
            str(checkpoint),
            "--json",
        ]
    )
    captured = capsys.readouterr()

    assert result == cli.EXIT_USO
    assert captured.out == ""
    assert "erro de checkpoint MCP" in captured.err
    assert "token=secret" not in captured.err
    assert "Traceback" not in captured.err
    assert tuple(repo.iterdir()) == ()


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
