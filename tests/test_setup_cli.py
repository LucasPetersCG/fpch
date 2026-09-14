"""C4a — contrato da CLI para planejar e aplicar o setup local."""

from __future__ import annotations

import hashlib
import io
import json
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


# --- `--mcp-artifact`/`--mcp-metadata` — análise de argumentos -----------------


def _plan_with_checkpoints(*names: str) -> SimpleNamespace:
    return SimpleNamespace(
        plan_id="fpch-plan-mcp",
        mcp_install_checkpoints=tuple(SimpleNamespace(name=n) for n in names),
    )


def test_setup_apply_mcp_flags_without_checkpoints_is_usage_error(
    tmp_path, monkeypatch, capsys
) -> None:
    plan_path = tmp_path / "plan.json"
    plan_path.write_text("{}", encoding="utf-8")
    parsed = SimpleNamespace(plan_id="fpch-plan-123")
    _without_policy(monkeypatch, tmp_path)
    monkeypatch.setattr(setup, "loads", lambda raw: parsed)
    monkeypatch.setattr(
        setup, "apply", lambda *_a, **_k: pytest.fail("apply não deve ser chamado")
    )

    assert cli.main(
        [
            "setup", "apply", str(plan_path), "--confirm", parsed.plan_id,
            "--mcp-artifact", "server=algo.tgz",
        ]
    ) == cli.EXIT_USO
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "checkpoint" in captured.err


def test_setup_apply_mcp_flag_malformed_empty_name_is_usage_error(
    tmp_path, monkeypatch, capsys
) -> None:
    plan_path = tmp_path / "plan.json"
    plan_path.write_text("{}", encoding="utf-8")
    parsed = _plan_with_checkpoints("server-a")
    _without_policy(monkeypatch, tmp_path)
    monkeypatch.setattr(setup, "loads", lambda raw: parsed)
    monkeypatch.setattr(
        setup, "apply", lambda *_a, **_k: pytest.fail("apply não deve ser chamado")
    )

    assert cli.main(
        [
            "setup", "apply", str(plan_path), "--confirm", parsed.plan_id,
            "--mcp-artifact", "=vazio.tgz",
            "--mcp-metadata", "server-a=meta.json",
        ]
    ) == cli.EXIT_USO
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "NAME vazio" in captured.err


def test_setup_apply_mcp_flag_malformed_empty_path_is_usage_error(
    tmp_path, monkeypatch, capsys
) -> None:
    plan_path = tmp_path / "plan.json"
    plan_path.write_text("{}", encoding="utf-8")
    parsed = _plan_with_checkpoints("server-a")
    _without_policy(monkeypatch, tmp_path)
    monkeypatch.setattr(setup, "loads", lambda raw: parsed)
    monkeypatch.setattr(
        setup, "apply", lambda *_a, **_k: pytest.fail("apply não deve ser chamado")
    )

    assert cli.main(
        [
            "setup", "apply", str(plan_path), "--confirm", parsed.plan_id,
            "--mcp-artifact", "server-a=",
            "--mcp-metadata", "server-a=meta.json",
        ]
    ) == cli.EXIT_USO
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "PATH vazio" in captured.err
    assert "server-a" in captured.err


def test_setup_apply_mcp_flag_duplicate_name_is_usage_error(
    tmp_path, monkeypatch, capsys
) -> None:
    plan_path = tmp_path / "plan.json"
    plan_path.write_text("{}", encoding="utf-8")
    parsed = _plan_with_checkpoints("server-a")
    _without_policy(monkeypatch, tmp_path)
    monkeypatch.setattr(setup, "loads", lambda raw: parsed)
    monkeypatch.setattr(
        setup, "apply", lambda *_a, **_k: pytest.fail("apply não deve ser chamado")
    )

    assert cli.main(
        [
            "setup", "apply", str(plan_path), "--confirm", parsed.plan_id,
            "--mcp-artifact", "server-a=um.tgz",
            "--mcp-artifact", "server-a=dois.tgz",
            "--mcp-metadata", "server-a=meta.json",
        ]
    ) == cli.EXIT_USO
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "repetido" in captured.err
    assert "server-a" in captured.err


