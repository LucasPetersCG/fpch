"""Testes do carregador de política externa (`src/fpch/policy.py`).

Cobre a seção 5 de `docs/decisoes/fatia-funcional-2026-08-31.md`: a cascata de
precedência de cinco níveis, a validação estrita (falha alto e cedo), o
default embutido, e a equivalência desse default com `models.CATALOG` — a
prova de que ligar a política em `models.py` (Onda 2A) não muda nenhum
comportamento hoje existente.

Todo teste que toca "arquivo do projeto" ou "arquivo do usuário" isola o
diretório de trabalho e o `$HOME` via `monkeypatch.setattr(policy, "_cwd"/"_home", ...)`
— nunca lê o `~/.fpch` real nem o diretório de trabalho real de quem roda a
suíte. Ver o mesmo cuidado em `tests/test_citations.py`.

Rodar: `uv run --with pytest pytest tests/test_policy.py -q`
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from fpch import audit, models, policy  # noqa: E402


@pytest.fixture(autouse=True)
def _isola_cascata(tmp_path, monkeypatch):
    """Isola TODO teste deste arquivo do ambiente real: sem cwd real, sem HOME
    real, sem `FPCH_POLICY` real vazando de fora. Quem quiser um nível
    específico populado sobrescreve o `tmp_path` sob os subdiretórios criados
    aqui.
    """
    projeto_dir = tmp_path / "projeto"
    home_dir = tmp_path / "home"
    projeto_dir.mkdir()
    home_dir.mkdir()
    monkeypatch.setattr(policy, "_cwd", lambda: projeto_dir)
    monkeypatch.setattr(policy, "_home", lambda: home_dir)
    monkeypatch.delenv("FPCH_POLICY", raising=False)
    return {"projeto": projeto_dir, "home": home_dir}


def _escreve(caminho: Path, conteudo: str) -> Path:
    caminho.write_text(conteudo, encoding="utf-8")
    return caminho


# --- ausência total de arquivo ------------------------------------------------

def test_ausencia_de_arquivo_devolve_default_sem_erro(_isola_cascata):
    pol = policy.load()
    assert pol.fonte == "default embutido"
    assert pol.escalada.max_escalations == 2
    assert pol.backends.timeout_s == 600
    assert pol.verificacao.timeout_s == 120
    assert all(v == "default embutido" for v in pol.origem.values())


# --- precedência: cada nível vence o de baixo --------------------------------

def test_arquivo_do_usuario_vence_default(_isola_cascata):
    home = _isola_cascata["home"]
    (home / ".fpch").mkdir()
    _escreve(home / ".fpch" / "policy.toml", "[escalada]\nmax_escalations = 9\n")

    pol = policy.load()
    assert pol.escalada.max_escalations == 9
    assert "arquivo do usuário" in pol.origem["escalada"]
    # bloco não sobrescrito continua com origem de default, mesmo com arquivo vencendo.
    assert pol.origem["backends"] == "default embutido"
    assert pol.backends.timeout_s == 600


def test_arquivo_do_projeto_vence_arquivo_do_usuario(_isola_cascata):
    projeto = _isola_cascata["projeto"]
    home = _isola_cascata["home"]
    (home / ".fpch").mkdir()
    _escreve(home / ".fpch" / "policy.toml", "[escalada]\nmax_escalations = 9\n")
    _escreve(projeto / "fpch.policy.toml", "[escalada]\nmax_escalations = 5\n")

    pol = policy.load()
    assert pol.escalada.max_escalations == 5
    assert "arquivo do projeto" in pol.origem["escalada"]


def test_env_var_vence_arquivo_do_projeto(_isola_cascata, monkeypatch, tmp_path):
    projeto = _isola_cascata["projeto"]
    _escreve(projeto / "fpch.policy.toml", "[escalada]\nmax_escalations = 5\n")
    caminho_env = _escreve(tmp_path / "via_env.toml", "[escalada]\nmax_escalations = 7\n")
    monkeypatch.setenv("FPCH_POLICY", str(caminho_env))

    pol = policy.load()
    assert pol.escalada.max_escalations == 7
    assert "FPCH_POLICY" in pol.origem["escalada"]


def test_cli_path_vence_env_var(_isola_cascata, monkeypatch, tmp_path):
    caminho_env = _escreve(tmp_path / "via_env.toml", "[escalada]\nmax_escalations = 7\n")
    monkeypatch.setenv("FPCH_POLICY", str(caminho_env))
    caminho_cli = _escreve(tmp_path / "via_cli.toml", "[escalada]\nmax_escalations = 3\n")

    pol = policy.load(cli_path=caminho_cli)
    assert pol.escalada.max_escalations == 3
    assert "linha de comando" in pol.origem["escalada"]


def test_cli_path_explicito_ausente_e_erro(_isola_cascata, tmp_path):
    """Diferente dos níveis 3 e 4, um caminho explícito que não existe é erro:
    foi pedido por nome, "não encontrei" é informação, não um caso a mascarar."""
    inexistente = tmp_path / "nao-existe.toml"
    with pytest.raises(policy.PolicyError, match=r"n(ã|a)o encontrado"):
        policy.load(cli_path=inexistente)


# --- validação estrita: falha alto e cedo -------------------------------------

def test_toml_malformado_leva_caminho_do_arquivo_na_mensagem(_isola_cascata):
    projeto = _isola_cascata["projeto"]
    ruim = _escreve(projeto / "fpch.policy.toml", "isto nao [ e toml valido\n")

    with pytest.raises(policy.PolicyError) as exc:
        policy.load()
    assert str(ruim) in str(exc.value)


def test_chave_de_topo_desconhecida_nomeia_a_chave(_isola_cascata):
    projeto = _isola_cascata["projeto"]
    _escreve(projeto / "fpch.policy.toml", "[bloco_que_nao_existe]\nx = 1\n")

    with pytest.raises(policy.PolicyError, match="bloco_que_nao_existe"):
        policy.load()


def test_chave_desconhecida_dentro_de_bloco_nomeia_a_chave(_isola_cascata):
    projeto = _isola_cascata["projeto"]
    _escreve(projeto / "fpch.policy.toml", "[escalada]\ncampo_fantasma = 1\n")

    with pytest.raises(policy.PolicyError, match="campo_fantasma"):
        policy.load()


def test_valor_de_tipo_errado_levanta_erro(_isola_cascata):
    projeto = _isola_cascata["projeto"]
    _escreve(projeto / "fpch.policy.toml", '[escalada]\nmax_escalations = "dois"\n')

    with pytest.raises(policy.PolicyError, match="max_escalations"):
        policy.load()


@pytest.mark.parametrize("valor", [0, -1])
def test_timeout_de_verificacao_deve_ser_positivo(_isola_cascata, valor):
    projeto = _isola_cascata["projeto"]
    _escreve(projeto / "fpch.policy.toml", f"[verificacao]\ntimeout_s = {valor}\n")

    with pytest.raises(policy.PolicyError, match="verificacao.timeout_s"):
        policy.load()


def test_verificacao_carrega_valor_e_origem(_isola_cascata):
    projeto = _isola_cascata["projeto"]
    _escreve(projeto / "fpch.policy.toml", "[verificacao]\ntimeout_s = 2\n")

    pol = policy.load()

    assert pol.verificacao.timeout_s == 2
    assert "arquivo do projeto" in pol.origem["verificacao"]


def test_causa_raiz_deriva_da_taxonomia_e_nao_e_externalizavel(_isola_cascata):
    pol = policy.load()
    assert pol.causa_raiz.categorias == tuple(audit.CAUSE_TAXONOMY)
    assert pol.causa_raiz.subtipos == tuple(
        subtipo for subtipos in audit.CAUSE_TAXONOMY.values() for subtipo in subtipos
    )

    projeto = _isola_cascata["projeto"]
    _escreve(projeto / "fpch.policy.toml", '[causa_raiz]\ncategorias = ["inventada"]\n')
    with pytest.raises(policy.PolicyError, match="causa_raiz"):
        policy.load()


def test_good_for_com_classe_de_tarefa_inexistente_levanta_erro(_isola_cascata):
    projeto = _isola_cascata["projeto"]
    _escreve(projeto / "fpch.policy.toml", """
