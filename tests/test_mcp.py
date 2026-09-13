"""Contrato fail-closed dos checkpoints declarativos de instalação MCP."""

from __future__ import annotations

import builtins
import hashlib
import json
import os
import socket
import subprocess
import urllib.request
from dataclasses import dataclass
from pathlib import Path

import pytest

from fpch import mcp


_SHA256 = "a" * 64


def _checkpoint(**changes: str) -> mcp.FpchMcpInstallCheckpoint:
    version = changes.get("version", "1.2.3")
    fields = {
        "name": "server-filesystem",
        "source": f"https://packages.example.test/mcp/server-filesystem/{version}.tgz",
        "version": version,
        "expected_sha256": _SHA256,
    }
    fields.update(changes)
    return mcp.FpchMcpInstallCheckpoint(**fields)


def test_checkpoint_json_is_canonical_and_roundtrips() -> None:
    value = _checkpoint()
    encoded = mcp.dumps(value)

    assert encoded.endswith("\n")
    assert encoded == json.dumps(
        json.loads(encoded),
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
    ) + "\n"
    assert mcp.loads(encoded) == value
    assert mcp.loads(encoded.encode("utf-8")) == value


def test_public_operations_revalidate_a_tampered_frozen_instance() -> None:
    value = _checkpoint()
    object.__setattr__(value, "expected_sha256", "invalid")

    with pytest.raises(mcp.FpchMcpError, match="expected_sha256"):
        mcp.validate(value)
    with pytest.raises(mcp.FpchMcpError, match="expected_sha256"):
        mcp.dumps(value)


def test_checkpoint_schema_rejects_subclasses() -> None:
    @dataclass(frozen=True)
    class ExtendedCheckpoint(mcp.FpchMcpInstallCheckpoint):
        extra: str = "open-schema"

    value = ExtendedCheckpoint(**_checkpoint().__dict__)
    with pytest.raises(mcp.FpchMcpError, match="deve ser"):
        mcp.validate(value)
    with pytest.raises(mcp.FpchMcpError, match="deve ser"):
        mcp.dumps(value)


@pytest.mark.parametrize(
    "version",
    ("0.0.0", "1.2.3-rc.1+build.5", "b" * 40, "c" * 64),
)
def test_version_accepts_only_exact_semver_or_full_commit(version: str) -> None:
    assert _checkpoint(version=version).version == version


@pytest.mark.parametrize(
    "name",
    ("", "-server", "Server", "server/path", "server path", "server\\path", "ação", "\U000e0001", "x" * 129),
)
def test_name_rejects_ambiguous_or_non_ascii_values(name: str) -> None:
    with pytest.raises(mcp.FpchMcpError, match="name"):
        _checkpoint(name=name)


@pytest.mark.parametrize(
    "source",
    (
        "",
        "http://packages.example.test/server.tgz",
        "file:///tmp/server.tgz",
        "https://packages.example.test",
        "https://user:secret@packages.example.test/server.tgz",
        "https://packages.example.test/server.tgz?token=secret",
        "https://packages.example.test/server.tgz#digest",
        "https://packages.example.test/a/../server.tgz",
        "https://packages.example.test/a/%2e%2e/server.tgz",
        "https://PACKAGES.example.test/mcp/server-filesystem/1.2.3.tgz",
        "https://packages.example.test./mcp/server-filesystem/1.2.3.tgz",
        "https://packages.example.test:443/mcp/server-filesystem/1.2.3.tgz",
        "https://packages .example.test/mcp/server-filesystem/1.2.3.tgz",
        "https://packages.example.test/mcp/server%2dfilesystem/1.2.3.tgz",
        "https://packages.example.test/latest/mcp/server-filesystem/1.2.3.tgz",
        "https://packages.example.test/mcp/server-filesystem/stable/1.2.3.tgz",
        "https://packages.example.test/mcp/server-filesystem/main/1.2.3.tgz",
        "https://packages.example.test/mcp/server-filesystem/HEAD/1.2.3.tgz",
        "https://packages.example.test/nightly/mcp/server-filesystem/1.2.3.tgz",
        "https://packages.example.test/next/mcp/server-filesystem/1.2.3.tgz",
        "https://packages.example.test/mcp/server-other/1.2.3.tgz",
        "https://packages.example.test/mcp/server-filesystem/1.2.4.tgz",
        "https://packages.example.test/mcp/server-filesystemish/1.2.3.tgz",
        "https://packages.example.test/mcp/server-filesystem/1.2.30.tgz",
        "https://packages.example.test/a\\server.tgz",
        "https://pacotes.exemplo.test/ação.tgz",
        "https://packages.example.test/" + "x" * 2049,
    ),
)
def test_source_rejects_mutable_ambiguous_or_secret_bearing_urls(source: str) -> None:
    with pytest.raises(mcp.FpchMcpError, match="source"):
        _checkpoint(source=source)


