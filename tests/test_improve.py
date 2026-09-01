"""Testes do laço de auto-aprimoramento (`src/fpch/improve.py`).

Cobre a seção 8 de `docs/decisoes/fatia-funcional-2026-08-31.md`. Três testes deste
lote valem mais que os outros, e é por eles que o módulo existe:

* `test_improve_sem_apply_nao_toca_a_politica` — compara o arquivo de política
  **byte a byte** antes e depois. Propor e autorizar são operações distintas; se
  este teste cair, a separação virou cerimônia.
* `test_apply_recusa_trilha_adulterada_e_registra` — quebra um hash de propósito e
  prova que a recusa foi **gravada na trilha**. Recusa que não deixa rastro não é
  auditável, e a alegação metodológica do TCC depende de que seja.
* `test_recusa_por_falta_do_lado_do_modelo` — recusar aqui é o resultado *correto*,
  não um erro do laço: falha do modelo não se conserta editando política.

Herméticos por construção: nenhuma rede, nenhum CLI de terceiro, nenhum processo
filho. Toda trilha, toda política e todo diretório de propostas vivem em `tmp_path`
— o `~/.fpch` real do autor nunca é lido nem escrito.
"""

from __future__ import annotations

import json
import tomllib
from dataclasses import fields

import pytest

from fpch import audit, improve, policy as policy_mod


@pytest.fixture(autouse=True)
def _limpa_estado_da_trilha():
    """`audit` guarda contadores, cache de cadeia e contadores de `seq` no módulo."""
    audit.reset_state()
    yield
    audit.reset_state()


@pytest.fixture(autouse=True)
def _isola_diretorios_reais(tmp_path, monkeypatch):
    """Rede de segurança: mesmo um caminho esquecido cai em `tmp_path`, não em `~/.fpch`."""
    monkeypatch.setenv("FPCH_PROPOSTAS_DIR", str(tmp_path / "propostas-fallback"))
    monkeypatch.setenv("FPCH_AUDIT_LOG", str(tmp_path / "audit-fallback.jsonl"))
    monkeypatch.setattr(policy_mod, "_home", lambda: tmp_path / "casa-falsa")
    monkeypatch.setattr(policy_mod, "_cwd", lambda: tmp_path / "projeto-falso")


@pytest.fixture
def trilha(tmp_path):
    return tmp_path / "audit.jsonl"


@pytest.fixture
def propostas(tmp_path):
    return tmp_path / "propostas"


@pytest.fixture
def pol():
    return policy_mod.load(None)


def _verify(trilha, *, tid="t1", component="pytest", verdict="fail",
            evidence="exit=None; o hook estourou o timeout de 120s e foi interrompido",
            fault_side="infraestrutura", cause_category=None, label=None) -> bool:
    """Grava um evento `verify` na trilha, encadeado como o `hooks.py` faria."""
    return audit.write(
        audit.Event(
            event="verify",
            trajectory_id=tid,
            seq=audit.next_seq(tid),
            label=label,
            component=component,
            verdict=verdict,
            evidence=evidence,
            fault_side=fault_side,
            cause_category=cause_category,
        ),
        path=trilha,
    )


def _linhas(path) -> list[dict]:
    if not path.exists():
        return []
    return [json.loads(ln) for ln in path.read_text(encoding="utf-8").splitlines() if ln.strip()]


def _politica(tmp_path, corpo: str):
    caminho = tmp_path / "fpch.policy.toml"
    caminho.write_text(corpo, encoding="utf-8")
    return caminho


# ---------------------------------------------------------------------------
# 1. As três condições cumpridas → proposta nasce
# ---------------------------------------------------------------------------

def test_proposta_nasce_quando_as_tres_condicoes_valem(pol, trilha, propostas):
    _verify(trilha)

    an = improve.propor(pol, trilha=trilha, propostas_dir=propostas)

    assert an.proposta is not None, an.recusas
    p = an.proposta
    # (a) evidência auditável: veio de um `verify`/`fail` de trajetória íntegra.
    assert p.trajectory_id == "t1"
    assert p.evidence
    assert audit.verify_chain(trilha) == (True, None)
    # (b) o lado da falta é o único que uma política pode consertar.
    assert p.fault_side == "infraestrutura"
    # (c) o caminho de remoção é declarado, e cita o valor de volta.
    assert p.remocao
    assert str(pol.backends.timeout_s) in p.remocao
    # a alteração é de bloco externalizável (seção 5.2).
    assert p.bloco in improve.BLOCOS_EXTERNALIZAVEIS
    assert (p.bloco, p.campo) == ("backends", "timeout_s")
    assert p.valor_proposto == pol.backends.timeout_s * 2


