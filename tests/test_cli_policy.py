"""Onda 2A — a política externa chega até a linha de comando.

O que estes testes prendem, e por quê:

1. **A derivação `Policy → catálogo` não inventa nem descarta.** Pool ou classe
   de tarefa inexistente levanta `CatalogError` em vez de sumir com a entrada.
2. **`--policy` muda comportamento observável.** Se o catálogo em vigor fosse
   sempre `models.CATALOG`, a opção seria decorativa — e a fatia funcional
   inteira (seção 5) seria só um arquivo que ninguém lê.
3. **Política quebrada falha alto, cedo e com código próprio.** Código 2, não 1:
   não chegou a rodar é diferente de rodou e reprovou.
4. **`fpch check` sem hook algum NÃO devolve sucesso.** É a seção 3 do contrato
   aplicada ao verificador — ver o docstring de `cli._cmd_check`.

Nenhum teste aqui toca a trilha real do autor: `FPCH_AUDIT_LOG` é redirecionado
para `tmp_path` em toda função que possa emitir evento.
"""

from __future__ import annotations

import sys

import pytest

from fpch import cli, models, policy
from fpch.models import Pool, TaskClass


# --------------------------------------------------------------------------
# Auxiliares
# --------------------------------------------------------------------------

def _politica_minima(*, modelos: str = "", hooks: str = "") -> str:
    return f"""
[meta]
versao = 1
{modelos}
{hooks}
"""


_UM_MODELO = """
[[modelos]]
id = "Modelo Fictício"
backend = "agy"
pool = "local"
power = 1
cost = 1
good_for = ["mechanical", "hard"]
notes = "só existe neste teste"
"""


@pytest.fixture(autouse=True)
def _trilha_isolada(tmp_path, monkeypatch):
    """Nenhum teste desta suíte escreve na trilha real (`~/.fpch/audit.jsonl`)."""
    monkeypatch.setenv("FPCH_AUDIT_LOG", str(tmp_path / "audit.jsonl"))
    # A cascata da política não pode enxergar o `fpch.policy.toml` do repositório
    # nem o `~/.fpch/policy.toml` de quem roda a suíte: os testes que querem o
    # default embutido precisam recebê-lo de fato.
    monkeypatch.delenv("FPCH_POLICY", raising=False)
    monkeypatch.setattr(policy, "_cwd", lambda: tmp_path)
    monkeypatch.setattr(policy, "_home", lambda: tmp_path / "home-inexistente")


# --------------------------------------------------------------------------
# 1. Derivação Policy -> catálogo
# --------------------------------------------------------------------------

def test_catalogo_derivado_do_default_reproduz_CATALOG():
    """O default embutido da política e `models.CATALOG` são o mesmo catálogo.

    `test_policy.py` já compara os `id`s; aqui a comparação é do objeto `Model`
    inteiro, depois da conversão de `pool`/`good_for` para os enums.
    """
    derivado = models.catalog_from_policy(policy.load())
    assert derivado == models.CATALOG


def test_pool_inexistente_levanta_CatalogError():
    entrada = policy.ModelEntry("X", "agy", "pool:que:nao:existe", 1, 1, ("mechanical",))
    with pytest.raises(models.CatalogError) as exc:
        models.model_from_entry(entrada)
    assert "pool desconhecido" in str(exc.value)
    assert "'X'" in str(exc.value)


def test_classe_de_tarefa_inexistente_levanta_CatalogError():
    entrada = policy.ModelEntry("Y", "agy", "local", 1, 1, ("inventada",))
    with pytest.raises(models.CatalogError) as exc:
        models.model_from_entry(entrada)
    assert "classe de tarefa desconhecida" in str(exc.value)


def test_conversao_produz_enums_e_nao_texto():
    m = models.model_from_entry(
        policy.ModelEntry("Z", "agy", "local", 2, 3, ("hard",), notes="n")
    )
    assert m.pool is Pool.LOCAL
    assert m.good_for == (TaskClass.HARD,)
    assert m.notes == "n"


# --------------------------------------------------------------------------
# 2. `candidates()` e o catálogo em vigor
# --------------------------------------------------------------------------

