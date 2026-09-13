"""C7 — contrato da CLI `fpch mcp verify` (offline, somente leitura)."""

from __future__ import annotations

import hashlib
import json
import socket
import subprocess
import urllib.request
from pathlib import Path

import pytest

from fpch import cli, mcp, policy


def _without_policy(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setattr(policy, "_cwd", lambda: tmp_path / "sem-politica")
    monkeypatch.setattr(policy, "_home", lambda: tmp_path / "sem-home")
    monkeypatch.delenv("FPCH_POLICY", raising=False)


def _fixture(
    tmp_path: Path,
    *,
    content: bytes = b"bytes do pacote MCP",
    artifact_name: str = "1.2.3.tgz",
    expected_sha256: str | None = None,
) -> tuple[Path, Path]:
    checkpoint = mcp.FpchMcpInstallCheckpoint(
        name="server-filesystem",
        source="https://packages.example.test/mcp/server-filesystem/1.2.3.tgz",
        version="1.2.3",
        expected_sha256=expected_sha256 or hashlib.sha256(content).hexdigest(),
    )
    checkpoint_path = tmp_path / "checkpoint.json"
    checkpoint_path.write_text(mcp.dumps(checkpoint), encoding="utf-8")
    folder = tmp_path / "artefatos"
    folder.mkdir(exist_ok=True)
    artifact = folder / artifact_name
    artifact.write_bytes(content)
    return checkpoint_path, artifact


def _snapshot(root: Path) -> list[tuple[Path, int, int]]:
    return sorted(
        (path, path.stat().st_size, path.stat().st_mtime_ns) for path in root.rglob("*")
    )


def test_mcp_verify_match_exit_zero_human_output(tmp_path, monkeypatch, capsys) -> None:
    checkpoint, artifact = _fixture(tmp_path)
    _without_policy(monkeypatch, tmp_path)
    before = _snapshot(tmp_path)

    assert cli.main(["mcp", "verify", str(checkpoint), str(artifact)]) == 0
    captured = capsys.readouterr()

    assert "artefato MCP: verificado" in captured.out
    assert "digest: confere" in captured.out
    assert "1.2.3.tgz (esperado: 1.2.3.tgz) — confere" in captured.out
    assert "nada foi instalado, executado ou gravado" in captured.out
    assert captured.err == ""
    assert _snapshot(tmp_path) == before


def test_mcp_verify_json_is_canonical_and_matches_library(
    tmp_path, monkeypatch, capsys
) -> None:
    checkpoint, artifact = _fixture(tmp_path)
    _without_policy(monkeypatch, tmp_path)

    assert cli.main(["mcp", "verify", str(checkpoint), str(artifact), "--json"]) == 0
    captured = capsys.readouterr()

    expected = mcp.verify_artifact(mcp.load(checkpoint), artifact)
    assert captured.out == mcp.dumps_verification(expected)
    assert json.loads(captured.out)["verified"] is True
    assert captured.err == ""


def test_mcp_verify_digest_mismatch_exit_one(tmp_path, monkeypatch, capsys) -> None:
    checkpoint, artifact = _fixture(tmp_path, expected_sha256="b" * 64)
    _without_policy(monkeypatch, tmp_path)

    assert cli.main(["mcp", "verify", str(checkpoint), str(artifact), "--json"]) == 1
    parsed = json.loads(capsys.readouterr().out)
    assert parsed["verified"] is False
    assert parsed["digest_matches"] is False
    assert parsed["basename_matches"] is True

    assert cli.main(["mcp", "verify", str(checkpoint), str(artifact)]) == 1
    out = capsys.readouterr().out
    assert "artefato MCP: NÃO verificado" in out
    assert "digest: diverge" in out


def test_mcp_verify_basename_mismatch_is_case_sensitive(
    tmp_path, monkeypatch, capsys
) -> None:
    checkpoint, artifact = _fixture(tmp_path, artifact_name="1.2.3.TGZ")
    _without_policy(monkeypatch, tmp_path)

    assert cli.main(["mcp", "verify", str(checkpoint), str(artifact), "--json"]) == 1
    parsed = json.loads(capsys.readouterr().out)
    assert parsed["digest_matches"] is True
    assert parsed["basename_matches"] is False
    assert parsed["artifact_basename"] == "1.2.3.TGZ"


def test_mcp_verify_unsafe_or_invalid_inputs_exit_two(
    tmp_path, monkeypatch, capsys
) -> None:
    checkpoint, artifact = _fixture(tmp_path)
    invalid_checkpoint = tmp_path / "invalido.json"
    invalid_checkpoint.write_text('{"name":"x"}', encoding="utf-8")
    _without_policy(monkeypatch, tmp_path)

    for argv in (
        ["mcp", "verify", str(checkpoint), str(artifact.parent)],
        ["mcp", "verify", str(checkpoint), str(artifact.with_name("ausente.tgz"))],
        ["mcp", "verify", str(invalid_checkpoint), str(artifact)],
        ["mcp", "verify", str(tmp_path / "ausente.json"), str(artifact), "--json"],
    ):
        assert cli.main(argv) == cli.EXIT_USO
        captured = capsys.readouterr()
        assert captured.out == ""
        assert "erro de verificação MCP" in captured.err


def test_mcp_verify_over_limit_exit_two(tmp_path, monkeypatch, capsys) -> None:
    checkpoint, artifact = _fixture(tmp_path, content=b"x" * 65)
    monkeypatch.setattr(mcp, "FPCH_MCP_ARTIFACT_MAX_BYTES", 64)
    _without_policy(monkeypatch, tmp_path)

    assert cli.main(["mcp", "verify", str(checkpoint), str(artifact)]) == cli.EXIT_USO
    assert "inseguro ou grande" in capsys.readouterr().err


def test_mcp_verify_usage_errors_exit_two(tmp_path, monkeypatch) -> None:
    _without_policy(monkeypatch, tmp_path)

    for argv in (["mcp"], ["mcp", "verify"], ["mcp", "verify", "so-um.json"]):
        with pytest.raises(SystemExit) as exc:
            cli.main(argv)
        assert exc.value.code == 2


def test_mcp_verify_does_not_use_network_or_processes(
    tmp_path, monkeypatch, capsys
) -> None:
    checkpoint, artifact = _fixture(tmp_path)
    _without_policy(monkeypatch, tmp_path)

    def forbidden(*_args, **_kwargs):
        pytest.fail("fpch mcp verify não pode acessar rede ou executar processo")

    monkeypatch.setattr(socket, "socket", forbidden)
    monkeypatch.setattr(socket, "create_connection", forbidden)
    monkeypatch.setattr(urllib.request, "urlopen", forbidden)
    monkeypatch.setattr(subprocess, "run", forbidden)
    monkeypatch.setattr(subprocess, "Popen", forbidden)

    assert cli.main(["mcp", "verify", str(checkpoint), str(artifact)]) == 0
    capsys.readouterr()