def test_proposta_e_gravada_em_arquivo_json_com_o_id_no_nome(pol, trilha, propostas):
    _verify(trilha)
    an = improve.propor(pol, trilha=trilha, propostas_dir=propostas)

    assert an.caminho == propostas / f"{an.proposta.id}.json"
    dados = json.loads(an.caminho.read_text(encoding="utf-8"))
    assert dados["schema_version"] == improve.PROPOSTA_SCHEMA
    assert dados["status"] == "proposta"
    assert dados["remocao"]
    assert dados["justificativa"]


def test_id_e_deterministico_para_a_mesma_evidencia(pol, trilha, propostas):
    """Rodar duas vezes reescreve a mesma proposta em vez de multiplicá-la."""
    _verify(trilha)
    primeira = improve.propor(pol, trilha=trilha, propostas_dir=propostas)
    segunda = improve.propor(pol, trilha=trilha, propostas_dir=propostas)

    assert primeira.proposta.id == segunda.proposta.id
    assert len(list(propostas.glob("*.json"))) == 1


def test_regra_de_assinatura_propoe_reconhecer_falha_silenciosa(trilha, propostas, tmp_path):
    """`exit == 0` que não é sucesso: a segunda regra, sobre [falha].assinaturas."""
    caminho = _politica(tmp_path, '[falha]\nassinaturas = ["auto-denied"]\n')
    pol = policy_mod.load(caminho)
    _verify(trilha, component="rota", evidence="saída: quota exceeded para o pool agy:google")

    an = improve.propor(pol, trilha=trilha, propostas_dir=propostas)

    assert an.proposta is not None, an.recusas
    assert (an.proposta.bloco, an.proposta.campo) == ("falha", "assinaturas")
    assert "quota exceeded" in an.proposta.valor_proposto
    assert "auto-denied" in an.proposta.valor_proposto


# ---------------------------------------------------------------------------
# 2. Recusa por cada condição isoladamente ausente
# ---------------------------------------------------------------------------

def test_recusa_por_evidencia_ausente(pol, trilha, propostas):
    """(a) ausente: reprovou, mas não deixou evidência. As outras duas valeriam."""
    _verify(trilha, evidence="", fault_side="infraestrutura")

    an = improve.propor(pol, trilha=trilha, propostas_dir=propostas)

    assert an.proposta is None
    assert [r.codigo for r in an.recusas] == [improve.RECUSA_SEM_EVIDENCIA]
    assert "condição (a)" in an.recusas[0].mensagem
    assert not list(propostas.glob("*.json"))  # nada gravado em disco