@pytest.mark.parametrize(
    "version",
    ("", "latest", "stable", "main", "HEAD", "v1.2.3", "1.2", "^1.2.3", "1.2.x", "d" * 39, "D" * 40),
)
def test_version_rejects_mutable_or_abbreviated_references(version: str) -> None:
    with pytest.raises(mcp.FpchMcpError, match="version"):
        _checkpoint(version=version)


def test_version_bound_guarantees_serialized_roundtrip_below_one_mib() -> None:
    with pytest.raises(mcp.FpchMcpError, match="version"):
        _checkpoint(version="1.2.3-" + "a" * mcp.FPCH_MCP_VERSION_MAX_LENGTH)

    encoded = mcp.dumps(_checkpoint(version="1.2.3-" + "a" * 240))
    assert len(encoded.encode("utf-8")) < mcp.FPCH_MCP_CHECKPOINT_MAX_BYTES
    assert mcp.loads(encoded).version.endswith("a" * 240)


@pytest.mark.parametrize(
    "digest",
    ("", "a" * 63, "a" * 65, "A" * 64, "g" * 64, "sha256:" + "a" * 64),
)
def test_expected_sha256_has_one_unambiguous_representation(digest: str) -> None:
    with pytest.raises(mcp.FpchMcpError, match="expected_sha256"):
        _checkpoint(expected_sha256=digest)


@pytest.mark.parametrize(
    ("field", "value"),
    (
        ("name", 1),
        ("source", Path("checkpoint.json")),
        ("version", None),
        ("expected_sha256", b"a" * 64),
    ),
)
def test_checkpoint_does_not_coerce_field_types(field: str, value: object) -> None:
    fields: dict[str, object] = {
        "name": "server-filesystem",
        "source": "https://packages.example.test/server.tgz",
        "version": "1.2.3",
        "expected_sha256": _SHA256,
    }
    fields[field] = value
    with pytest.raises(mcp.FpchMcpError):
        mcp.FpchMcpInstallCheckpoint(**fields)  # type: ignore[arg-type]


def test_loads_rejects_open_schema_duplicate_keys_and_non_object() -> None:
    payload = json.loads(mcp.dumps(_checkpoint()))

    for changed in (
        {**payload, "unknown": True},
        {key: value for key, value in payload.items() if key != "source"},
        [payload],
    ):
        with pytest.raises(mcp.FpchMcpError, match="campos inválidos"):
            mcp.loads(json.dumps(changed))

    with pytest.raises(mcp.FpchMcpError, match="duplicada"):
        mcp.loads(
            '{"name":"a","name":"b","source":"https://example.test/a",'
            '"version":"1.0.0","expected_sha256":"' + _SHA256 + '"}'
        )


def test_loads_rejects_invalid_utf8_unicode_and_size() -> None:
    with pytest.raises(mcp.FpchMcpError, match="UTF-8"):
        mcp.loads(b"\xff")
    with pytest.raises(mcp.FpchMcpError, match="UTF-8"):
        mcp.loads("\ud800")
    with pytest.raises(mcp.FpchMcpError, match="excede"):
        mcp.loads(b" " * (mcp.FPCH_MCP_CHECKPOINT_MAX_BYTES + 1))