def test_candidates_mantem_assinatura_posicional_antiga():
    """`candidates(task, exclude_pools)` continua significando o mesmo."""
    candidatos = models.candidates(TaskClass.STANDARD, frozenset({Pool.AGY_GOOGLE}))
    assert all(m.pool is not Pool.AGY_GOOGLE for m in candidatos)
    assert candidatos, "o copilot ainda serve a 'standard'"


def test_candidates_aceita_catalogo_explicito():
    catalogo = (models.model_from_entry(
        policy.ModelEntry("Só Este", "agy", "local", 1, 1, ("hard",)),
    ),)
    assert [m.id for m in models.candidates(TaskClass.HARD, catalog=catalogo)] == ["Só Este"]
    # E o catálogo embutido segue intocado.
    assert models.active_catalog() == models.CATALOG


def test_use_catalog_restaura_inclusive_com_excecao():
    catalogo = (models.model_from_entry(
        policy.ModelEntry("Efêmero", "agy", "local", 1, 1, ("hard",)),
    ),)
    with pytest.raises(RuntimeError):
        with models.use_catalog(catalogo):
            assert models.active_catalog() == catalogo
            assert "Efêmero" in models.BY_ID
            raise RuntimeError("boom")
    assert models.active_catalog() == models.CATALOG
    assert models.BY_ID == models.index_by_id(models.CATALOG)


# --------------------------------------------------------------------------
# 3. `--policy` é observável na CLI
# --------------------------------------------------------------------------

def test_policy_muda_o_catalogo_que_models_imprime(tmp_path, capsys):
    p = tmp_path / "p.toml"
    p.write_text(_politica_minima(modelos=_UM_MODELO), encoding="utf-8")

    assert cli.main(["--policy", str(p), "models"]) == 0
    saida = capsys.readouterr().out
    assert "Modelo Fictício" in saida
    assert "Gemini 3.5 Flash (Medium)" not in saida, "o catálogo embutido não deve vazar"
    assert str(p) in saida, "a origem da política precisa aparecer"


def test_sem_policy_a_cli_usa_o_default_embutido(tmp_path, capsys):
    assert cli.main(["models"]) == 0
    saida = capsys.readouterr().out
    assert "default embutido" in saida
    assert "Gemini 3.5 Flash (Medium)" in saida


def test_catalogo_em_vigor_nao_vaza_depois_do_comando(tmp_path):
    p = tmp_path / "p.toml"
    p.write_text(_politica_minima(modelos=_UM_MODELO), encoding="utf-8")
    cli.main(["--policy", str(p), "models"])
    assert models.active_catalog() == models.CATALOG
    assert models.BY_ID == models.index_by_id(models.CATALOG)


# --------------------------------------------------------------------------
# 4. Política quebrada falha alto, cedo, com código 2
# --------------------------------------------------------------------------

def test_policy_inexistente_falha_com_codigo_2(tmp_path, capsys):
    ausente = tmp_path / "nao-existe.toml"
    assert cli.main(["--policy", str(ausente), "models"]) == cli.EXIT_USO
    err = capsys.readouterr().err
    assert "erro de política" in err
    assert str(ausente) in err


def test_toml_malformado_falha_com_codigo_2(tmp_path, capsys):
    p = tmp_path / "p.toml"
    p.write_text("[meta\nversao = 1", encoding="utf-8")
    assert cli.main(["--policy", str(p), "models"]) == cli.EXIT_USO
    assert "malformado" in capsys.readouterr().err


def test_chave_desconhecida_falha_com_codigo_2(tmp_path, capsys):
    p = tmp_path / "p.toml"
    p.write_text("[meta]\nversao = 1\ninventada = 3\n", encoding="utf-8")
    assert cli.main(["--policy", str(p), "models"]) == cli.EXIT_USO
    err = capsys.readouterr().err
    assert "chave desconhecida" in err and "inventada" in err


def test_a_falha_de_politica_acontece_antes_do_subcomando(tmp_path, capsys):
    """Mesmo um subcomando que nem olharia a política é abortado.

    A carga é única e anterior ao despacho: descobrir a política quebrada no meio
    da execução seria descobri-la depois do efeito colateral.
    """
    p = tmp_path / "p.toml"
    p.write_text("isto não é toml [[[", encoding="utf-8")
    assert cli.main(["--policy", str(p), "audit"]) == cli.EXIT_USO
    assert "sem registros" not in capsys.readouterr().out


