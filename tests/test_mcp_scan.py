"""Varredura offline e determinística de Unicode oculto em metadados MCP."""

from __future__ import annotations

import builtins
import dataclasses
import hashlib
import json
import os
import socket
import subprocess
import time
import unicodedata
import urllib.request
from pathlib import Path

import pytest

from fpch import mcp


def _text(value: str) -> mcp.FpchMcpMetadataScan:
    return mcp.scan_bytes(value.encode("utf-8"), format="text")


def _json(value: str | bytes) -> mcp.FpchMcpMetadataScan:
    raw = value if isinstance(value, bytes) else value.encode("utf-8")
    return mcp.scan_bytes(raw, format="json")


def _categories(scan: mcp.FpchMcpMetadataScan) -> list[str]:
    return [finding.category for finding in scan.findings]


# --- Classificação ------------------------------------------------------------


@pytest.mark.parametrize(
    ("cp", "category"),
    [
        (0xE0000, "tag"),
        (0xE0001, "tag"),
        (0xE0041, "tag"),
        (0xE007F, "tag"),
        (0x202A, "bidi"),
        (0x202E, "bidi"),
        (0x2066, "bidi"),
        (0x2069, "bidi"),
        (0x200E, "bidi"),
        (0x200F, "bidi"),
        (0x061C, "bidi"),
        (0x200B, "zero_width"),
        (0x200D, "zero_width"),
        (0x2060, "zero_width"),
        (0xFEFF, "zero_width"),
        (0xFE00, "variation_selector"),
        (0xFE0F, "variation_selector"),
        (0xE0100, "variation_selector"),
        (0xE01EF, "variation_selector"),
        (0x180B, "variation_selector"),
        (0x180D, "variation_selector"),
        (0x180F, "variation_selector"),
        (0x0000, "control"),
        (0x0008, "control"),
        (0x000B, "control"),
        (0x001B, "control"),
        (0x001F, "control"),
        (0x007F, "control"),
        (0x0080, "control"),
        (0x0085, "control"),
        (0x009F, "control"),
        (0x2028, "line_separator"),
        (0x2029, "line_separator"),
        (0x034F, "invisible_filler"),
        (0x115F, "invisible_filler"),
        (0x1160, "invisible_filler"),
        (0x17B4, "invisible_filler"),
        (0x17B5, "invisible_filler"),
        (0x2800, "invisible_filler"),
        (0x3164, "invisible_filler"),
        (0xFFA0, "invisible_filler"),
        (0xFDD0, "noncharacter"),
        (0xFDEF, "noncharacter"),
        (0xFFFE, "noncharacter"),
        (0xFFFF, "noncharacter"),
        (0x1FFFE, "noncharacter"),
        (0x10FFFF, "noncharacter"),
        (0xD800, "surrogate"),
        (0xDFFF, "surrogate"),
        (0xE000, "private_use"),
        (0xF8FF, "private_use"),
        (0xF0000, "private_use"),
        (0x00AD, "other_format"),
        (0x2061, "other_format"),
        (0x2064, "other_format"),
        (0xFFF9, "other_format"),
        (0xFFFB, "other_format"),
        (0x1D173, "other_format"),
        (0x1D17A, "other_format"),
    ],
)
def test_classify_code_point_boundaries(cp: int, category: str) -> None:
    assert mcp.classify_code_point(cp) == category


def test_classify_mongolian_vowel_separator_follows_unicodedata() -> None:
    expected = "other_format" if unicodedata.category("᠎") == "Cf" else None
    assert mcp.classify_code_point(0x180E) == expected


def test_classify_unassigned_follows_unicodedata() -> None:
    expected = "unassigned" if unicodedata.category("͸") == "Cn" else None
    assert expected == "unassigned"
    assert mcp.classify_code_point(0x0378) == expected


@pytest.mark.parametrize(
    "cp", [0x09, 0x0A, 0x0D, 0x20, 0x41, 0x7E, 0xA0, 0x202F, 0xFE10, 0xE7, 0xE3, 0x1F44D, 0xFFFC, 0xFFFD]
)
def test_classify_code_point_clean(cp: int) -> None:
    assert mcp.classify_code_point(cp) is None


