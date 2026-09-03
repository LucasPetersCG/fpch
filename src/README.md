# FPCH — PoC (C1)

> 🧭 **Mapa de docs:** [`AGENTS.md`](../AGENTS.md) · [`docs/PROJECT.md`](../docs/PROJECT.md) · [`docs/STATUS.md`](../docs/STATUS.md) · [`TODO.md`](../TODO.md) · [`docs/analises/camada-multi-modelo.md`](../docs/analises/camada-multi-modelo.md) · [`docs/referencias/estado-da-arte-2026-07.md`](../docs/referencias/estado-da-arte-2026-07.md)

Código do FPCH. Nasceu cobrindo **C1** (camada de acesso multi-modelo) e a ferramenta **cannibalize**; desde a fatia funcional de 01/09/2026 instancia **as cinco camadas do framework** — política (`policy.py`), hooks determinísticos (`hooks.py`), laço de autoaprimoramento (`improve.py`), trilha verificável (`audit.py` + `fpch audit --verify`) e acesso multi-modelo (`router.py`/`backends.py`/`models.py`). **12 arquivos, 4.940 linhas, 235 testes passando** (medido em 02/09/2026).

**Status:** funcional e verificado ponta a ponta em 16/07/2026. Stack escolhida nesta sessão (C1 estava aberto): **Python ≥3.11 + uv**. Zero dependências de runtime — só a stdlib e os CLIs oficiais já instalados.

## Uso

```bash
uv run fpch models                    # catálogo, agrupado por pool de cota
uv run fpch plan hard                 # mostra a cadeia de escalada, sem gastar cota
uv run fpch ask standard "prompt"     # roteia
uv run fpch ask hard --file spec.md   # prompt longo vem de arquivo
uv run fpch audit                     # para onde a cota foi
```

Canibalizar um artefato de terceiro:

```bash
uv run fpch canib add https://github.com/x/y --note "por que importa"
uv run fpch canib accept <id>         # aprovação humana — obrigatória
uv run fpch canib run <id>            # → ficha em docs/referencias/fichamentos/
```

## A ideia central: pool de cota

O mesmo binário (`agy`) fala com modelos que consomem **cotas independentes**. Rotear sem saber disso queima uma enquanto a outra sobra.

| Pool | Modelos |
|---|---|
| `agy:google` | Gemini 3.5 Flash (Low/Medium/High), Gemini 3.1 Pro (Low/High), GPT-OSS 120B |
| `agy:anthropic` | Claude Sonnet 4.6, Claude Opus 4.6 — **cota à parte** |
| `copilot` | AI credits |
| `claude` | a assinatura do host |

Política: começar pelo mais barato que plausivelmente resolve, escalar só diante de **falha objetiva** (exit≠0, vazio, timeout). Opus 4.6 **não entra na cadeia automática** — só via `--model`.

Escalada por *qualidade* seria melhor, mas exige um avaliador — e um avaliador que o agente influencia é convite a Goodhart. Juízo de qualidade fica com o humano ou com hook determinístico externo. Fundamento: Huang et al., ICLR 2024 (Tier A) — auto-correção sem feedback externo degrada.

## Fronteira legal — não cruzar

Só invocamos o **binário oficial** de cada fornecedor, em modo headless documentado por ele. Nunca lemos token, nunca forjamos header, nunca reempacotamos cota como API.

Essa é a linha real entre uso pretendido e banimento: o ilícito é **mentir sobre a identidade do cliente**, não automatizar. Anthropic cortou clientes que forjavam identidade em 04/04/2026; Google responde 403; GitHub AUP proíbe. Detalhes em [`docs/analises/camada-multi-modelo.md`](../docs/analises/camada-multi-modelo.md).

⚠️ Um ban do Google derruba **Antigravity + Gemini CLI + Code Assist juntos** — não são cotas independentes do ponto de vista da conta.

## Segurança

- Defaults: `sandbox=True`, `allow_write=False`. Sub-agente que só pesquisa não escreve.
- `subprocess` sempre com **lista de argumentos**, `shell=False`. (Não é teórico: o `Start-Process` do PowerShell estropiou um prompt multi-linha nesta mesma sessão.)
- Output de sub-agente é **dado não-confiável**, nunca instrução para o host.
- `cannibalize` ingere conteúdo **hostil por premissa** — um README pode tentar sequestrar quem o lê. Por isso: sandbox, output vira ficha `.md` (dado, não execução), e a adoção é proposta que o autor aplica à mão.

## Trilha de auditoria

`~/.fpch/audit.jsonl`, append-only. Serve a três coisas: saber qual pool está drenando; **produzir o dado empírico que o TCC não tem** (lacuna nº 5 do inventário); e reconstruir o que um agente fez, depois do fato.

## Limites conhecidos (16/07/2026)

- `agy models` **trava** — o catálogo foi extraído via `agy --model __invalido__`. Se a Google mudar os nomes, `models.py` quebra em silêncio. Falta um teste de contrato.
- `gemini` CLI **inutilizável** nesta conta (`IneligibleTierError`) — não está no catálogo.
- `ollama` e `litellm` **não instalados** — a camada API-key (LiteLLM) ainda não existe. Só a camada assinatura está implementada.
- Custo/potência dos modelos são **juízo do autor**, não medição. O audit log existe justamente para substituir isso por dado.
- ~~Sem testes automatizados ainda.~~ **Superado:** 235 testes em `tests/` (11 arquivos, 3.164 linhas), `uv run --with pytest pytest tests/ -q`. Os demais limites desta lista são de 16/07/2026 e não foram reconferidos.
