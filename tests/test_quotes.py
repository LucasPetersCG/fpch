"""Testes de `fpch.quotes`.

O foco destes testes **não** é provar que o verificador pega fabricação — isso é
a parte fácil. É provar que ele **não acusa o inocente**, porque foi exatamente
esse o erro que custou caro a este projeto em 17/07/2026 (ver o cabeçalho de
`src/fpch/citations.py`).

Cada caso de ruído de extração de PDF abaixo — hifenização, ligadura, aspas
curvas, quebra de linha no meio da frase — é um cenário em que um comparador
ingênuo gritaria "citação fabricada" sobre uma citação honesta.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from fpch import quotes  # noqa: E402

FONTE = """
===== PAGINA 1 =====
From Prompts to Contracts: Harness Engineering for Auditable Enterprise LLM Agents

===== PAGINA 2 =====
We argue that deterministic behaviour should migrate into code, manifests and
schemas around a replaceable composition boundary, while grounded answers remain
under runtime authority.

===== PAGINA 3 =====
Holding the model fixed and varying only the enforcement layer, prompt-only
instructions let violations through that the harness blocks entirely.
"""


def _ficha(corpo: str) -> str:
    return f"# [TIER B] Teste\n\n## Trechos\n\n{corpo}\n"


# --- o verificador precisa achar o que existe -------------------------------


def test_trecho_exato_e_reconhecido():
    f = _ficha('> "deterministic behaviour should migrate into code, manifests and schemas"')
    r = quotes.validate(f, FONTE)
    assert [q.verdict for q in r.quotes] == ["exato"]
    assert not r.failed


def test_trecho_atravessando_quebra_de_linha_e_exato():
    # No PDF a frase quebra entre "remain" e "under". Na ficha veio numa linha só.
    f = _ficha('> "grounded answers remain under runtime authority"')
    r = quotes.validate(f, FONTE)
    assert [q.verdict for q in r.quotes] == ["exato"]


# --- e, sobretudo, NÃO pode acusar o inocente -------------------------------


def test_hifenizacao_de_quebra_de_linha_nao_vira_acusacao():
    fonte = FONTE.replace("composition", "compo-\nsition")
    f = _ficha('> "a replaceable composition boundary, while grounded answers remain"')
    r = quotes.validate(f, fonte)
    assert r.quotes[0].verdict == "exato", "hifenização do PDF não pode virar fabricação"


def test_aspas_curvas_e_travessao_nao_viram_acusacao():
    fonte = FONTE.replace("prompt-only", "prompt–only")
    f = _ficha('> “prompt-only instructions let violations through”')
    r = quotes.validate(f, fonte)
    assert r.quotes[0].verdict == "exato"


def test_espacamento_irregular_nao_vira_acusacao():
    fonte = FONTE.replace("under runtime authority", "under    runtime\n\n  authority")
    f = _ficha('> "grounded answers remain under runtime authority"')
    r = quotes.validate(f, fonte)
    assert r.quotes[0].verdict == "exato"


def test_trecho_em_portugues_e_tratado_como_traducao_nao_como_falha():
    f = _ficha('> "o comportamento determinístico deve migrar para o código, '
               'manifestos e esquemas em torno de uma fronteira substituível"')
    r = quotes.validate(f, FONTE)
    assert r.quotes[0].verdict == "nao-verificavel"
    assert not r.failed, "tradução não é fabricação"


def test_marcado_como_parafrase_nao_e_cobrado_como_literal():
    f = _ficha('"the harness blocks violations that prompts do not" (paráfrase)')
    r = quotes.validate(f, FONTE)
    assert r.quotes[0].verdict == "nao-verificavel"


# --- distinguir edição leve de invenção -------------------------------------


def test_citacao_levemente_editada_e_aproximada_nao_ausente():
    # Trocou uma palavra no meio; o resto existe.
    f = _ficha('> "deterministic behaviour should migrate into code, manifests and '
               'blueprints around a replaceable composition boundary"')
    r = quotes.validate(f, FONTE)
    assert r.quotes[0].verdict == "aproximado"
    assert not r.failed, "edição leve é aviso, não reprovação"


def test_frase_inexistente_e_ausente_e_reprova():
    f = _ficha('> "the authors report a ninety-seven percent reduction in total '
               'operating cost across every evaluated deployment scenario"')
    r = quotes.validate(f, FONTE)
    assert r.quotes[0].verdict == "ausente"
    assert r.failed


# --- páginas ----------------------------------------------------------------


def test_conta_paginas_da_fonte():
    assert quotes.count_pages(FONTE) == 3


def test_pagina_dentro_do_intervalo_passa():
    r = quotes.validate(_ficha("Afirmação qualquer (p. 2)."), FONTE)
    assert [p.verdict for p in r.pages] == ["ok"]
    assert not r.failed


def test_pagina_alem_do_fim_reprova():
    r = quotes.validate(_ficha("Afirmação inventada (p. 41)."), FONTE)
    assert r.bad_pages and r.bad_pages[0].page == 41
    assert r.failed


def test_paginas_repetidas_contam_uma_vez():
    r = quotes.validate(_ficha("(p. 2) e de novo (p. 2) e (p. 3)."), FONTE)
    assert len(r.pages) == 2


# --- comportamento de borda -------------------------------------------------


def test_termo_curto_entre_aspas_nao_e_tratado_como_trecho():
    r = quotes.validate(_ficha('O paper usa "harness" no sentido amplo.'), FONTE)
    assert r.quotes == ()


def test_ficha_sem_trechos_gera_bloco_de_alerta():
    r = quotes.validate(_ficha("Nenhuma citação literal aqui."), FONTE)
    bloco = quotes.render_block(r, "fonte.txt")
    assert "não-ancoradas" in bloco


def test_bloco_sempre_declara_o_limite_do_verificador():
    f = _ficha('> "deterministic behaviour should migrate into code, manifests and schemas"')
    bloco = quotes.render_block(quotes.validate(f, FONTE), "fonte.txt")
    # O verificador prova presença, não pertinência. Isso precisa estar escrito.
    assert "não** prova que" in bloco or "não* prova" in bloco or "não prova" in bloco


def test_trecho_duplicado_conta_uma_vez():
    trecho = '> "grounded answers remain under runtime authority"'
    r = quotes.validate(_ficha(trecho + "\n\n" + trecho), FONTE)
    assert len(r.quotes) == 1


def test_fonte_sem_marcador_de_pagina_nao_reprova_referencia():
    r = quotes.validate(_ficha("Afirmação (p. 9)."), "texto solto sem marcadores")
    assert r.source_pages == 0
    assert not r.failed, "sem marcador de página não dá para julgar — não acusar"
