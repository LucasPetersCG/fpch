"""Testes do validador de citações.

Este arquivo existe por uma razão específica: em 17/07/2026 um verificador
não-calibrado acusou um modelo de fabricar 35% das citações. A acusação era falsa —
o verificador contava linhas errado (`Measure-Object -Line` ignora linhas em branco).

Um hook determinístico é automaticamente *consistente*, não automaticamente
*correto*. Consistente e errado é pior que ausente: produz falsa confiança e acusa
quem estava certo. Então o contador de linhas — a única coisa que este módulo
realmente precisa acertar — é testado contra casos onde a resposta é indiscutível.

Rodar: `uv run pytest tests/ -q`
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from fpch import citations  # noqa: E402


def _write(tmp_path: Path, name: str, content: str) -> Path:
    p = tmp_path / name
    p.write_bytes(content.encode("utf-8"))
    return p


# --- o contador de linhas: onde o bug de 17/07/2026 morava --------------------

def test_conta_linhas_em_branco(tmp_path: Path):
    """O bug original. `Measure-Object -Line` daria 2; a resposta é 5."""
    p = _write(tmp_path, "a.py", "um\n\n\n\ncinco\n")
    assert citations._count_lines(p) == 5


def test_conta_arquivo_sem_newline_final(tmp_path: Path):
    p = _write(tmp_path, "a.py", "um\ndois\ntres")
    assert citations._count_lines(p) == 3


def test_conta_crlf(tmp_path: Path):
    """Windows. Se contasse \\r como separador, daria o dobro."""
    p = _write(tmp_path, "a.py", "um\r\ndois\r\ntres\r\n")
    assert citations._count_lines(p) == 3


def test_arquivo_vazio(tmp_path: Path):
    p = _write(tmp_path, "a.py", "")
    assert citations._count_lines(p) == 0


def test_arquivo_so_de_linhas_em_branco(tmp_path: Path):
    """Caso onde o método bugado daria 0 e o certo dá 3."""
    p = _write(tmp_path, "a.py", "\n\n\n")
    assert citations._count_lines(p) == 3


# --- classificação de citações -----------------------------------------------

def test_citacao_valida_na_ultima_linha(tmp_path: Path):
    """`README.md:92` → 'MIT' foi o caso real acusado injustamente."""
    _write(tmp_path, "README.md", "\n" * 91 + "MIT\n")
    rep = citations.validate("a licença está em README.md:92", tmp_path)
    assert rep.total == 1
    assert rep.valid == 1
    assert rep.fabrication_rate == 0.0


def test_linha_alem_do_fim(tmp_path: Path):
    _write(tmp_path, "a.py", "um\ndois\n")
    rep = citations.validate("ver a.py:99", tmp_path)
    assert rep.fabricated == 1
    assert rep.failures()[0].verdict == "linha-inexistente"


def test_prefixo_src_inventado(tmp_path: Path):
    """Modelos inventam `src/` por hábito — visto em duas fichas independentes."""
    _write(tmp_path, "main.py", "x\n")
    rep = citations.validate("ver src/main.py:1", tmp_path)
    assert rep.fabricated == 1
    assert rep.failures()[0].verdict == "prefixo-inventado"
    assert "main.py" in rep.failures()[0].detail


def test_arquivo_inexistente(tmp_path: Path):
    rep = citations.validate("ver fantasma.py:1", tmp_path)
    assert rep.fabricated == 1
    assert rep.failures()[0].verdict == "arquivo-inexistente"


def test_deduplica_citacao_repetida(tmp_path: Path):
    """A mesma citação repetida é um erro, não vários — senão o % mente."""
    _write(tmp_path, "a.py", "um\n")
    rep = citations.validate("a.py:9 e de novo a.py:9 e a.py:9", tmp_path)
    assert rep.total == 1


def test_sem_citacoes_nao_e_sucesso(tmp_path: Path):
    """Ausência de citação não pode passar como 0% de fabricação."""
    rep = citations.validate("texto sem nenhuma citação", tmp_path)
    assert rep.total == 0
    assert "nenhuma citação" in citations.render_block(rep, tmp_path)


def test_render_block_sempre_reporta(tmp_path: Path):
    """Validador que só aparece na falha não é auditável."""
    _write(tmp_path, "a.py", "um\n")
    rep = citations.validate("ver a.py:1", tmp_path)
    block = citations.render_block(rep, tmp_path)
    assert "✅" in block
    assert "1 citações · 1 válidas" in block