def test_recusa_por_cadeia_quebrada_e_a_condicao_a(pol, trilha, propostas):
    """(a) ausente pelo outro lado: a evidência existe, a trilha é que não confere."""
    _verify(trilha)
    linhas = trilha.read_text(encoding="utf-8").splitlines()
    adulterada = json.loads(linhas[0])
    adulterada["evidence"] = "timeout inventado a posteriori"
    linhas[0] = json.dumps(adulterada, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    trilha.write_text("\n".join(linhas) + "\n", encoding="utf-8")

    assert audit.verify_chain(trilha)[0] is False
    an = improve.propor(pol, trilha=trilha, propostas_dir=propostas)

    assert an.proposta is None
    assert improve.RECUSA_CADEIA_QUEBRADA in [r.codigo for r in an.recusas]


def test_recusa_por_falta_do_lado_do_modelo(pol, trilha, propostas):
    """(b) ausente. Recusar é o resultado CORRETO — não um erro a contornar.

    A evidência casaria com a regra de timeout e a cadeia está íntegra: as
    condições (a) e (c) valem. Só o lado da falta é outro, e isso basta — regra de
    política contra falha do modelo é a proteção fantasma que Wang et al. descrevem.
    """
    _verify(trilha, fault_side="modelo")

    an = improve.propor(pol, trilha=trilha, propostas_dir=propostas)

    assert an.proposta is None
    assert [r.codigo for r in an.recusas] == [improve.RECUSA_LADO_MODELO]
    assert "modelo" in an.recusas[0].mensagem


def test_recusa_por_falta_de_caminho_de_remocao(pol, trilha, propostas):
    """(c) ausente: evidência auditável, lado certo, mas nenhuma remoção declarável."""
    _verify(trilha, evidence="exit=1; saída: assert 1 == 2 em tests/test_x.py")

    an = improve.propor(pol, trilha=trilha, propostas_dir=propostas)

    assert an.proposta is None
    assert [r.codigo for r in an.recusas] == [improve.RECUSA_SEM_REMOCAO]
    assert "condição (c)" in an.recusas[0].mensagem


def test_trilha_sem_falha_alguma_recusa_por_evidencia(pol, trilha, propostas):
    _verify(trilha, verdict="pass", fault_side=None)

    an = improve.propor(pol, trilha=trilha, propostas_dir=propostas)

    assert an.proposta is None
    assert an.recusas[0].codigo == improve.RECUSA_SEM_EVIDENCIA


def test_recusas_sao_registradas_na_trilha(pol, trilha, propostas):
    """Seção 3 do contrato: proposta recusada é recusada COM a recusa registrada."""
    _verify(trilha, fault_side="modelo")
    antes = len(_linhas(trilha))

    improve.propor(pol, trilha=trilha, propostas_dir=propostas)

    depois = _linhas(trilha)
    assert len(depois) == antes + 1
    registro = depois[-1]
    assert registro["component"] == improve.COMPONENTE_PROPOR
    assert registro["verdict"] == "fail"
    assert improve.RECUSA_LADO_MODELO in registro["evidence"]
    assert audit.verify_chain(trilha) == (True, None)


def test_evento_do_proprio_laco_nunca_vira_candidato(pol, trilha, propostas):
    """Sem isto, a recusa de uma proposta viraria evidência para a proposta seguinte."""
    _verify(trilha, component=improve.COMPONENTE_APLICAR,
            evidence="RECUSA cadeia_quebrada: timeout", fault_side="infraestrutura")

    an = improve.propor(pol, trilha=trilha, propostas_dir=propostas)

    assert an.proposta is None
    assert an.recusas[0].codigo == improve.RECUSA_SEM_EVIDENCIA
    # e o evento gravado pela própria recusa também não é candidato
    assert improve.propor(pol, trilha=trilha, propostas_dir=propostas).proposta is None


# ---------------------------------------------------------------------------
# 3. O invariante central: propor NUNCA altera a política
# ---------------------------------------------------------------------------

def test_improve_sem_apply_nao_toca_a_politica(trilha, propostas, tmp_path):
    """Byte a byte, antes e depois. É o critério de aceitação da seção 8."""
    caminho = _politica(tmp_path, "[backends]\ntimeout_s = 600\narg_limit = 24000\n")
    pol = policy_mod.load(caminho)
    antes = caminho.read_bytes()

    _verify(trilha)
    an = improve.propor(pol, trilha=trilha, propostas_dir=propostas)

    assert an.proposta is not None, an.recusas
    assert caminho.read_bytes() == antes
    assert policy_mod.load(caminho).backends.timeout_s == 600


def test_improve_recusando_tambem_nao_toca_a_politica(trilha, propostas, tmp_path):
    caminho = _politica(tmp_path, "[backends]\ntimeout_s = 600\n")
    pol = policy_mod.load(caminho)
    antes = caminho.read_bytes()

    _verify(trilha, fault_side="modelo")
    improve.propor(pol, trilha=trilha, propostas_dir=propostas)

    assert caminho.read_bytes() == antes


# ---------------------------------------------------------------------------
# 4. `--apply`: o portão de autorização
# ---------------------------------------------------------------------------

def test_apply_altera_o_bloco_externalizavel_e_registra(trilha, propostas, tmp_path):
    caminho = _politica(tmp_path, "[backends]\ntimeout_s = 600\narg_limit = 24000\n")
    pol = policy_mod.load(caminho)
    _verify(trilha)
    an = improve.propor(pol, trilha=trilha, propostas_dir=propostas)

    res = improve.aplicar(an.proposta.id, policy_path=caminho, trilha=trilha,
                          propostas_dir=propostas)

    assert res.ok, res.recusa
    assert res.exit_code == 0
    assert policy_mod.load(caminho).backends.timeout_s == 1200
    # o resto do arquivo sobreviveu: edição cirúrgica, não regravação
    assert policy_mod.load(caminho).backends.arg_limit == 24000
    registro = _linhas(trilha)[-1]
    assert registro["component"] == improve.COMPONENTE_APLICAR
    assert registro["verdict"] == "pass"
    assert json.loads((propostas / f"{an.proposta.id}.json").read_text(encoding="utf-8"))["status"] == "aplicada"


def test_apply_recusa_trilha_adulterada_e_registra(trilha, propostas, tmp_path):
    """Quebra um hash DEPOIS de a proposta nascer e prova que a recusa foi gravada."""
    caminho = _politica(tmp_path, "[backends]\ntimeout_s = 600\n")
    pol = policy_mod.load(caminho)
    _verify(trilha)
    an = improve.propor(pol, trilha=trilha, propostas_dir=propostas)
    assert an.proposta is not None, an.recusas
    antes_politica = caminho.read_bytes()

    # adultera o hash da primeira linha — a cadeia inteira passa a não conferir
    linhas = trilha.read_text(encoding="utf-8").splitlines()
    row = json.loads(linhas[0])
    row["hash"] = "0" * 64
    linhas[0] = json.dumps(row, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    trilha.write_text("\n".join(linhas) + "\n", encoding="utf-8")
    audit.reset_state()
    linhas_antes = len(_linhas(trilha))

    res = improve.aplicar(an.proposta.id, policy_path=caminho, trilha=trilha,
                          propostas_dir=propostas)

    assert not res.ok
    assert res.exit_code == 1
    assert res.recusa.codigo == improve.RECUSA_CADEIA_QUEBRADA
    # a política ficou intacta
    assert caminho.read_bytes() == antes_politica
    # e a recusa está NA TRILHA, com a razão
    registros = _linhas(trilha)
    assert len(registros) == linhas_antes + 1
    assert registros[-1]["component"] == improve.COMPONENTE_APLICAR
    assert registros[-1]["verdict"] == "fail"
    assert improve.RECUSA_CADEIA_QUEBRADA in registros[-1]["evidence"]


def test_apply_recusa_bloco_imutavel_e_registra(trilha, propostas, tmp_path):
    """Proposta forjada apontando para um bloco da seção 5.3 é recusada no portão."""
    caminho = _politica(tmp_path, "[backends]\ntimeout_s = 600\n")
    _verify(trilha)
    forjada = {
        "id": "prop-forjada",
        "criado_em": "2026-08-31T00:00:00.000000-03:00",
        "trajectory_id": "t1",
        "component": "pytest",
        "evidence": "timeout",
        "regra": "ampliar_timeout",
        "gatilho": "timeout",
        "bloco": "padroes_de_contencao",
        "campo": "sandbox",
        "valor_atual": "on",
        "valor_proposto": "off",
        "justificativa": "…",
        "remocao": "…",
        "fault_side": "infraestrutura",
        "cause_category": None,
        "schema_version": improve.PROPOSTA_SCHEMA,
        "status": "proposta",
        "aplicado_em": None,
    }
    propostas.mkdir(parents=True, exist_ok=True)
    (propostas / "prop-forjada.json").write_text(json.dumps(forjada), encoding="utf-8")
    antes = caminho.read_bytes()

    res = improve.aplicar("prop-forjada", policy_path=caminho, trilha=trilha,
                          propostas_dir=propostas)

    assert not res.ok
    assert res.recusa.codigo == improve.RECUSA_BLOCO_IMUTAVEL
    assert "padroes_de_contencao" in improve.BLOCOS_IMUTAVEIS
    assert caminho.read_bytes() == antes
    registro = _linhas(trilha)[-1]
    assert registro["component"] == improve.COMPONENTE_APLICAR
    assert registro["verdict"] == "fail"
    assert improve.RECUSA_BLOCO_IMUTAVEL in registro["evidence"]


def test_apply_recusa_trajetoria_inexistente(trilha, propostas, tmp_path):
    caminho = _politica(tmp_path, "[backends]\ntimeout_s = 600\n")
    pol = policy_mod.load(caminho)
    _verify(trilha)
    an = improve.propor(pol, trilha=trilha, propostas_dir=propostas)

    outra_trilha = tmp_path / "outra.jsonl"
    audit.reset_state()
    _verify(outra_trilha, tid="t-outra")

    res = improve.aplicar(an.proposta.id, policy_path=caminho, trilha=outra_trilha,
                          propostas_dir=propostas)

    assert not res.ok
    assert res.recusa.codigo == improve.RECUSA_TRAJETORIA_AUSENTE
    assert _linhas(outra_trilha)[-1]["verdict"] == "fail"


def test_apply_recusa_id_inexistente_como_erro_de_uso(trilha, propostas, tmp_path):
    caminho = _politica(tmp_path, "[backends]\ntimeout_s = 600\n")
    res = improve.aplicar("prop-nao-existe", policy_path=caminho, trilha=trilha,
                          propostas_dir=propostas)

    assert not res.ok
    assert res.recusa.codigo == improve.RECUSA_PROPOSTA_DESCONHECIDA
    assert res.exit_code == 2  # não chegou a rodar, não reprovou


def test_apply_recusa_quando_a_politica_ja_mudou(trilha, propostas, tmp_path):
    """Proposta calculada sobre 600s não se aplica a uma política que hoje diz 900s."""
    caminho = _politica(tmp_path, "[backends]\ntimeout_s = 600\n")
    pol = policy_mod.load(caminho)
    _verify(trilha)
    an = improve.propor(pol, trilha=trilha, propostas_dir=propostas)
    caminho.write_text("[backends]\ntimeout_s = 900\n", encoding="utf-8")

    res = improve.aplicar(an.proposta.id, policy_path=caminho, trilha=trilha,
                          propostas_dir=propostas)

    assert not res.ok
    assert res.recusa.codigo == improve.RECUSA_VALOR_DIVERGENTE
    assert policy_mod.load(caminho).backends.timeout_s == 900


def test_apply_de_proposta_com_schema_futuro_e_recusado(trilha, propostas, tmp_path):
    caminho = _politica(tmp_path, "[backends]\ntimeout_s = 600\n")
    propostas.mkdir(parents=True, exist_ok=True)
    (propostas / "prop-futuro.json").write_text(
        json.dumps({"schema_version": 999, "bloco": "backends", "campo": "timeout_s",
                    "trajectory_id": "t1", "component": "x"}),
        encoding="utf-8",
    )
    res = improve.aplicar("prop-futuro", policy_path=caminho, trilha=trilha,
                          propostas_dir=propostas)

    assert not res.ok
    assert res.recusa.codigo == improve.RECUSA_SCHEMA


# ---------------------------------------------------------------------------
# 5. Escritor TOML mínimo
# ---------------------------------------------------------------------------

def test_escreve_chave_substitui_valor_existente():
    texto = "[meta]\nversao = 1\n\n[backends]\ntimeout_s = 600\narg_limit = 24000\n"
    novo = improve.escreve_chave(texto, "backends", "timeout_s", 1200)

    assert "timeout_s = 1200" in novo
    assert "arg_limit = 24000" in novo
    assert "versao = 1" in novo


def test_escreve_chave_cria_bloco_ausente():
    novo = improve.escreve_chave("[meta]\nversao = 1\n", "backends", "timeout_s", 1200)
    assert "[backends]" in novo
    assert "timeout_s = 1200" in novo


def test_escreve_chave_substitui_array_multilinha():
    texto = '[falha]\nassinaturas = [\n  "a",\n  "b",\n]\nmin_chars = 40\n'
    novo = improve.escreve_chave(texto, "falha", "assinaturas", ["a", "b", "c"])

    assert "min_chars = 40" in novo
    assert tomllib.loads(novo)["falha"]["assinaturas"] == ["a", "b", "c"]


def test_escritor_recusa_tipo_nao_suportado():
    with pytest.raises(improve.TomlNaoSuportado):
        improve.escreve_chave("", "backends", "timeout_s", {"a": 1})
    with pytest.raises(improve.TomlNaoSuportado):
        improve.escreve_chave("", "backends", "timeout_s", True)


def test_apply_restaura_politica_se_o_resultado_nao_carregar(trilha, propostas, tmp_path, monkeypatch):
    """Nunca se deixa para trás uma política quebrada."""
    caminho = _politica(tmp_path, "[backends]\ntimeout_s = 600\n")
    pol = policy_mod.load(caminho)
    _verify(trilha)
    an = improve.propor(pol, trilha=trilha, propostas_dir=propostas)
    antes = caminho.read_bytes()

    monkeypatch.setattr(improve, "escreve_chave",
                        lambda *a, **k: "[backends]\ntimeout_s = isto nao e toml\n")
    res = improve.aplicar(an.proposta.id, policy_path=caminho, trilha=trilha,
                          propostas_dir=propostas)

    assert not res.ok
    assert res.recusa.codigo == improve.RECUSA_POLITICA_INVALIDA
    assert caminho.read_bytes() == antes


# ---------------------------------------------------------------------------
# 6. Espelhos e invariantes estruturais
# ---------------------------------------------------------------------------

def test_blocos_externalizaveis_espelham_policy():
    """Se `Policy` ganhar um bloco, este teste falha até o portão saber dele."""
    campos = {f.name for f in fields(policy_mod.Policy)} - {"origem", "fonte"}
    assert set(improve.BLOCOS_EXTERNALIZAVEIS) == campos


def test_nenhum_bloco_imutavel_e_externalizavel():
    assert not set(improve.BLOCOS_IMUTAVEIS) & set(improve.BLOCOS_EXTERNALIZAVEIS)


def test_toda_regra_aponta_para_bloco_externalizavel(pol):
    for regra in improve.REGRAS:
        assert regra.bloco in improve.BLOCOS_EXTERNALIZAVEIS
        assert hasattr(getattr(pol, regra.bloco), regra.campo)


def test_timeout_nao_passa_do_teto(trilha, propostas, tmp_path):
    caminho = _politica(tmp_path, f"[backends]\ntimeout_s = {improve.TETO_TIMEOUT_S}\n")
    pol = policy_mod.load(caminho)
    _verify(trilha)

    an = improve.propor(pol, trilha=trilha, propostas_dir=propostas)

    assert an.proposta is None
    assert an.recusas[0].codigo == improve.RECUSA_SEM_MUDANCA


# ---------------------------------------------------------------------------
# 7. CLI
# ---------------------------------------------------------------------------

def test_cli_improve_devolve_0_com_proposta_e_1_sem(trilha, propostas, tmp_path, capsys):
    from fpch import cli

    caminho = _politica(tmp_path, "[backends]\ntimeout_s = 600\n")
    _verify(trilha)
    code = cli.main(["--policy", str(caminho), "improve",
                     "--trilha", str(trilha), "--propostas", str(propostas)])
    assert code == 0
    saida = capsys.readouterr().out
    assert "NADA foi alterado na política" in saida
    assert caminho.read_text(encoding="utf-8") == "[backends]\ntimeout_s = 600\n"

    audit.reset_state()
    outra = tmp_path / "vazia.jsonl"
    assert cli.main(["--policy", str(caminho), "improve",
                     "--trilha", str(outra), "--propostas", str(propostas)]) == 1


def test_cli_apply_sem_policy_e_erro_de_uso(trilha, propostas, capsys):
    from fpch import cli

    code = cli.main(["improve", "--apply", "prop-x",
                     "--trilha", str(trilha), "--propostas", str(propostas)])
    assert code == 2
    assert "nomeie o arquivo" in capsys.readouterr().err


def test_cli_apply_aplica_a_proposta(trilha, propostas, tmp_path, capsys):
    from fpch import cli

    caminho = _politica(tmp_path, "[backends]\ntimeout_s = 600\n")
    _verify(trilha)
    cli.main(["--policy", str(caminho), "improve",
              "--trilha", str(trilha), "--propostas", str(propostas)])
    ident = json.loads(next(propostas.glob("*.json")).read_text(encoding="utf-8"))["id"]
    capsys.readouterr()

    code = cli.main(["--policy", str(caminho), "improve", "--apply", ident,
                     "--trilha", str(trilha), "--propostas", str(propostas)])

    assert code == 0
    assert policy_mod.load(caminho).backends.timeout_s == 1200