@pytest.mark.parametrize("cp", [-1, 0x110000, True, "A", 65.0, None])
def test_classify_code_point_rejects_invalid_input(cp: object) -> None:
    with pytest.raises(mcp.FpchMcpError):
        mcp.classify_code_point(cp)  # type: ignore[arg-type]


def test_scan_classes_are_frozen() -> None:
    assert mcp.FPCH_MCP_SCAN_CLASSES == (
        "tag",
        "bidi",
        "zero_width",
        "variation_selector",
        "control",
        "line_separator",
        "invisible_filler",
        "noncharacter",
        "surrogate",
        "private_use",
        "other_format",
        "unassigned",
    )
    assert mcp.FPCH_MCP_SCAN_RULESET == "fpch-hidden-unicode-1"
    assert mcp.FPCH_MCP_METADATA_MAX_BYTES == 1024 * 1024
    assert mcp.FPCH_MCP_SCAN_MAX_FINDINGS == 100


# --- Modo texto -----------------------------------------------------------------


def test_text_clean_portuguese_and_typographic_spaces() -> None:
    scan = _text("ação e informação — 10 %︐ fim\tok\r\nlinha\n")
    assert scan.clean is True
    assert scan.findings_total == 0
    assert scan.counts == ()
    assert scan.findings == ()
    assert scan.truncated is False
    assert scan.format == "text"
    assert scan.leading_bom is False


def test_text_lines_and_columns_after_multibyte_characters() -> None:
    scan = _text("ação​\r\nçã\tx‮\nlinha\n\n\U000E0041")
    positions = [(f.line, f.column, f.category) for f in scan.findings]
    assert positions == [
        (1, 5, "zero_width"),
        (2, 5, "bidi"),
        (5, 1, "tag"),
    ]
    first = scan.findings[0]
    assert (first.pointer, first.in_key, first.index) == (None, None, None)


def test_text_lone_carriage_return_does_not_advance_line() -> None:
    scan = _text("a\rb​")
    assert [(f.line, f.column) for f in scan.findings] == [(1, 4)]


def test_text_counts_follow_class_order_not_document_order() -> None:
    scan = _text("​\U000E0041‮​")
    assert scan.counts == (("tag", 1), ("bidi", 1), ("zero_width", 2))
    assert _categories(scan) == ["zero_width", "tag", "bidi", "zero_width"]


def test_leading_bom_is_allowed_once_in_text() -> None:
    scan = _text("﻿ação")
    assert scan.leading_bom is True
    assert scan.clean is True
    assert scan.size_bytes == len("﻿ação".encode("utf-8"))

    doubled = _text("﻿﻿x")
    assert doubled.leading_bom is True
    assert [(f.category, f.line, f.column) for f in doubled.findings] == [
        ("zero_width", 1, 1)
    ]

    later = _text("x﻿")
    assert later.leading_bom is False
    assert _categories(later) == ["zero_width"]


def test_emoji_have_no_allowance() -> None:
    assert _text("👍").clean is True
    assert _categories(_text("👨‍💻")) == ["zero_width"]
    assert _categories(_text("❤️")) == ["variation_selector"]
    england = "\U0001F3F4\U000E0067\U000E0062\U000E0065\U000E006E\U000E0067\U000E007F"
    scan = _text(england)
    assert scan.counts == (("tag", 6),)
    assert _categories(scan) == ["tag"] * 6


def test_tag_payload_is_never_spelled_in_output() -> None:
    payload = "".join(chr(0xE0000 + ord(c)) for c in "ignore previous")
    scan = _text(f"desc{payload}")
    serialized = mcp.dumps_scan(scan)
    assert "ignore" not in serialized
    assert "previous" not in serialized
    assert scan.findings_total == len("ignore previous")


def test_invalid_utf8_and_utf16_are_rejected() -> None:
    with pytest.raises(mcp.FpchMcpError, match="não são UTF-8 válido"):
        mcp.scan_bytes(b"\xff\xfeo\x00i\x00", format="text")
    with pytest.raises(mcp.FpchMcpError, match="não são UTF-8 válido"):
        mcp.scan_bytes(b"abc\xc3", format="json")
    with pytest.raises(mcp.FpchMcpError, match="não são UTF-8 válido"):
        mcp.scan_bytes("\ud800".encode("utf-8", "surrogatepass"), format="text")