def test_load_reads_regular_file_without_network_or_execution(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    checkpoint_path = tmp_path / "checkpoint.json"
    checkpoint_path.write_text(mcp.dumps(_checkpoint()), encoding="utf-8")

    def forbidden(*_args, **_kwargs):
        pytest.fail("checkpoint declarativo não pode acessar rede ou executar processo")

    monkeypatch.setattr(urllib.request, "urlopen", forbidden)
    monkeypatch.setattr(subprocess, "run", forbidden)

    assert mcp.load(checkpoint_path) == _checkpoint()


def test_load_rejects_missing_directory_and_oversized_file(tmp_path: Path) -> None:
    with pytest.raises(mcp.FpchMcpError, match="não foi possível ler"):
        mcp.load(tmp_path / "missing.json")
    with pytest.raises(mcp.FpchMcpError, match="inseguro ou grande"):
        mcp.load(tmp_path)

    oversized = tmp_path / "oversized.json"
    oversized.write_bytes(b" " * (mcp.FPCH_MCP_CHECKPOINT_MAX_BYTES + 1))
    with pytest.raises(mcp.FpchMcpError, match="inseguro ou grande"):
        mcp.load(oversized)


def test_load_rejects_final_symlink(tmp_path: Path) -> None:
    target = tmp_path / "target.json"
    target.write_text(mcp.dumps(_checkpoint()), encoding="utf-8")
    link = tmp_path / "checkpoint.json"
    try:
        link.symlink_to(target)
    except (NotImplementedError, OSError) as exc:
        pytest.skip(f"links simbólicos indisponíveis neste ambiente: {exc}")

    with pytest.raises(mcp.FpchMcpError, match="link ou reparse"):
        mcp.load(link)


def test_load_rejects_linklike_ancestor(tmp_path: Path) -> None:
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "checkpoint.json").write_text(mcp.dumps(_checkpoint()), encoding="utf-8")
    link = tmp_path / "linked"
    try:
        link.symlink_to(outside, target_is_directory=True)
    except (NotImplementedError, OSError) as exc:
        pytest.skip(f"links simbólicos indisponíveis neste ambiente: {exc}")

    with pytest.raises(mcp.FpchMcpError, match="link ou reparse"):
        mcp.load(link / "checkpoint.json")