[[modelos]]
id = "modelo-teste"
backend = "agy"
pool = "agy:google"
power = 1
cost = 1
good_for = ["classe-que-nao-existe"]
""")

    with pytest.raises(policy.PolicyError, match="classe-que-nao-existe"):
        policy.load()


def test_pool_desconhecido_levanta_erro(_isola_cascata):
    projeto = _isola_cascata["projeto"]
    _escreve(projeto / "fpch.policy.toml", """
[[modelos]]
id = "modelo-teste"
backend = "agy"
pool = "pool-que-nao-existe"
power = 1
cost = 1
good_for = ["standard"]
""")

    with pytest.raises(policy.PolicyError, match="pool-que-nao-existe"):
        policy.load()


def test_modelo_sem_campo_obrigatorio_levanta_erro(_isola_cascata):
    projeto = _isola_cascata["projeto"]
    _escreve(projeto / "fpch.policy.toml", """
[[modelos]]
id = "modelo-teste"
backend = "agy"
pool = "agy:google"
good_for = ["standard"]
""")

    with pytest.raises(policy.PolicyError, match="modelos"):
        policy.load()


# --- equivalência do default embutido com models.CATALOG ---------------------

def test_catalogo_default_espelha_models():
    """A prova central: o default embutido de policy.py não é uma opinião nova,
    é o que já está em models.CATALOG hoje. Ligar a política em Onda 2A não
    pode mudar roteamento nenhum enquanto ninguém escrever um arquivo."""
    pol = policy._default()

    real = {
        (m.id, m.backend, m.pool.value, m.power, m.cost, tuple(t.value for t in m.good_for))
        for m in models.CATALOG
    }
    do_default = {
        (m.id, m.backend, m.pool, m.power, m.cost, m.good_for)
        for m in pol.modelos
    }
    assert do_default == real
    # e a ordem também é preservada — importa para o desempate estável de
    # candidates() em caso de (cost, -power) igual entre dois modelos.
    assert [m.id for m in pol.modelos] == [m.id for m in models.CATALOG]


def test_pools_validos_espelham_models_pool():
    assert policy.POOLS_VALIDOS == {p.value for p in models.Pool}


def test_task_classes_validas_espelham_models_taskclass():
    assert policy.TASK_CLASSES_VALIDAS == {t.value for t in models.TaskClass}


def test_escalada_default_espelha_router():
    """router.plan() e router.route() usam max_escalations=2 como default hoje."""
    pol = policy._default()
    assert pol.escalada.max_escalations == 2
    assert pol.escalada.ordenar_por == ("cost", "-power")


def test_backends_default_espelha_backends_py():
    pol = policy._default()
    assert pol.backends.timeout_s == 600
    assert pol.backends.arg_limit == 24_000


def test_falha_default_espelha_backends_py():
    pol = policy._default()
    assert pol.falha.min_chars == 40
    assert "no output produced" in pol.falha.assinaturas
    assert "auto-denied" in pol.falha.assinaturas
    assert len(pol.falha.assinaturas) == 8


# --- dump() mostra origem correta --------------------------------------------

def test_dump_mostra_origem_default(_isola_cascata):
    pol = policy.load()
    saida = policy.dump(pol)
    assert "[escalada] (origem: default embutido)" in saida
    assert "max_escalations = 2" in saida
    assert "[verificacao] (origem: default embutido)" in saida
    assert "timeout_s = 120" in saida
    # os blocos imutáveis aparecem, listados à parte.
    assert "taxonomia_de_causa_raiz" in saida
    assert "regras_de_proveniencia" in saida
    assert "clausula_anti_injecao" in saida
    assert "padroes_de_contencao" in saida
    assert "portao_de_aprovacao_humana" in saida


def test_dump_mostra_origem_de_arquivo(_isola_cascata):
    projeto = _isola_cascata["projeto"]
    _escreve(projeto / "fpch.policy.toml", "[escalada]\nmax_escalations = 4\n")

    pol = policy.load()
    saida = policy.dump(pol)
    assert "max_escalations = 4" in saida
    linha_escalada = next(l for l in saida.splitlines() if l.startswith("[escalada]"))
    assert "arquivo do projeto" in linha_escalada
    # bloco não tocado pelo arquivo continua marcado como default embutido.
    linha_backends = next(l for l in saida.splitlines() if l.startswith("[backends]"))
    assert "default embutido" in linha_backends


def test_catalogo_nao_cita_gemini_3_5_flash():
    """`agy` deixou de aceitar a família 3.5 Flash (ping real de 14/09/2026)."""
    assert not [m.id for m in models.CATALOG if "3.5" in m.id]
    assert not [m.id for m in policy._default().modelos if "3.5" in m.id]


def test_todo_modelo_codex_do_catalogo_e_traduzivel_em_flags():
    from fpch import backends

    codex = [m for m in models.CATALOG if m.backend == "codex"]
    assert codex, "o catálogo embutido deve ter modelos codex"
    for m in codex:
        assert m.pool is models.Pool.CODEX
        slug, esforco = backends.fpch_codex_id(m.id)
        assert esforco in backends.FPCH_CODEX_ESFORCOS
        assert slug not in {"gpt-reserve", "codex-auto-review"}, "modelo oculto não entra"


def test_todo_backend_do_catalogo_tem_adaptador():
    from fpch import backends

    assert {m.backend for m in models.CATALOG} <= set(backends.ADAPTERS)