def test_scan_bytes_rejects_bad_arguments() -> None:
    with pytest.raises(mcp.FpchMcpError):
        mcp.scan_bytes("texto", format="text")  # type: ignore[arg-type]
    with pytest.raises(mcp.FpchMcpError):
        mcp.scan_bytes(b"x", format="auto")  # type: ignore[arg-type]
    with pytest.raises(mcp.FpchMcpError):
        mcp.scan_bytes(b"x", format="TEXT")  # type: ignore[arg-type]


# --- Modo JSON ------------------------------------------------------------------


def test_json_escaped_tag_pair_in_ascii_file() -> None:
    raw = b'{"tools":[{"name":"ok","description":"leia\\udb40\\udc41"}]}'
    assert raw.isascii()
    scan = _json(raw)
    assert scan.counts == (("tag", 1),)
    finding = scan.findings[0]
    assert finding.code_point == 0xE0041
    assert finding.pointer == "/tools/0/description"
    assert finding.in_key is False
    assert finding.index == 4
    assert (finding.line, finding.column) == (None, None)
    assert finding.to_dict()["code_point"] == "U+E0041"


def test_json_key_hit_and_pointer_escaping() -> None:
    scan = _json('{"a~b/c​": {"x": ["", "ok", "z‮"]}}')
    assert [(f.category, f.pointer, f.in_key, f.index) for f in scan.findings] == [
        ("zero_width", "/a~0b~1c\ufffd", True, 5),
        ("bidi", "/a~0b~1c\ufffd/x/2", False, 1),
    ]


def test_json_findings_follow_document_order() -> None:
    scan = _json('{"b": "​", "a": ["‮", {"k⁠": "\U000E0001"}]}')
    assert [(f.pointer, f.in_key) for f in scan.findings] == [
        ("/b", False),
        ("/a/0", False),
        ("/a/1/k\ufffd", True),
        ("/a/1/k\ufffd", False),
    ]


def test_json_root_string_and_scalars() -> None:
    scan = _json('"x\\u200b"')
    assert [(f.pointer, f.in_key, f.index) for f in scan.findings] == [("", False, 1)]
    assert _json("42").clean is True
    assert _json("null").clean is True


def test_json_lone_surrogate_escape_is_flagged_and_output_is_ascii() -> None:
    scan = _json(b'{"k":"a\\ud800"}')
    assert _categories(scan) == ["surrogate"]
    assert scan.findings[0].to_dict()["code_point"] == "U+D800"
    serialized = mcp.dumps_scan(scan)
    assert serialized.isascii()
    json.loads(serialized)


def test_json_raw_hidden_characters_and_numbers_are_not_confused() -> None:
    scan = _json('{"n": 1.5e3, "t": true, "s": "ação"}')
    assert [(f.category, f.pointer, f.index) for f in scan.findings] == [
        ("control", "/s", 4)
    ]


def test_leading_bom_is_allowed_once_in_json() -> None:
    scan = _json('﻿{"a": "b"}')
    assert scan.leading_bom is True
    assert scan.clean is True
    inner = _json('﻿{"a": "﻿"}')
    assert _categories(inner) == ["zero_width"]
    with pytest.raises(mcp.FpchMcpError, match="JSON de metadados MCP inválido"):
        _json('﻿﻿{"a": "b"}')


@pytest.mark.parametrize(
    ("raw", "message"),
    [
        ('{"a": ', "inválido"),
        ("", "inválido"),
        ('{"a": 1}{', "inválido"),
        ('{"a": 1, "a": 2}', "chave duplicada"),
        ('{"a": 1, "\\u0061": 2}', "chave duplicada"),
        ('{"a": NaN}', "constante não finita"),
        ("[Infinity]", "constante não finita"),
        ("[-Infinity]", "constante não finita"),
        ("[" * 257 + "]" * 257, "profundidade"),
        ("[" * 100_000 + "]" * 100_000, "inválido|profundidade"),
        ('{"a": "x\ny"}', "inválido"),
    ],
    ids=[
        "truncado",
        "vazio",
        "lixo-final",
        "chave-duplicada",
        "chave-duplicada-escapada",
        "nan",
        "infinity",
        "menos-infinity",
        "profundidade-257",
        "profundidade-100000",
        "controle-cru-em-string",
    ],
)
def test_json_errors_raise(raw: str, message: str) -> None:
    with pytest.raises(mcp.FpchMcpError, match=message):
        _json(raw)