# --------------------------------------------------------------------------
# 5. `fpch config --dump`
# --------------------------------------------------------------------------

def test_config_dump_mostra_origem_e_blocos_imutaveis(tmp_path, capsys):
    p = tmp_path / "p.toml"
    p.write_text(_politica_minima(modelos=_UM_MODELO), encoding="utf-8")
    assert cli.main(["--policy", str(p), "config", "--dump"]) == 0
    saida = capsys.readouterr().out
    assert "Política efetiva do FPCH" in saida
    # bloco vindo do arquivo x bloco herdado do default, lado a lado
    assert str(p) in saida
    assert "default embutido" in saida
    for b in policy.IMMUTABLE_BLOCKS:
        assert b.nome in saida


def test_config_sem_dump_e_erro_de_uso(capsys):
    assert cli.main(["config"]) == cli.EXIT_USO
    assert "uso:" in capsys.readouterr().err


# --------------------------------------------------------------------------
# 6. `fpch check`
# --------------------------------------------------------------------------

def _hook_toml(nome: str, codigo: str, criterio: str = "exit_zero") -> str:
    argv = [sys.executable, "-c", codigo]
    itens = ", ".join(repr(a).replace("'", '"') for a in argv)
    return f'\n[[hooks]]\nnome = "{nome}"\ncmd = [{itens}]\nquando = "sempre"\ncriterio = "{criterio}"\n'


def test_check_sem_hook_nao_devolve_sucesso(tmp_path, capsys):
    """Decisão da Onda 2A: ausência de verificador NÃO é aprovação.

    Ver o docstring de `cli._cmd_check` para o registro da escolha e da
    alternativa recusada.
    """
    p = tmp_path / "p.toml"
    p.write_text(_politica_minima(), encoding="utf-8")
    codigo = cli.main(["--policy", str(p), "check"])
    assert codigo != 0
    cap = capsys.readouterr()
    assert "absent" in cap.out
    assert "nenhum hook declarado" in cap.err
    assert "NÃO é aprovação" in cap.err


def test_check_com_hook_que_passa_devolve_zero(tmp_path, capsys):
    p = tmp_path / "p.toml"
    p.write_text(_politica_minima(hooks=_hook_toml("ok", "pass")), encoding="utf-8")
    assert cli.main(["--policy", str(p), "check", "--cwd", str(tmp_path)]) == 0
    saida = capsys.readouterr().out
    assert "veredito geral: pass" in saida


def test_check_com_hook_que_reprova_devolve_nao_zero(tmp_path, capsys):
    p = tmp_path / "p.toml"
    p.write_text(
        _politica_minima(hooks=_hook_toml("falha", "raise SystemExit(3)")),
        encoding="utf-8",
    )
    assert cli.main(["--policy", str(p), "check", "--cwd", str(tmp_path)]) != 0
    saida = capsys.readouterr().out
    assert "veredito geral: fail" in saida
    assert "exit=3" in saida


def test_check_com_criterio_irreconhecivel_e_absent_nao_pass(tmp_path, capsys):
    p = tmp_path / "p.toml"
    p.write_text(
        _politica_minima(hooks=_hook_toml("sem-criterio", "pass", criterio="acho_que_sim")),
        encoding="utf-8",
    )
    assert cli.main(["--policy", str(p), "check", "--cwd", str(tmp_path)]) != 0
    assert "veredito geral: absent" in capsys.readouterr().out


def test_check_deixa_rastro_na_trilha(tmp_path, capsys):
    from fpch import audit

    p = tmp_path / "p.toml"
    p.write_text(_politica_minima(hooks=_hook_toml("ok", "pass")), encoding="utf-8")
    cli.main(["--policy", str(p), "check", "--cwd", str(tmp_path)])
    capsys.readouterr()

    eventos = audit.read_all(tmp_path / "audit.jsonl")
    verifies = [e for e in eventos if e.get("event") == "verify"]
    assert len(verifies) == 1
    assert verifies[0]["component"] == "ok"
    assert verifies[0]["verdict"] == "pass"
