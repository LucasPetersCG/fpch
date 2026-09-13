"""Contrato fail-closed dos checkpoints declarativos de instalação MCP."""

from __future__ import annotations

import json
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