def test_json_depth_limit_is_inclusive() -> None:
    assert _json("[" * 256 + "]" * 256).clean is True
    nested = '{"a":' * 255 + '"​"' + "}" * 255
    scan = _json(nested)
    assert scan.findings[0].pointer == "/a" * 255


# --- Limite de achados ----------------------------------------------------------


def test_findings_are_capped_but_totals_are_exact() -> None:
    scan = _text("x​" * 150)
    assert scan.findings_total == 150
    assert len(scan.findings) == 100
    assert scan.truncated is True
    assert scan.clean is False
    assert scan.counts == (("zero_width", 150),)
    assert scan.findings[-1].column == 200


def test_json_findings_are_capped_but_totals_are_exact() -> None:
    scan = _json(json.dumps({"k": ["‮"] * 120 + ["\U000E0041"] * 30}))
    assert scan.findings_total == 150
    assert len(scan.findings) == 100
    assert scan.truncated is True
    assert scan.counts == (("tag", 30), ("bidi", 120))
    assert _categories(scan) == ["bidi"] * 100


def test_exactly_cap_is_not_truncated() -> None:
    scan = _text("​" * 100)
    assert (scan.findings_total, len(scan.findings), scan.truncated) == (100, 100, False)


# --- Serialização e instâncias forjadas ------------------------------------------


def test_dumps_scan_is_canonical_deterministic_and_ascii() -> None:
    raw = "ação​\n\U000E0041".encode("utf-8")
    first = mcp.dumps_scan(mcp.scan_bytes(raw, format="text"))
    second = mcp.dumps_scan(mcp.scan_bytes(raw, format="text"))
    assert first == second
    assert first.isascii()
    assert first.endswith("\n")
    payload = json.loads(first)
    assert first == json.dumps(payload, ensure_ascii=True, sort_keys=True, separators=(",", ":")) + "\n"
    assert payload["sha256"] == hashlib.sha256(raw).hexdigest()
    assert payload["size_bytes"] == len(raw)
    assert payload["ruleset"] == mcp.FPCH_MCP_SCAN_RULESET
    assert payload["unicode_version"] == unicodedata.unidata_version
    assert payload["counts"] == {"tag": 1, "zero_width": 1}
    assert payload["findings"][0] == {
        "category": "zero_width",
        "code_point": "U+200B",
        "column": 5,
        "escaped": False,
        "in_key": None,
        "index": None,
        "line": 1,
        "pointer": None,
        "pointer_truncated": False,
    }
    assert "basename" not in first and "path" not in first


def test_code_point_hex_has_at_least_four_uppercase_digits() -> None:
    scan = _text("\x1b‮\U000E007F")
    assert [f.to_dict()["code_point"] for f in scan.findings] == [
        "U+001B",
        "U+202E",
        "U+E007F",
    ]


def _clean_scan_fields() -> dict[str, object]:
    return {
        field.name: getattr(_text("ok"), field.name)
        for field in dataclasses.fields(mcp.FpchMcpMetadataScan)
    }


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("clean", False),
        ("truncated", True),
        ("findings_total", 1),
        ("counts", (("tag", 1),)),
        ("format", "yaml"),
        ("sha256", "A" * 64),
        ("size_bytes", -1),
        ("size_bytes", True),
        ("leading_bom", 1),
        ("ruleset", "outro"),
        ("unicode_version", "0.0.0"),
        ("findings", []),
    ],
)
def test_forged_clean_scan_raises(field: str, value: object) -> None:
    fields = _clean_scan_fields()
    fields[field] = value
    with pytest.raises(mcp.FpchMcpError):
        mcp.FpchMcpMetadataScan(**fields)  # type: ignore[arg-type]