def test_setup_apply_mcp_partial_coverage_reports_faltam(
    tmp_path, monkeypatch, capsys
) -> None:
    plan_path = tmp_path / "plan.json"
    plan_path.write_text("{}", encoding="utf-8")
    parsed = _plan_with_checkpoints("server-a", "server-b")
    _without_policy(monkeypatch, tmp_path)
    monkeypatch.setattr(setup, "loads", lambda raw: parsed)
    monkeypatch.setattr(
        setup, "apply", lambda *_a, **_k: pytest.fail("apply não deve ser chamado")
    )

    assert cli.main(
        [
            "setup", "apply", str(plan_path), "--confirm", parsed.plan_id,
            "--mcp-artifact", "server-a=um.tgz",
            "--mcp-metadata", "server-a=um.json",
        ]
    ) == cli.EXIT_USO
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "faltam" in captured.err
    assert "server-b" in captured.err


def test_setup_apply_mcp_unknown_name_is_usage_error(
    tmp_path, monkeypatch, capsys
) -> None:
    plan_path = tmp_path / "plan.json"
    plan_path.write_text("{}", encoding="utf-8")
    parsed = _plan_with_checkpoints("server-a")
    _without_policy(monkeypatch, tmp_path)
    monkeypatch.setattr(setup, "loads", lambda raw: parsed)
    monkeypatch.setattr(
        setup, "apply", lambda *_a, **_k: pytest.fail("apply não deve ser chamado")
    )

    assert cli.main(
        [
            "setup", "apply", str(plan_path), "--confirm", parsed.plan_id,
            "--mcp-artifact", "server-a=um.tgz",
            "--mcp-artifact", "server-x=outro.tgz",
            "--mcp-metadata", "server-a=um.json",
            "--mcp-metadata", "server-x=outro.json",
        ]
    ) == cli.EXIT_USO
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "desconhecidos" in captured.err
    assert "server-x" in captured.err


def test_setup_apply_full_mcp_coverage_calls_apply_with_evidence_mapping(
    tmp_path, monkeypatch, capsys
) -> None:
    plan_path = tmp_path / "plan.json"
    plan_path.write_text("{}", encoding="utf-8")
    parsed = _plan_with_checkpoints("server-a", "server-b")
    calls: list[tuple[object, str, Path | None, object]] = []
    _without_policy(monkeypatch, tmp_path)
    monkeypatch.setattr(setup, "loads", lambda raw: parsed)

    def fake_apply(value, *, confirmation, trilha, mcp_evidence=None):
        calls.append((value, confirmation, trilha, mcp_evidence))
        return SimpleNamespace(
            ok=True,
            plan_id=parsed.plan_id,
            created=("AGENTS.md",),
            unchanged=(),
            conflict=None,
            audit_written=True,
            mcp_verified=("server-a", "server-b"),
        )

    monkeypatch.setattr(setup, "apply", fake_apply)

    artifact_a = tmp_path / "a.tgz"
    metadata_a = tmp_path / "a.json"
    artifact_b = tmp_path / "b.tgz"
    metadata_b = tmp_path / "b.json"

    assert cli.main(
        [
            "setup", "apply", str(plan_path), "--confirm", parsed.plan_id,
            "--mcp-artifact", f"server-a={artifact_a}",
            "--mcp-artifact", f"server-b={artifact_b}",
            "--mcp-metadata", f"server-a={metadata_a}",
            "--mcp-metadata", f"server-b={metadata_b}",
        ]
    ) == 0
    captured = capsys.readouterr()
    assert "MCP verificados (não instalados): server-a, server-b" in captured.out
    assert "nenhum MCP foi instalado ou executado" in captured.out

    assert len(calls) == 1
    _value, confirmation, trilha, mcp_evidence = calls[0]
    assert confirmation == parsed.plan_id
    assert trilha is None
    assert set(mcp_evidence) == {"server-a", "server-b"}
    assert mcp_evidence["server-a"].artifact == Path(artifact_a)
    assert mcp_evidence["server-a"].metadata == Path(metadata_a)
    assert mcp_evidence["server-b"].artifact == Path(artifact_b)
    assert mcp_evidence["server-b"].metadata == Path(metadata_b)


# --- Fim a fim com `setup plan --mcp-checkpoint` real --------------------------


