# FPCH — PoC (C1–C3)

> 🧭 **Mapa de docs:** [`AGENTS.md`](../AGENTS.md) · [`docs/PROJECT.md`](../docs/PROJECT.md) · [`docs/STATUS.md`](../docs/STATUS.md) · [`TODO.md`](../TODO.md) · [`docs/analises/fpch-evidencias-desenvolvimento.md`](../docs/analises/fpch-evidencias-desenvolvimento.md) · [`docs/analises/camada-multi-modelo.md`](../docs/analises/camada-multi-modelo.md) · [`docs/referencias/estado-da-arte-2026-07.md`](../docs/referencias/estado-da-arte-2026-07.md)

Código do FPCH. Nasceu cobrindo **C1** (camada de acesso multi-modelo) e a ferramenta **cannibalize**; desde a fatia funcional de 01/09/2026 instancia **as cinco camadas do framework** — política (`policy.py`), hooks determinísticos (`hooks.py`), laço de autoaprimoramento (`improve.py`), trilha verificável (`audit.py` + `fpch audit --verify`) e acesso multi-modelo (`router.py`/`backends.py`/`models.py`). Em 09/09/2026, C2 acrescentou a exploração automática em `discovery.py` e C3, a captura explícita de preferências em `interview.py`. **14 módulos, 6.686 linhas físicas de código; 14 arquivos e 4.977 linhas físicas de testes; 369 testes passando e 3 ignorados** (medido em 09/09/2026).

**Status:** C1 funcional e verificado ponta a ponta desde 16/07/2026; C2 e C3 concluídas em 09/09/2026. C4 fará a geração e instalação. Stack: **Python ≥3.11 + uv**. Zero dependências de runtime — só a stdlib e os CLIs oficiais já instalados.

## Uso

```bash
uv run fpch models                    # catálogo, agrupado por pool de cota
uv run fpch plan hard                 # mostra a cadeia de escalada, sem gastar cota
uv run fpch discover . --json         # explora manifestos e CI/CD sem modificar o repo
uv run fpch interview .               # captura preferências explícitas em TTY
uv run fpch interview . --answers preferences.json --json
uv run fpch interview . --answers - --output preferences.json
uv run fpch ask standard "prompt"     # roteia
uv run fpch ask hard --file spec.md   # prompt longo vem de arquivo
uv run fpch audit                     # para onde a cota foi
uv run fpch audit --causes            # falhas agregadas por causa raiz e por fonte
uv run fpch audit --process           # marcos t_err/t_lock/t_obs por trajetória
uv run fpch audit --verify            # confere o encadeamento de hashes da trilha
```

## Exploração automática (`discover`)

`fpch discover <repo> --json` é o primeiro estágio da face de criação. Ele analisa, de forma **estática, offline, determinística e somente leitura**, `pyproject.toml`, `requirements*.txt`, `package.json`, `Cargo.toml`, `go.mod` e configurações de GitHub Actions, GitLab CI, Jenkins e Azure Pipelines. Em Python, distingue dependências do projeto, opcionais e grupos PEP 735.

O comando não executa subprocessos, não acessa a rede e não escreve no repositório inspecionado. Cada manifesto tem limite de 1 MiB; *symlinks*, *junctions* e demais *reparse points* são ignorados; problemas de leitura ou análise viram avisos estruturados. No dogfood do FPCH, detectou Python e `pytest >=8` em `pyproject.toml`, no escopo `group:dev`, sem CI/CD nem avisos, com exit 0.

Limites assumidos neste corte: não interpreta configurações específicas de Poetry/PDM, *workspaces* nem classifica semanticamente as dependências. C3 faz a entrevista; C4 fará a geração e instalação. Evidências reproduzíveis e ressalvas estão em [`docs/analises/fpch-evidencias-desenvolvimento.md`](../docs/analises/fpch-evidencias-desenvolvimento.md).

## Entrevista (`interview`)

`fpch interview <repo> [--answers PATH|-] [--json] [--output PATH]` é o segundo estágio da face de criação. O modelo `FpchInterviewPreferences` usa esquema fechado versão 1 e registra exatamente três listas: `skills_on_demand`, `mandatory_linters` e `mandatory_formatters`. A descoberta C2 fornece contexto, mas **não pré-seleciona** respostas; toda preferência precisa ser informada pelo operador.