def test_forged_dirty_scan_inconsistencies_raise() -> None:
    real = _text("​‮")
    base = {field.name: getattr(real, field.name) for field in dataclasses.fields(real)}
    forgeries = [
        {"clean": True},
        {"counts": (("zero_width", 1), ("bidi", 1))},
        {"counts": (("bidi", 1), ("zero_width", 2))},
        {"counts": (("bidi", 2),)},
        {"findings": tuple(reversed(real.findings))},
        {"findings": real.findings[:1]},
        {"format": "json"},
    ]
    for change in forgeries:
        with pytest.raises(mcp.FpchMcpError):
            mcp.FpchMcpMetadataScan(**{**base, **change})  # type: ignore[arg-type]


@pytest.mark.parametrize(
    "change",
    [
        {"category": "tag"},
        {"code_point": 0x41, "category": None},
        {"line": 0},
        {"line": 1, "pointer": "/a"},
        {"line": None, "column": None, "pointer": "a", "in_key": False, "index": 0},
        {"line": None, "column": None, "pointer": "/~2", "in_key": False, "index": 0},
        {"line": None, "column": None, "pointer": "", "in_key": True, "index": 0},
        {"line": None, "column": None, "pointer": "/a", "in_key": 0, "index": 0},
        {"line": None, "column": None, "pointer": "/a", "in_key": False, "index": -1},
        {"escaped": 1},
        {"escaped": None},
        {"pointer_truncated": True},
        {"pointer_truncated": None},
        {"line": None, "column": None, "pointer": "/a", "in_key": False, "index": 0,
         "escaped": True},
        {"line": None, "column": None, "pointer": "/a\u200b", "in_key": False,
         "index": 0},
        {"line": None, "column": None, "pointer": "/a", "in_key": False, "index": 0,
         "pointer_truncated": True},
        {"line": None, "column": None, "pointer": "/~\u2026", "in_key": False,
         "index": 0, "pointer_truncated": True},
        {"line": None, "column": None, "in_key": False, "index": 0,
         "pointer": "/" + "a" * mcp.FPCH_MCP_SCAN_POINTER_MAX_CHARS},
    ],
)
def test_forged_finding_raises(change: dict[str, object]) -> None:
    base = dataclasses.asdict(_text("​").findings[0])
    with pytest.raises(mcp.FpchMcpError):
        mcp.FpchMcpUnicodeFinding(**{**base, **change})  # type: ignore[arg-type]


def test_dumps_scan_catches_tampering() -> None:
    scan = _text("​")
    object.__setattr__(scan, "clean", True)
    with pytest.raises(mcp.FpchMcpError):
        mcp.dumps_scan(scan)

    other = _text("​")
    object.__setattr__(other.findings[0], "category", "tag")
    with pytest.raises(mcp.FpchMcpError):
        mcp.dumps_scan(other)

    third = _text("​")
    object.__setattr__(third, "sha256", "0" * 63)
    with pytest.raises(mcp.FpchMcpError):
        mcp.dumps_scan(third)

    with pytest.raises(mcp.FpchMcpError):
        mcp.dumps_scan(dataclasses.asdict(_text("ok")))  # type: ignore[arg-type]


# --- Leitura de arquivo -----------------------------------------------------------


def test_scan_metadata_auto_format_and_override(tmp_path: Path) -> None:
    content = '{"a": "\\u200b"}'
    upper = tmp_path / "tools.JSON"
    upper.write_bytes(content.encode("utf-8"))
    as_json = mcp.scan_metadata(upper)
    assert as_json.format == "json"
    assert as_json.findings[0].pointer == "/a"

    as_text = mcp.scan_metadata(upper, format="text")
    assert as_text.format == "text"
    # Modo texto também reporta o escape ASCII, marcado como escapado.
    assert as_text.clean is False
    assert [
        (f.category, f.code_point, f.line, f.column, f.escaped)
        for f in as_text.findings
    ] == [("zero_width", 0x200B, 1, 8, True)]

    txt = tmp_path / "tools.txt"
    txt.write_bytes(content.encode("utf-8"))
    assert mcp.scan_metadata(txt).format == "text"
    assert mcp.scan_metadata(txt, format="json").findings_total == 1

    with pytest.raises(mcp.FpchMcpError, match="format"):
        mcp.scan_metadata(txt, format="yaml")  # type: ignore[arg-type]