def _artifact_and_metadata(
    tmp_path: Path, label: str, *, content: bytes = b"pacote de exemplo do checkpoint"
) -> tuple[Path, str, Path, Path]:
    digest = hashlib.sha256(content).hexdigest()
    artifact_dir = tmp_path / f"{label}-artefato"
    artifact_dir.mkdir()
    artifact = artifact_dir / "1.2.3.tgz"
    artifact.write_bytes(content)
    metadata = tmp_path / f"{label}-metadata.json"
    metadata.write_text(
        json.dumps({"tools": [{"description": "ok"}]}), encoding="utf-8"
    )
    return artifact_dir, digest, artifact, metadata


def test_setup_apply_end_to_end_mcp_evidence_success(
    tmp_path, monkeypatch, capsys
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    answers = _answers(tmp_path / "answers.json")
    _, digest, artifact, metadata = _artifact_and_metadata(tmp_path, "ok")
    checkpoint = _checkpoint(
        tmp_path / "checkpoint.json", "server-filesystem", expected_sha256=digest
    )
    plan_path = tmp_path / "plan.json"
    _without_policy(monkeypatch, tmp_path)

    assert cli.main(
        [
            "setup", "plan", str(repo), "--answers", str(answers),
            "--mcp-checkpoint", str(checkpoint), "--output", str(plan_path),
        ]
    ) == 1
    capsys.readouterr()
    planned = setup.loads(plan_path.read_bytes())

    result_code = cli.main(
        [
            "setup", "apply", str(plan_path), "--confirm", planned.plan_id,
            "--mcp-artifact", f"server-filesystem={artifact}",
            "--mcp-metadata", f"server-filesystem={metadata}",
        ]
    )
    captured = capsys.readouterr()

    assert result_code == 0
    assert "não instalados" in captured.out
    assert (repo / ".fpch" / "mcp-verification.json").exists()


def test_setup_apply_end_to_end_mcp_digest_mismatch_writes_nothing(
    tmp_path, monkeypatch, capsys
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    answers = _answers(tmp_path / "answers.json")
    _, _digest, artifact, metadata = _artifact_and_metadata(tmp_path, "divergente")
    checkpoint = _checkpoint(
        tmp_path / "checkpoint.json", "server-filesystem", expected_sha256="a" * 64
    )
    plan_path = tmp_path / "plan.json"
    _without_policy(monkeypatch, tmp_path)

    assert cli.main(
        [
            "setup", "plan", str(repo), "--answers", str(answers),
            "--mcp-checkpoint", str(checkpoint), "--output", str(plan_path),
        ]
    ) == 1
    capsys.readouterr()
    planned = setup.loads(plan_path.read_bytes())

    result_code = cli.main(
        [
            "setup", "apply", str(plan_path), "--confirm", planned.plan_id,
            "--mcp-artifact", f"server-filesystem={artifact}",
            "--mcp-metadata", f"server-filesystem={metadata}",
        ]
    )
    capsys.readouterr()

    assert result_code == 1
    assert tuple(repo.iterdir()) == ()


def test_setup_apply_end_to_end_mcp_metadata_with_hidden_unicode_fails(
    tmp_path, monkeypatch, capsys
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    answers = _answers(tmp_path / "answers.json")
    content = b"pacote de exemplo do checkpoint"
    digest = hashlib.sha256(content).hexdigest()
    artifact_dir = tmp_path / "oculto-artefato"
    artifact_dir.mkdir()
    artifact = artifact_dir / "1.2.3.tgz"
    artifact.write_bytes(content)
    metadata = tmp_path / "oculto-metadata.json"
    hidden = json.dumps(
        {"tools": [{"description": "oculto" + chr(0xE0041)}]}, ensure_ascii=False
    )
    metadata.write_bytes(hidden.encode("utf-8"))
    checkpoint = _checkpoint(
        tmp_path / "checkpoint.json", "server-filesystem", expected_sha256=digest
    )
    plan_path = tmp_path / "plan.json"
    _without_policy(monkeypatch, tmp_path)

    assert cli.main(
        [
            "setup", "plan", str(repo), "--answers", str(answers),
            "--mcp-checkpoint", str(checkpoint), "--output", str(plan_path),
        ]
    ) == 1
    capsys.readouterr()
    planned = setup.loads(plan_path.read_bytes())

    result_code = cli.main(
        [
            "setup", "apply", str(plan_path), "--confirm", planned.plan_id,
            "--mcp-artifact", f"server-filesystem={artifact}",
            "--mcp-metadata", f"server-filesystem={metadata}",
        ]
    )
    capsys.readouterr()

    assert result_code == 1
    assert tuple(repo.iterdir()) == ()