def test_load_rejects_simulated_reparse_point(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    checkpoint_path = tmp_path / "checkpoint.json"
    checkpoint_path.write_text(mcp.dumps(_checkpoint()), encoding="utf-8")
    real_is_linklike = mcp._is_linklike
    monkeypatch.setattr(
        mcp,
        "_is_linklike",
        lambda path: Path(path) == checkpoint_path or real_is_linklike(Path(path)),
    )

    with pytest.raises(mcp.FpchMcpError, match="link ou reparse"):
        mcp.load(checkpoint_path)


# --- C7: verificação offline de artefato local contra checkpoint -------------


def _artifact(
    tmp_path: Path, content: bytes = b"conteudo do pacote", name: str = "1.2.3.tgz"
) -> tuple[mcp.FpchMcpInstallCheckpoint, Path]:
    folder = tmp_path / "artefatos"
    folder.mkdir(exist_ok=True)
    artifact = folder / name
    artifact.write_bytes(content)
    checkpoint = _checkpoint(expected_sha256=hashlib.sha256(content).hexdigest())
    return checkpoint, artifact


def test_verify_artifact_matches_digest_and_basename(tmp_path: Path) -> None:
    checkpoint, artifact = _artifact(tmp_path)

    result = mcp.verify_artifact(checkpoint, artifact)

    assert result.verified is True
    assert result.digest_matches is True
    assert result.basename_matches is True
    assert result.actual_sha256 == checkpoint.expected_sha256
    assert result.size_bytes == len(b"conteudo do pacote")
    assert result.artifact_basename == "1.2.3.tgz"
    assert result.expected_basename == "1.2.3.tgz"
    assert (result.name, result.source, result.version) == (
        checkpoint.name,
        checkpoint.source,
        checkpoint.version,
    )
    assert mcp.verify_artifact(checkpoint, str(artifact)) == result


def test_verify_artifact_json_is_canonical(tmp_path: Path) -> None:
    checkpoint, artifact = _artifact(tmp_path)
    encoded = mcp.dumps_verification(mcp.verify_artifact(checkpoint, artifact))

    assert encoded.endswith("\n")
    parsed = json.loads(encoded)
    assert encoded == json.dumps(
        parsed, ensure_ascii=True, sort_keys=True, separators=(",", ":")
    ) + "\n"
    assert parsed["verified"] is True
    assert set(parsed) == {
        "actual_sha256", "artifact_basename", "basename_matches", "digest_matches",
        "expected_basename", "expected_sha256", "name", "size_bytes", "source",
        "verified", "version",
    }
    with pytest.raises(mcp.FpchMcpError, match="deve ser"):
        mcp.dumps_verification(parsed)  # type: ignore[arg-type]


def test_verify_artifact_digest_mismatch_returns_unverified(tmp_path: Path) -> None:
    checkpoint, artifact = _artifact(tmp_path)
    tampered = _checkpoint(expected_sha256="b" * 64)

    result = mcp.verify_artifact(tampered, artifact)

    assert result.verified is False
    assert result.digest_matches is False
    assert result.basename_matches is True
    assert result.actual_sha256 == checkpoint.expected_sha256


@pytest.mark.parametrize("name", ("outro.tgz", "1.2.3.TGZ", "1.2.3.tgz.bak", "x1.2.3.tgz"))
def test_verify_artifact_basename_is_exact_and_case_sensitive(
    tmp_path: Path, name: str
) -> None:
    checkpoint, artifact = _artifact(tmp_path, name=name)

    result = mcp.verify_artifact(checkpoint, artifact)

    assert result.verified is False
    assert result.digest_matches is True
    assert result.basename_matches is False
    assert result.artifact_basename == name


def test_verification_result_rejects_inconsistent_verified_flag() -> None:
    fields = dict(
        name="server-filesystem",
        source="https://packages.example.test/mcp/server-filesystem/1.2.3.tgz",
        version="1.2.3",
        expected_sha256=_SHA256,
        actual_sha256="b" * 64,
        size_bytes=0,
        artifact_basename="1.2.3.tgz",
        expected_basename="1.2.3.tgz",
        digest_matches=False,
        basename_matches=True,
    )
    with pytest.raises(mcp.FpchMcpError, match="inconsistente"):
        mcp.FpchMcpArtifactVerification(**fields, verified=True)


def test_verify_artifact_accepts_empty_file(tmp_path: Path) -> None:
    checkpoint, artifact = _artifact(tmp_path, content=b"")

    result = mcp.verify_artifact(checkpoint, artifact)

    assert result.verified is True
    assert result.size_bytes == 0
    assert result.actual_sha256 == (
        "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855"
    )


def test_verify_artifact_limit_is_inclusive_and_over_limit_raises(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(mcp, "FPCH_MCP_ARTIFACT_MAX_BYTES", 300)
    monkeypatch.setattr(mcp, "_READ_CHUNK_BYTES", 64)
    checkpoint, artifact = _artifact(tmp_path, content=b"x" * 300)

    assert mcp.verify_artifact(checkpoint, artifact).verified is True

    artifact.write_bytes(b"x" * 301)
    with pytest.raises(mcp.FpchMcpError, match="inseguro ou grande"):
        mcp.verify_artifact(checkpoint, artifact)


def test_verify_artifact_detects_growth_beyond_limit_during_read(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(mcp, "FPCH_MCP_ARTIFACT_MAX_BYTES", 300)
    checkpoint, artifact = _artifact(tmp_path, content=b"x" * 300)
    real_read = mcp.os.read

    def read_then_grow(descriptor: int, count: int) -> bytes:
        with artifact.open("ab") as stream:
            stream.write(b"y" * 10)
        return real_read(descriptor, count)

    monkeypatch.setattr(mcp.os, "read", read_then_grow)
    with pytest.raises(mcp.FpchMcpError, match="mudou ou excede"):
        mcp.verify_artifact(checkpoint, artifact)


def test_verify_artifact_detects_same_size_rewrite_during_read(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    checkpoint, artifact = _artifact(tmp_path, content=b"a" * 1000)
    original_mtime_ns = artifact.stat().st_mtime_ns
    real_read = mcp.os.read
    calls = {"count": 0}

    def read_then_rewrite(descriptor: int, count: int) -> bytes:
        calls["count"] += 1
        chunk = real_read(descriptor, count)
        if calls["count"] == 1:
            with artifact.open("r+b") as stream:
                stream.write(b"b" * 1000)
            os.utime(
                artifact,
                ns=(original_mtime_ns, original_mtime_ns + 5_000_000_000),
            )
        return chunk

    monkeypatch.setattr(mcp.os, "read", read_then_rewrite)
    with pytest.raises(mcp.FpchMcpError, match="mudou ou excede"):
        mcp.verify_artifact(checkpoint, artifact)


def test_verify_artifact_rejects_missing_directory_and_non_path(tmp_path: Path) -> None:
    checkpoint, artifact = _artifact(tmp_path)

    with pytest.raises(mcp.FpchMcpError, match="não foi possível ler artefato"):
        mcp.verify_artifact(checkpoint, artifact.with_name("ausente.tgz"))
    with pytest.raises(mcp.FpchMcpError, match="inseguro ou grande"):
        mcp.verify_artifact(checkpoint, artifact.parent)
    with pytest.raises(mcp.FpchMcpError, match="caminho de artefato"):
        mcp.verify_artifact(checkpoint, 123)  # type: ignore[arg-type]


def test_verify_artifact_revalidates_checkpoint_before_io(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    checkpoint, artifact = _artifact(tmp_path)
    object.__setattr__(checkpoint, "expected_sha256", "invalid")

    def forbidden(*_args, **_kwargs):
        pytest.fail("checkpoint inválido não pode chegar ao disco")

    monkeypatch.setattr(mcp, "_read_guarded", forbidden)
    with pytest.raises(mcp.FpchMcpError, match="expected_sha256"):
        mcp.verify_artifact(checkpoint, artifact)
    with pytest.raises(mcp.FpchMcpError, match="deve ser"):
        mcp.verify_artifact(object(), artifact)  # type: ignore[arg-type]


def test_verify_artifact_never_uses_network_execution_or_writes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    checkpoint, artifact = _artifact(tmp_path)
    before = sorted((path, path.stat().st_mtime_ns) for path in tmp_path.rglob("*"))

    def forbidden(*_args, **_kwargs):
        pytest.fail("verificação offline não pode acessar rede, executar ou abrir via open")

    real_os_open = os.open
    writable = os.O_WRONLY | os.O_RDWR | os.O_CREAT | os.O_TRUNC | os.O_APPEND
    opened_flags: list[int] = []

    def read_only_open(path, flags, *args, **kwargs):
        opened_flags.append(flags)
        return real_os_open(path, flags, *args, **kwargs)

    monkeypatch.setattr(socket, "socket", forbidden)
    monkeypatch.setattr(socket, "create_connection", forbidden)
    monkeypatch.setattr(urllib.request, "urlopen", forbidden)
    monkeypatch.setattr(subprocess, "run", forbidden)
    monkeypatch.setattr(subprocess, "Popen", forbidden)
    monkeypatch.setattr(builtins, "open", forbidden)
    monkeypatch.setattr(os, "open", read_only_open)

    verified = mcp.verify_artifact(checkpoint, artifact).verified
    monkeypatch.undo()

    assert verified is True
    assert opened_flags and all(not flags & writable for flags in opened_flags)
    assert sorted((path, path.stat().st_mtime_ns) for path in tmp_path.rglob("*")) == before


def test_verify_artifact_rejects_final_symlink(tmp_path: Path) -> None:
    checkpoint, target = _artifact(tmp_path, name="real.tgz")
    link = tmp_path / "1.2.3.tgz"
    try:
        link.symlink_to(target)
    except (NotImplementedError, OSError) as exc:
        pytest.skip(f"links simbólicos indisponíveis neste ambiente: {exc}")

    with pytest.raises(mcp.FpchMcpError, match="link ou reparse"):
        mcp.verify_artifact(checkpoint, link)


def test_verify_artifact_rejects_symlinked_ancestor(tmp_path: Path) -> None:
    checkpoint, artifact = _artifact(tmp_path)
    link = tmp_path / "linked"
    try:
        link.symlink_to(artifact.parent, target_is_directory=True)
    except (NotImplementedError, OSError) as exc:
        pytest.skip(f"links simbólicos indisponíveis neste ambiente: {exc}")

    with pytest.raises(mcp.FpchMcpError, match="link ou reparse"):
        mcp.verify_artifact(checkpoint, link / artifact.name)


def test_verify_artifact_rejects_junction_ancestor(tmp_path: Path) -> None:
    try:
        import _winapi
    except ImportError:
        pytest.skip("junctions existem apenas no Windows")
    checkpoint, artifact = _artifact(tmp_path)
    junction = tmp_path / "juncao"
    try:
        _winapi.CreateJunction(str(artifact.parent), str(junction))
    except (AttributeError, OSError) as exc:
        pytest.skip(f"junction indisponível neste ambiente: {exc}")

    try:
        assert (junction / artifact.name).is_file()
        with pytest.raises(mcp.FpchMcpError, match="link ou reparse"):
            mcp.verify_artifact(checkpoint, junction / artifact.name)
    finally:
        os.rmdir(junction)


def test_verify_artifact_rejects_simulated_reparse_point(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    checkpoint, artifact = _artifact(tmp_path)
    real_is_linklike = mcp._is_linklike
    monkeypatch.setattr(
        mcp,
        "_is_linklike",
        lambda path: Path(path) == artifact.parent or real_is_linklike(Path(path)),
    )

    with pytest.raises(mcp.FpchMcpError, match="link ou reparse"):
        mcp.verify_artifact(checkpoint, artifact)


# --- C7: endurecimento pós-revisão --------------------------------------------


def test_verify_artifact_detects_growth_that_stays_under_limit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    checkpoint, artifact = _artifact(tmp_path, content=b"x" * 100)
    real_read = mcp.os.read
    calls = {"count": 0}

    def read_after_single_growth(descriptor: int, count: int) -> bytes:
        calls["count"] += 1
        if calls["count"] == 1:
            with artifact.open("ab") as stream:
                stream.write(b"y" * 10)
        return real_read(descriptor, count)

    monkeypatch.setattr(mcp.os, "read", read_after_single_growth)
    with pytest.raises(mcp.FpchMcpError, match="mudou ou excede"):
        mcp.verify_artifact(checkpoint, artifact)
    assert artifact.stat().st_size < mcp.FPCH_MCP_ARTIFACT_MAX_BYTES


def test_verify_artifact_accepts_relative_path_and_checks_cwd_ancestors(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    checkpoint, artifact = _artifact(tmp_path)
    monkeypatch.chdir(artifact.parent)

    result = mcp.verify_artifact(checkpoint, "1.2.3.tgz")
    assert result.verified is True
    assert result.artifact_basename == "1.2.3.tgz"

    real_is_linklike = mcp._is_linklike
    monkeypatch.setattr(
        mcp,
        "_is_linklike",
        lambda path: Path(path) == tmp_path or real_is_linklike(Path(path)),
    )
    with pytest.raises(mcp.FpchMcpError, match="link ou reparse"):
        mcp.verify_artifact(checkpoint, "1.2.3.tgz")
    with pytest.raises(mcp.FpchMcpError, match="link ou reparse"):
        mcp.load("1.2.3.tgz")


@pytest.mark.parametrize("bad", ("art\x00efato.tgz", "\ud800.tgz"))
def test_malformed_path_is_fpch_error(tmp_path: Path, bad: str) -> None:
    checkpoint, _artifact_path = _artifact(tmp_path)

    with pytest.raises(mcp.FpchMcpError):
        mcp.verify_artifact(checkpoint, tmp_path / bad)
    with pytest.raises(mcp.FpchMcpError):
        mcp.load(tmp_path / bad)


def test_forged_verification_raises_and_tampering_is_caught_on_dumps(
    tmp_path: Path,
) -> None:
    checkpoint, artifact = _artifact(tmp_path)
    result = mcp.verify_artifact(checkpoint, artifact)
    fields = result.to_dict()

    forgeries = (
        {"actual_sha256": "c" * 64},  # digest_matches=True mentiroso
        {"artifact_basename": "outro.tgz"},  # basename_matches=True mentiroso
        {"expected_basename": "outro.tgz", "basename_matches": False, "verified": False},
        {"size_bytes": -1},
        {"size_bytes": True},
        {"verified": 1},
        {"actual_sha256": "C" * 64},
        {"artifact_basename": "../1.2.3.tgz"},
    )
    for change in forgeries:
        with pytest.raises(mcp.FpchMcpError):
            mcp.FpchMcpArtifactVerification(**{**fields, **change})

    object.__setattr__(result, "actual_sha256", "c" * 64)
    with pytest.raises(mcp.FpchMcpError, match="inconsistente"):
        mcp.dumps_verification(result)


def test_verify_artifact_detects_swap_between_lstat_and_open(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    checkpoint, artifact = _artifact(tmp_path, content=b"original")
    real_open = mcp.os.open

    def swap_then_open(path, flags, *args, **kwargs):
        artifact.unlink()
        artifact.write_bytes(b"trocado!")
        return real_open(path, flags, *args, **kwargs)

    monkeypatch.setattr(mcp.os, "open", swap_then_open)
    with pytest.raises(mcp.FpchMcpError, match="mudou durante abertura"):
        mcp.verify_artifact(checkpoint, artifact)


def test_verify_artifact_rechecks_ancestors_after_read(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    checkpoint, artifact = _artifact(tmp_path)
    state = {"eof": False}
    real_read = mcp.os.read
    real_is_linklike = mcp._is_linklike

    def read_marking_eof(descriptor: int, count: int) -> bytes:
        chunk = real_read(descriptor, count)
        if not chunk:
            state["eof"] = True
        return chunk

    def linklike_after_read(path: Path) -> bool:
        if state["eof"] and Path(path) == artifact.parent:
            return True
        return real_is_linklike(Path(path))

    monkeypatch.setattr(mcp.os, "read", read_marking_eof)
    monkeypatch.setattr(mcp, "_is_linklike", linklike_after_read)
    with pytest.raises(mcp.FpchMcpError, match="link ou reparse"):
        mcp.verify_artifact(checkpoint, artifact)
    assert state["eof"] is True


def test_verify_artifact_uses_on_disk_name_on_case_insensitive_fs(
    tmp_path: Path,
) -> None:
    checkpoint, stored = _artifact(tmp_path, name="1.2.3.TGZ")
    typed = stored.with_name("1.2.3.tgz")
    if not typed.exists():
        pytest.skip("sistema de arquivos diferencia maiúsculas de minúsculas")

    result = mcp.verify_artifact(checkpoint, typed)

    assert result.artifact_basename == "1.2.3.TGZ"
    assert result.digest_matches is True
    assert result.basename_matches is False
    assert result.verified is False


def test_verify_artifact_fails_closed_without_unique_directory_entry(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    checkpoint, artifact = _artifact(tmp_path)
    real_scandir = mcp.os.scandir

    class DuplicatedEntries:
        def __init__(self, entries: list) -> None:
            self._entries = entries

        def __enter__(self) -> list:
            return self._entries

        def __exit__(self, *_exc: object) -> None:
            return None

    def scandir_with(transform):
        def fake(path):
            with real_scandir(path) as entries:
                return DuplicatedEntries(transform(list(entries)))

        return fake

    monkeypatch.setattr(mcp.os, "scandir", scandir_with(lambda entries: entries * 2))
    with pytest.raises(mcp.FpchMcpError, match="sem ambiguidade"):
        mcp.verify_artifact(checkpoint, artifact)

    monkeypatch.setattr(mcp.os, "scandir", scandir_with(lambda _entries: []))
    with pytest.raises(mcp.FpchMcpError, match="sem ambiguidade"):
        mcp.verify_artifact(checkpoint, artifact)