def test_scan_metadata_matches_scan_bytes(tmp_path: Path) -> None:
    raw = "﻿ação​\r\n\U000E0041".encode("utf-8")
    path = tmp_path / "desc.md"
    path.write_bytes(raw)
    assert mcp.scan_metadata(path) == mcp.scan_bytes(raw, format="text")
    assert mcp.dumps_scan(mcp.scan_metadata(path)) == mcp.dumps_scan(
        mcp.scan_bytes(raw, format="text")
    )
    assert "desc.md" not in mcp.dumps_scan(mcp.scan_metadata(path))


def test_scan_metadata_invalid_utf8_file(tmp_path: Path) -> None:
    path = tmp_path / "utf16.json"
    path.write_bytes('{"a": 1}'.encode("utf-16"))
    with pytest.raises(mcp.FpchMcpError, match="não são UTF-8 válido"):
        mcp.scan_metadata(path)


def test_size_limit_is_inclusive_and_over_limit_raises(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(mcp, "FPCH_MCP_METADATA_MAX_BYTES", 300)
    monkeypatch.setattr(mcp, "_READ_CHUNK_BYTES", 64)
    exact = tmp_path / "exact.txt"
    exact.write_bytes(b"a" * 300)
    assert mcp.scan_metadata(exact).size_bytes == 300
    assert mcp.scan_bytes(b"a" * 300, format="text").size_bytes == 300

    over = tmp_path / "over.txt"
    over.write_bytes(b"a" * 301)
    with pytest.raises(mcp.FpchMcpError, match="grande demais|excede"):
        mcp.scan_metadata(over)
    with pytest.raises(mcp.FpchMcpError, match="excedem 1 MiB"):
        mcp.scan_bytes(b"a" * 301, format="text")


def test_default_size_limit_rejects_over_one_mib(tmp_path: Path) -> None:
    over = tmp_path / "over.txt"
    over.write_bytes(b" " * (mcp.FPCH_MCP_METADATA_MAX_BYTES + 1))
    with pytest.raises(mcp.FpchMcpError, match="grande demais"):
        mcp.scan_metadata(over)


def test_scan_metadata_rejects_missing_directory_and_non_path(tmp_path: Path) -> None:
    with pytest.raises(mcp.FpchMcpError, match="não foi possível ler"):
        mcp.scan_metadata(tmp_path / "missing.json")
    with pytest.raises(mcp.FpchMcpError, match="inseguro ou grande"):
        mcp.scan_metadata(tmp_path)
    with pytest.raises(mcp.FpchMcpError, match="caminho"):
        mcp.scan_metadata(123)  # type: ignore[arg-type]


def test_scan_metadata_rejects_final_symlink(tmp_path: Path) -> None:
    target = tmp_path / "target.json"
    target.write_text('{"a": "b"}', encoding="utf-8")
    link = tmp_path / "tools.json"
    try:
        link.symlink_to(target)
    except (NotImplementedError, OSError) as exc:
        pytest.skip(f"links simbólicos indisponíveis neste ambiente: {exc}")

    with pytest.raises(mcp.FpchMcpError, match="link ou reparse"):
        mcp.scan_metadata(link)


def test_scan_metadata_rejects_simulated_reparse_point(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "tools.json"
    path.write_text('{"a": "b"}', encoding="utf-8")
    real_is_linklike = mcp._is_linklike
    monkeypatch.setattr(
        mcp,
        "_is_linklike",
        lambda candidate: Path(candidate) == path or real_is_linklike(Path(candidate)),
    )

    with pytest.raises(mcp.FpchMcpError, match="link ou reparse"):
        mcp.scan_metadata(path)


def test_scan_never_uses_network_execution_or_writes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "tools.json"
    path.write_text('{"a": "\\udb40\\udc41"}', encoding="utf-8")
    before = sorted((item, item.stat().st_mtime_ns) for item in tmp_path.rglob("*"))

    def forbidden(*_args, **_kwargs):
        pytest.fail("varredura offline não pode acessar rede, executar ou abrir via open")

    real_os_open = os.open
    writable = os.O_WRONLY | os.O_RDWR | os.O_CREAT | os.O_TRUNC | os.O_APPEND
    opened_flags: list[int] = []

    def read_only_open(target, flags, *args, **kwargs):
        opened_flags.append(flags)
        return real_os_open(target, flags, *args, **kwargs)

    monkeypatch.setattr(socket, "socket", forbidden)
    monkeypatch.setattr(socket, "create_connection", forbidden)
    monkeypatch.setattr(urllib.request, "urlopen", forbidden)
    monkeypatch.setattr(subprocess, "run", forbidden)
    monkeypatch.setattr(subprocess, "Popen", forbidden)
    monkeypatch.setattr(builtins, "open", forbidden)
    monkeypatch.setattr(os, "open", read_only_open)

    scan = mcp.scan_metadata(path)
    serialized = mcp.dumps_scan(scan)
    monkeypatch.undo()

    assert scan.counts == (("tag", 1),)
    assert serialized.isascii()
    assert opened_flags and all(not flags & writable for flags in opened_flags)
    assert sorted((item, item.stat().st_mtime_ns) for item in tmp_path.rglob("*")) == before


def test_unicode_version_matches_runtime() -> None:
    assert _text("ok").unicode_version == unicodedata.unidata_version


def test_one_mib_non_ascii_text_scans_quickly(tmp_path: Path) -> None:
    unit = "informação ação çãõé 😀 "
    unit_bytes = len(unit.encode("utf-8"))
    body = unit * (mcp.FPCH_MCP_METADATA_MAX_BYTES // unit_bytes)
    path = tmp_path / "big.txt"
    path.write_bytes(body.encode("utf-8"))
    assert path.stat().st_size <= mcp.FPCH_MCP_METADATA_MAX_BYTES

    started = time.perf_counter()
    scan = mcp.scan_metadata(path)
    elapsed = time.perf_counter() - started

    assert scan.clean is True
    assert elapsed < 3.0


def test_one_mib_of_hidden_characters_scans_quickly() -> None:
    raw = ("​" * (mcp.FPCH_MCP_METADATA_MAX_BYTES // 3)).encode("utf-8")
    started = time.perf_counter()
    scan = mcp.scan_bytes(raw, format="text")
    elapsed = time.perf_counter() - started
    assert scan.findings_total == mcp.FPCH_MCP_METADATA_MAX_BYTES // 3
    assert len(scan.findings) == mcp.FPCH_MCP_SCAN_MAX_FINDINGS
    assert elapsed < 3.0


# --- Escapes ASCII no modo texto -----------------------------------------------


def _esc(hex_digits: str) -> str:
    return "\\" + "u" + hex_digits


def test_text_mode_reports_escaped_hidden_code_points() -> None:
    content = (
        "a" + _esc("DB40") + _esc("dc41")
        + " b" + _esc("200b")
        + " " + _esc("{E0041}")
        + " " + _esc("d800")
        + " " + _esc("0041")
        + " " + _esc("{110000}")
        + "\n" + _esc("2800") + chr(0x200B)
    )
    assert content[:-1].isascii()
    scan = _text(content)
    assert [
        (f.category, f.code_point, f.line, f.column, f.escaped) for f in scan.findings
    ] == [
        ("tag", 0xE0041, 1, 2, True),
        ("zero_width", 0x200B, 1, 16, True),
        ("tag", 0xE0041, 1, 23, True),
        ("surrogate", 0xD800, 1, 33, True),
        ("invisible_filler", 0x2800, 2, 1, True),
        ("zero_width", 0x200B, 2, 7, False),
    ]
    assert scan.counts == (
        ("tag", 2),
        ("zero_width", 2),
        ("invisible_filler", 1),
        ("surrogate", 1),
    )
    assert mcp.dumps_scan(scan).isascii()


def test_text_mode_high_surrogate_without_low_is_reported_alone() -> None:
    scan = _text("x" + _esc("d800") + _esc("0041") + _esc("dc00"))
    assert [(f.code_point, f.column, f.escaped) for f in scan.findings] == [
        (0xD800, 2, True),
        (0xDC00, 14, True),
    ]


def test_text_mode_clean_escapes_stay_clean() -> None:
    assert _text(_esc("0041") + _esc("00e7") + _esc("000a") + "\\n").clean is True


def test_json_mode_findings_are_never_marked_escaped() -> None:
    scan = _json('{"a": "' + _esc("200b") + '"}')
    assert [f.escaped for f in scan.findings] == [False]


# --- Pointer sanitizado e limitado ----------------------------------------------


def test_pointer_replaces_hidden_code_points_in_keys_and_ancestors() -> None:
    tag = chr(0xE0041)
    scan = _json(json.dumps({"pai" + tag: {"filho" + chr(0x202E): "v" + chr(0x200B)}}))
    pointers = [f.pointer for f in scan.findings]
    assert pointers == [
        "/pai" + chr(0xFFFD),
        "/pai" + chr(0xFFFD) + "/filho" + chr(0xFFFD),
        "/pai" + chr(0xFFFD) + "/filho" + chr(0xFFFD),
    ]
    assert all(tag not in p and chr(0x202E) not in p for p in pointers)
    assert [f.pointer_truncated for f in scan.findings] == [False, False, False]


def test_huge_hidden_key_pointer_is_capped_and_counts_stay_exact() -> None:
    cap = mcp.FPCH_MCP_SCAN_POINTER_MAX_CHARS
    key = chr(0xE0041) * 200_000
    scan = _json(json.dumps({key: [chr(0x200B)] * 10}, ensure_ascii=False))
    assert scan.findings_total == 200_010
    assert len(scan.findings) == mcp.FPCH_MCP_SCAN_MAX_FINDINGS
    assert scan.truncated is True
    assert scan.counts == (("tag", 200_000), ("zero_width", 10))
    for finding in scan.findings:
        assert len(finding.pointer) <= cap
        assert finding.pointer_truncated is True
        assert finding.pointer.endswith(mcp.FPCH_MCP_SCAN_POINTER_TRUNCATION_MARKER)
        assert chr(0xE0041) not in finding.pointer
    serialized = mcp.dumps_scan(scan)
    per_finding = 6 * cap + 256
    assert len(serialized) <= mcp.FPCH_MCP_SCAN_MAX_FINDINGS * per_finding + 4096
    assert json.loads(serialized)["findings"][0]["pointer_truncated"] is True


def test_truncated_pointer_never_splits_tilde_escape() -> None:
    scan = _json(json.dumps({"a" + "~" * 5000: {"x": chr(0x200B)}}))
    finding = scan.findings[0]
    assert finding.pointer_truncated is True
    assert len(finding.pointer) <= mcp.FPCH_MCP_SCAN_POINTER_MAX_CHARS
    body = finding.pointer[: -len(mcp.FPCH_MCP_SCAN_POINTER_TRUNCATION_MARKER)]
    assert body.endswith("~0")


def test_pointer_at_exact_cap_is_not_truncated() -> None:
    cap = mcp.FPCH_MCP_SCAN_POINTER_MAX_CHARS
    scan = _json(json.dumps({"a" * (cap - 1): chr(0x200B)}))
    finding = scan.findings[0]
    assert finding.pointer == "/" + "a" * (cap - 1)
    assert finding.pointer_truncated is False
    over = _json(json.dumps({"a" * cap: chr(0x200B)})).findings[0]
    assert over.pointer_truncated is True
    assert len(over.pointer) <= cap


# --- Leitura JSON estrita para consumidores ---------------------------------------


def test_read_metadata_json_uses_strict_loader(tmp_path: Path) -> None:
    path = tmp_path / "meta.txt"
    path.write_bytes(b'\xef\xbb\xbf{"a": [1]}')
    sha256, root = mcp.read_metadata_json(path)
    assert sha256 == mcp.scan_metadata(path).sha256
    assert root == {"a": [1]}
    path.write_bytes(b'{"a": 1, "a": 2}')
    with pytest.raises(mcp.FpchMcpError, match="chave duplicada"):
        mcp.read_metadata_json(path)
    path.write_bytes(b"texto livre")
    with pytest.raises(mcp.FpchMcpError, match="JSON de metadados MCP inválido"):
        mcp.read_metadata_json(path)