Sem `--answers`, o comando entrevista em TTY. Com `--answers PATH`, lê JSON de um arquivo; com `--answers -`, lê stdin como bytes e exige UTF-8 válido. O JSON é fechado e canônico: campos extras ou ausentes e versões desconhecidas falham; o documento tem limite de 64 KiB, cada lista aceita até 64 IDs, os identificadores usam um conjunto ASCII conservador e a ordem declarada é preservada. Mensagens de duplicidade não ecoam o identificador recebido.

O comando é offline: não executa subprocessos, não usa rede, não instala componentes e, por padrão, apenas apresenta o resultado. Só grava quando `--output` é fornecido explicitamente. Essa gravação é criação exclusiva, com modo `0600`; recusa alvo ou ancestral que seja *symlink*, *junction* ou outro *reparse point*, e remove uma saída parcial somente se ela ainda tiver a identidade do arquivo criado pela própria execução. Permanece um limite TOCTOU inerente no intervalo entre verificações do caminho e chamadas ao sistema de arquivos; portanto, isto é contenção defensiva, não garantia formal contra um adversário local concorrente.

O smoke test real foi não interativo e usou **respostas sintéticas** (`context-mode:ctx-search`, `ruff`, `black`), não preferências do autor; também não forneceu `--output`, portanto não persistiu arquivo. C4 continuará responsável por gerar e instalar o harness.

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

`~/.fpch/audit.jsonl`, append-only, encadeada por hash. Serve a três coisas: saber qual pool está drenando; **produzir o dado empírico que o TCC não tem** (lacuna nº 5 do inventário); e reconstruir o que um agente fez, depois do fato.

**Esquema 3 (03/09/2026).** Três acréscimos, cada um respondendo a uma pergunta que a trilha antes não respondia:

- **Causa raiz com vocabulário fechado** — 3 categorias e 9 subtipos adotados de Zhao et al. (2026), validados na construção do evento. Antes eram string livre: um erro de digitação fazia a categoria sumir do agregado sem aviso. Agora `fpch audit --causes` responde *por que* falhou, não só *quantas vezes*.
- **Atribuição de fonte** (`source`/`source_ref`) — de onde veio cada injeção de contexto: `nlah`, `politica`, `hook`, `mcp`, `memoria`, `skill`, `catalogo`, `usuario`, mais o identificador concreto. É o campo que permite responder **qual parte do harness produziu o efeito**; sem ele, "o harness ajudou" é anedota.
- **Marcos do processo de falha** (`t_err`/`t_lock`/`t_obs`) — anotados por evento `annotate`, que aponta o `seq` alvo e **entra na cadeia de hash**. Marco é retrospectivo e a trilha é append-only, então anotar nunca reescreve: rotular em retrospecto fica, ele próprio, auditável. `fpch audit --process` calcula janela de correção e atraso de observabilidade.

⚠️ **Limite declarado:** as janelas são medidas em **eventos da trilha**, não em turnos internos do agente. Zhao et al. medem passos de raciocínio-e-ação dentro de uma execução; o FPCH registra uma linha por chamada de backend. A adoção é da estrutura conceitual, não da unidade de medida — comparar os dois números seria erro de leitura.

**Invariante de segurança:** evento `install` sem o campo `inverse` levanta `ValueError`. Instalar sem registrar como desinstalar é o que o requisito de composabilidade temporal proíbe. O executor (`fpch undo`) ainda não existe — só o campo e a invariante.

## Limites conhecidos (16/07/2026)

- `agy models` **trava** — o catálogo foi extraído via `agy --model __invalido__`. Se a Google mudar os nomes, `models.py` quebra em silêncio. Falta um teste de contrato.
- `gemini` CLI **inutilizável** nesta conta (`IneligibleTierError`) — não está no catálogo.
- `ollama` e `litellm` **não instalados** — a camada API-key (LiteLLM) ainda não existe. Só a camada assinatura está implementada.
- Custo/potência dos modelos são **juízo do autor**, não medição. O audit log existe justamente para substituir isso por dado.
- ~~Sem testes automatizados ainda.~~ **Superado:** 339 testes aprovados e 1 ignorado em `tests/` (13 arquivos, 4.552 linhas), `uv run --with pytest pytest tests/ -q`. O único *skip* ocorreu porque o Windows não permitiu criar o *symlink* do E2E de escape; a contenção de *reparse points* está implementada. Os demais limites desta lista são de 16/07/2026 e não foram reconferidos.
