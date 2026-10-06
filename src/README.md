# FPCH — PoC (C1–C4a + C16/C7 + C17 + C18)

> 🧭 **Mapa de docs:** [`AGENTS.md`](../AGENTS.md) · [`docs/PROJECT.md`](../docs/PROJECT.md) · [`docs/STATUS.md`](../docs/STATUS.md) · [`TODO.md`](../TODO.md) · [`docs/analises/fpch-evidencias-desenvolvimento.md`](../docs/analises/fpch-evidencias-desenvolvimento.md) · [`docs/analises/camada-multi-modelo.md`](../docs/analises/camada-multi-modelo.md) · [`docs/referencias/estado-da-arte-2026-07.md`](../docs/referencias/estado-da-arte-2026-07.md)

> **Publicação (06/10/2026):** o código canônico vive no repositório da pesquisa; <https://github.com/LucasPetersCG/fpch> (público, MIT) é uma cópia que recebe as mudanças por sincronização (`tools/fpch_sync_public.py`, no repositório da pesquisa). Os *hashes* diferem entre os dois: o protótipo avaliado no TCC é `58c288a` no repositório da pesquisa e `3c7d800` no público.

Código do FPCH. Nasceu cobrindo **C1** (camada de acesso multi-modelo) e a ferramenta **cannibalize**; desde a fatia funcional de 01/09/2026 instancia **as cinco camadas do framework** — política (`policy.py`), hooks determinísticos (`hooks.py`), laço de autoaprimoramento (`improve.py`), trilha verificável (`audit.py` + `fpch audit --verify`) e acesso multi-modelo (`router.py`/`backends.py`/`models.py`). Em 09/09/2026, C2 acrescentou a exploração automática em `discovery.py`, C3 a captura explícita de preferências em `interview.py` e C4a o setup local com transação compensatória journalada em `setup.py`; em 13/09/2026, C16 acrescentou o checkpoint MCP declarativo em `mcp.py`, um segundo corte no mesmo dia acrescentou a verificação offline de artefato MCP (`verify_artifact`), um terceiro corte, também no mesmo dia, acrescentou a varredura de Unicode oculto (`scan_metadata`) e o portão de evidência MCP verificada em `setup apply`, e um quarto corte concluiu **C17** — numa segunda rodada no mesmo corte, revisão independente corrigiu um docstring que superafirmava o alcance do gate de aprovação de `cannibalize.py` (commit `eace234`). Em 14/09/2026, **C18** trocou a `_FAILURE_SIGNATURES` como linha principal de defesa de `backends.invoke`/`route` por um **contrato de saída por sentinela** — nonce por chamada, fail-closed, independente do texto do fornecedor (commit `6e0285a`). **16 módulos, 11.378 linhas físicas de código; 20 arquivos e 10.797 linhas físicas de testes; 865 testes passando e 12 ignorados em 14,01 s** (medido em 14/09/2026, após C18).

**Status:** C1 funcional e verificado ponta a ponta desde 16/07/2026; C2 e C3 concluídas em 09/09/2026; C16 e C17 concluídas em 13/09/2026; C18 concluída em 14/09/2026; C4a implementada como fatia parcial local. C4 e C7 permanecem parciais: o plano aceita checkpoints MCP, `fpch mcp verify` confere o artefato obtido localmente contra o checkpoint, `fpch mcp scan` varre metadados MCP em busca de Unicode oculto, e `fpch setup apply` já aceita evidência verificada de ambos como precondição para aplicar um plano com checkpoint — mas nada é baixado, resolvido, executado ou instalado. Stack: **Python ≥3.11 + uv**. Zero dependências de runtime — só a stdlib e os CLIs oficiais já instalados.

## Uso

```bash
uv run fpch models                    # catálogo, agrupado por pool de cota
uv run fpch plan hard                 # mostra a cadeia de escalada, sem gastar cota
uv run fpch discover . --json         # explora manifestos e CI/CD sem modificar o repo
uv run fpch interview .               # captura preferências explícitas em TTY
uv run fpch interview . --answers preferences.json --json
uv run fpch interview . --answers - --output preferences.json
uv run fpch setup plan . --answers preferences.json --json --output fpch-setup-plan.json
uv run fpch setup plan . --answers preferences.json --mcp-checkpoint filesystem.json --json
uv run fpch setup apply fpch-setup-plan.json --confirm <plan_id>
uv run fpch setup apply fpch-setup-plan.json --confirm <plan_id> \
    --mcp-artifact filesystem=artefato.bin --mcp-metadata filesystem=metadados.json
uv run fpch mcp verify filesystem.json artefato.bin --json  # confere SHA-256 + nome de arquivo, offline
uv run fpch mcp scan metadados.json --json                  # varre Unicode oculto, offline
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

Limites assumidos neste corte: não interpreta configurações específicas de Poetry/PDM, *workspaces* nem classifica semanticamente as dependências. C3 faz a entrevista; C4a consome ambos os snapshots para gerar artefatos locais. Evidências reproduzíveis e ressalvas estão em [`docs/analises/fpch-evidencias-desenvolvimento.md`](../docs/analises/fpch-evidencias-desenvolvimento.md).

## Entrevista (`interview`)

`fpch interview <repo> [--answers PATH|-] [--json] [--output PATH]` é o segundo estágio da face de criação. O modelo `FpchInterviewPreferences` usa esquema fechado versão 1 e registra exatamente três listas: `skills_on_demand`, `mandatory_linters` e `mandatory_formatters`. A descoberta C2 fornece contexto, mas **não pré-seleciona** respostas; toda preferência precisa ser informada pelo operador.

Sem `--answers`, o comando entrevista em TTY. Com `--answers PATH`, lê JSON de um arquivo; com `--answers -`, lê stdin como bytes e exige UTF-8 válido. O JSON é fechado e canônico: campos extras ou ausentes e versões desconhecidas falham; o documento tem limite de 64 KiB, cada lista aceita até 64 IDs, os identificadores usam um conjunto ASCII conservador e a ordem declarada é preservada. Mensagens de duplicidade não ecoam o identificador recebido.

O comando é offline: não executa subprocessos, não usa rede, não instala componentes e, por padrão, apenas apresenta o resultado. Só grava quando `--output` é fornecido explicitamente. Essa gravação é criação exclusiva, com modo `0600`; recusa alvo ou ancestral que seja *symlink*, *junction* ou outro *reparse point*, e remove uma saída parcial somente se ela ainda tiver a identidade do arquivo criado pela própria execução. Permanece um limite TOCTOU inerente no intervalo entre verificações do caminho e chamadas ao sistema de arquivos; portanto, isto é contenção defensiva, não garantia formal contra um adversário local concorrente.

O smoke test real foi não interativo e usou **respostas sintéticas** (`context-mode:ctx-search`, `ruff`, `black`), não preferências do autor; também não forneceu `--output`, portanto não persistiu arquivo. C4a agora consome essas respostas apenas para planejar e criar artefatos locais conhecidos.

## Setup local e checkpoint MCP declarativo (`setup`) — C4/C7 parciais

`fpch setup plan <repo> --answers PATH|- [--json] [--output PATH]` redescobre o repositório, valida respostas C3 e produz um plano canônico para `AGENTS.md`, `CLAUDE.md` e `.fpch/setup-manifest.json`. O plano contém os snapshots C2/C3 e seus hashes, a identidade física da raiz, o conteúdo e estado de cada artefato e um `plan_id` que cobre o contrato inteiro. Planejar é somente leitura no alvo; `--output` apenas cria explicitamente um arquivo de plano e nunca o sobrescreve.

`fpch setup apply PLAN_PATH --confirm PLAN_ID [--trilha CAMINHO]` exige confirmação literal do `plan_id`, revalida a identidade da raiz, repete C2 e compara o snapshot, além de conferir novamente os estados dos artefatos. A aplicação é *create-only*: conteúdo igual fica `unchanged`, conteúdo diferente ou caminho inseguro vira conflito e nada é sobrescrito.

`--mcp-checkpoint CAMINHO` pode ser repetido no comando `plan`. Cada arquivo deve conter exatamente `name`, `source`, `version` e `expected_sha256` em JSON; o loader impõe 1 MiB, esquema fechado, URL HTTPS canônica/coerente e contenção de *links/reparse points* com snapshot. Todos os campos entram no `plan_id`, e a ordenação canônica mantém o mesmo conjunto determinístico. Planejar continua offline e *plan-only*.

A transação usa *lock* e *journal* em `.fpch/`, com criação exclusiva e `fsync` a cada mudança de estado. Em falha antes da auditoria, arquivos ainda idênticos aos criados pela transação são movidos para quarentena e removidos em ordem inversa; falha ao gravar o evento `install` também aciona rollback. O evento registra uma inversa estruturada, mas `fpch undo` ainda não existe (C25). `audit.write` serializa escritores por *lock* de thread e processo e só confirma após `fsync` do arquivo e, quando suportado, do diretório.

O regime suportado é **trusted single writer**. Uma troca hostil de ancestrais durante chamadas ao sistema fica fora do modelo; alterações detectadas falham fechadas. Se houver interrupção no intervalo ambíguo em que a auditoria pode ter sido escrita, o estado `auditing` exige recuperação explícita, pois repetir ou desfazer automaticamente poderia falsificar o resultado. Um plano com checkpoint MCP só é aceito por `setup.apply` quando acompanhado de evidência verificada para cada checkpoint (ver seção seguinte); sem ela, a aplicação é recusada antes de qualquer mutação. Não há *download*, rede, resolução, execução, instalação nem catálogo C15. C16 está concluída; C4 e C7 permanecem parciais; C15, C17, C23 e C25 continuam abertos.

### Verificação offline de artefato MCP (`mcp verify`) — 2º corte de C7

`fpch mcp verify <checkpoint.json> <artefato> [--json]` confere um artefato MCP que o operador já obteve localmente contra o checkpoint declarativo de C16, sem tocar rede. A API em `mcp.py` é `verify_artifact(checkpoint, path) -> FpchMcpArtifactVerification` e `dumps_verification`; o teto de leitura é `FPCH_MCP_ARTIFACT_MAX_BYTES` = **256 MiB**. Códigos de saída: **0** verificado; **1** *digest* ou nome de arquivo divergente do checkpoint (o resultado ainda é impresso); **2** checkpoint inválido, caminho inseguro ou erro de leitura.

A verificação faz dois testes independentes: um *hash* SHA-256 por *streaming*, comparado por `hmac.compare_digest` contra `expected_sha256`; e o nome do arquivo tal como gravado em disco — obtido via `os.scandir` mais identidade de *stat*, não pelo argumento de linha de comando — que precisa igualar exatamente, inclusive em maiúsculas/minúsculas, o último segmento de `source`.

O endurecimento reaproveita e compartilha com `load` o mesmo leitor defensivo: caminho absoluto obtido lexicamente (`abspath`, sem seguir o sistema de arquivos via `resolve`); recusa de *links*, *junctions* e demais *reparse points* no caminho e em cada ancestral, checados antes e depois da leitura; comparação de *snapshot* do arquivo entre as duas checagens; `ValueError` (NUL embutido no caminho) convertido em `FpchMcpError`; e um objeto de resultado que revalida seus campos e recalcula as próprias *flags* na construção, de modo que uma instância forjada ou adulterada por fora falhe, inclusive em `dumps_verification`.

O que este corte **não** faz: acessar rede, baixar, resolver URL, executar, extrair, instalar ou escrever. `setup.apply` continua recusando qualquer plano com checkpoint MCP, independentemente de uma verificação bem-sucedida — `verified=True` prova que os bytes batem com o checkpoint, não que a fonte é confiável.

**Limites residuais:** uma reescrita *in-place* que preserve tamanho e restaure `mtime` não é detectada pela comparação de *snapshot*; janela pequena entre a última checagem de ancestral e o fechamento do arquivo; *hardlinks* para o mesmo conteúdo são aceitos; o Windows não oferece `O_NOFOLLOW` (mitigado por checagem de identidade pós-abertura); nomes curtos 8.3 do Windows e *hardlinks* que só diferem em caixa são recusados; sem garantia formal contra escritor local concorrente.

### Varredura de Unicode oculto em metadados MCP (`mcp scan`) — 3º corte de C7

`fpch mcp scan <arquivo> [--json] [--format auto|json|text]` varre metadados MCP (o que um cliente vê em `tools/list`, ou um manifesto) em busca de Unicode oculto — ameaça descrita em arXiv:2607.05744. Saída **0** limpo; **1** achado (o resultado ainda é impresso); **2** erro. API em `mcp.py`: `scan_metadata`, `scan_bytes`, `classify_code_point`, `dumps_scan`, `read_metadata_json`, com os tipos `FpchMcpMetadataScan` e `FpchMcpUnicodeFinding` (campos incluem `escaped` e `pointer_truncated`). *Ruleset* `fpch-hidden-unicode-1`.

Classes sinalizadas, todas fail-closed: `tag`, `bidi`, `zero_width`, `variation_selector`, `control`, `line_separator`, `invisible_filler` (inclusive U+2800), `noncharacter`, `surrogate`, `private_use`, `other_format` (Cf) e `unassigned` (Cn). **Não há exceção para emoji** — bandeira usa TAG, e ZWJ/VS16 também são sinalizados. Um BOM inicial é permitido e registrado como `leading_bom`, não como achado.

O modo JSON varre chaves e strings decodificadas; chave duplicada, `NaN` e profundidade acima de 256 são rejeitados. O modo texto também detecta escapes `\uXXXX`/`\u{...}`, marcados "escapado" — essa detecção ignora contexto de citação, fonte conhecida de falso positivo. Os apontadores de achado são sanitizados (caractere sinalizado vira U+FFFD) e limitados a 1.024 caracteres, podendo não resolver sob RFC 6901 quando truncados. Achados detalhados são limitados a 100; os totais por classe permanecem exatos. `unicode_version` é registrado. Limite de 1 MiB, mesmo leitor defensivo. **Membros de arquivo compactado não são varridos** — um cliente MCP real só vê `tools/list` em tempo de execução, e decodificar compactados em memória traz risco de *bomb* e de diferença de *parser*.

### Portão de evidência MCP verificada em `setup apply` — C4/C7

`fpch setup apply plano.json --confirm <id> --mcp-artifact NOME=CAMINHO --mcp-metadata NOME=CAMINHO` — ambas repetíveis, devem cobrir cada checkpoint do plano exatamente uma vez; nome desconhecido ou duplicado retorna saída 2. Na API: `apply(..., mcp_evidence={name: FpchMcpApplyEvidence(artifact, metadata)})`; `FpchSetupResult.mcp_verified` lista os nomes verificados.

Para cada checkpoint, a aplicação exige `verify_artifact` verificado **e** uma varredura de metadados limpa — a varredura é obrigatória, **sempre em modo JSON independentemente da extensão do arquivo**, e o documento não pode ser vazio. A checagem roda antes do *lock* e é reconferida dentro do *lock*, antes da auditoria, inclusive no ramo *no-op*; uma diferença aciona rollback. Caminhos de artefato e de metadados dentro de `.fpch` são recusados, por comparação léxica e por `realpath` (com tratamento de prefixos do Windows).

Sucesso grava `.fpch/mcp-verification.json` (esquema 1, canônico, `installed: false`, sem caminhos absolutos) por criação exclusiva, e o manifesto de setup passa a **versão 2** com `"mcp": {checkpoints, evidence_path, installed: false}`. Rótulo de auditoria: `"fpch setup apply (MCP verificado, não instalado)"`; **verificações que falham não são auditadas**. Planos sem checkpoint continuam byte-idênticos (manifesto v1). `fpch setup plan` com checkpoint continua retornando saída 1 (`is_applicable` falso) mesmo com este portão — assimetria registrada como ponto **[AUTOR]**, não decidida por este documento.

Nada é instalado ou executado: sem *download*, rede, resolução, escrita de `.mcp.json`, *allowlist*, catálogo C15 ou executor C25. Limites residuais e as escolhas de *default* tomadas (metadados obrigatórios, BOM permitido, teto de 1 MiB, verificação falha não auditada, caminhos fora do `plan_id`, saída de `plan` inalterada) estão detalhados em [`docs/analises/fpch-evidencias-desenvolvimento.md`](../docs/analises/fpch-evidencias-desenvolvimento.md) §20–24, junto da evidência de processo (revisão independente: 1 bloqueador achado e corrigido, 8 riscos; *smoke test* ponta a ponta).

## Canibalização (`canib`) — gate na fronteira do módulo (C17)

`cannibalize.run(target)` não confia no objeto `Target` recebido: antes de montar qualquer prompt ou tocar o roteador, relê a fila persistida (`~/.fpch/cannibalize.json`), localiza o registro por `target.id` (ausente ou duplicado → recusa) e exige que o **status persistido** seja `accepted` e que `url`/`kind`/`note`/`focus`/`local_path`/`status` coincidam com o objeto recebido — divergência recusa nomeando os campos, nunca os valores, via `TargetApprovalMismatchError(TargetNotApprovedError)`. Daí em diante a execução usa o registro persistido, não o objeto do chamador. Isso fecha o contorno que `Target(status="accepted")` ou `dataclasses.replace(t, status="accepted")` construídos à mão exploravam sobre o portão original (que já checava `target.status` na entrada de `run()`, desde antes deste corte).

A aprovação também é revogada por mudança posterior da fonte: `set_local_path`/`add` que alteram `local_path` de um alvo `accepted` devolvem-no a `pending` (mesmo valor não revoga nada — a aprovação fixa a **string** do caminho, não o conteúdo do diretório). Se o registro aprovado tem `local_path`, mas o caminho está ausente ou não é diretório, `run()` levanta a nova `ApprovedSourceMissingError(RuntimeError)` antes de qualquer chamada ao roteador, em vez de recuar em silêncio para a busca pela URL — a checagem é repetida antes da validação de citações, porque a fonte pode sumir durante a execução. Numa execução longa, `run()` relê e reconfere a aprovação **depois** das chamadas ao roteador e **antes** de escrever a ficha — um alvo rejeitado ou alterado no meio do caminho não ganha ficha nem vira `done`. `_load()` falha fechado com `QueueFormatError` sobre fila ilegível, topo que não é lista, item que não é objeto, chave desconhecida/ausente ou tipo errado; `_cmd_canib` na CLI captura esse erro para `list`/`accept`/`add` e devolve saída 1 com mensagem legível, enquanto `run`/`runall` já capturavam a nova exceção por já tratarem `RuntimeError`. Ids fora do formato interno (`[0-9a-f]{8}`) são escapados e cortados em 40 caracteres nas mensagens de erro, contra injeção em terminal/log.

**Limite dito com precisão** (reescrito numa segunda rodada, depois que revisão independente encontrou o docstring original superafirmando o alcance do portão — ver [`docs/analises/fpch-evidencias-desenvolvimento.md`](../docs/analises/fpch-evidencias-desenvolvimento.md) §29, quase-repetição do modo de falha de [proteção fantasma no laço](../docs/decisoes/protecao-fantasma-no-laco-2026-09-03.md)): o portão recusa, em relação à fila persistida, objetos forjados, obsoletos ou divergentes, e fontes trocadas depois da aprovação. Ele **não** distingue um `fpch canib accept` humano de código que chama a API da fila (`set_status`, `_save`), reatribui `QUEUE_PATH` ou grava `~/.fpch/cannibalize.json` diretamente — no mesmo processo ou fora dele; para o portão, tudo isso é aprovação. Aprovação fora de banda de verdade exigiria algo fora do processo, e isso não está implementado. Não há trava de arquivo, e uma rejeição tardia não desfaz chamadas de backend já feitas. Detalhe completo em [`docs/analises/fpch-evidencias-desenvolvimento.md`](../docs/analises/fpch-evidencias-desenvolvimento.md) §25–32.

Canibalizar um artefato de terceiro:

```bash
uv run fpch canib add https://github.com/x/y --note "por que importa"
uv run fpch canib accept <id>         # aprovação humana — obrigatória
uv run fpch canib run <id>            # → ficha em docs/referencias/fichamentos/
```

## Contrato de saída por sentinela (`backends.invoke`/`route`) — C18

`exit 0` **não é sucesso**. Antes de C18, `backends.invoke` só reprovava uma chamada com `exit 0` quando a saída continha uma de 8 frases literais de erro de terceiro (`_FAILURE_SIGNATURES`) ou tinha menos de 40 caracteres — se um fornecedor mudasse o texto do erro, a falha semântica passava calada, a mesma classe de risco do incidente de 16/07/2026 (mensagem de erro do `agy` analisada como artefato, 6 fichas alucinadas) e do C11.

Desde 14/09/2026, `invoke`/`route` exigem por padrão (`output_contract=True`) um **contrato positivo, independente do fornecedor**: cada chamada recebe um nonce de `secrets.token_hex(8)` e o prompt é instruído a terminar a resposta com a linha `FPCH-FIM-<nonce>` — a instrução descreve a sentinela **em partes**, nunca a cadeia inteira, para que um eco do prompt na resposta não a cumpra por acidente. A sentinela precisa ser a **última linha não vazia** da resposta (CRLF, espaço e decoração markdown tolerados); a regra de "última ocorrência" foi rejeitada por aceitar texto depois da sentinela, que é exatamente a forma de um erro de fornecedor anexado ao fim de uma resposta boa. Só o token desta chamada é removido de `Result.text`, delimitado por fronteira de palavra — sentinelas de outras chamadas, ou literais já existentes neste repositório, não são tocados.

**Ordem de decisão de falha:** `exit≠0` → contrato → *denylist*. Uma falha de contrato produz o prefixo estável `"contrato de saída não cumprido: sentinela ausente"`, mais a assinatura da *denylist* quando uma bate e um trecho saneado de até 200 caracteres. `_FAILURE_SIGNATURES` deixou de ser a linha principal de defesa e virou diagnóstico e defesa em profundidade. `Result.contract` (`"sentinela"|"nenhum"`) e `Result.failure_signature` entram na trilha sem *bump* de esquema de auditoria — campos vazios não são gravados, e um evento pré-C18 congelado por hash literal fixa a compatibilidade retroativa da cadeia. Um opt-out explícito (`output_contract=False`) existe e fica registrado como `contract="nenhum"`; nenhum chamador do FPCH o usa.

`_ARG_LIMIT`/*staging* de prompts longos passou a contar o tamanho da instrução do contrato (`FPCH_CONTRATO_INSTRUCAO_CHARS`); `prompt_chars`, gravado na trilha, continua sendo só o prompt do chamador, preservando a comparabilidade histórica da série. O adaptador do **Copilot** passou a sempre receber `-s`/`--silent` (confirmado no `--help`) — sem essa flag, as estatísticas impressas depois da resposta quebrariam a regra da última linha.

**Envelope JSON: investigado, declarado ausente.** `claude` 2.1.270, `copilot` 1.0.83 e `agy` 1.2.2 aceitam `--output-format json` no próprio `--help`, mas nenhum documenta o esquema da saída — cada adaptador declara `Regime.saida_estruturada = AUSENTE`, com a evidência do `--help` anexada. Nenhum modelo foi invocado durante o desenvolvimento deste corte, só `--help`/`--version`.

**Limites residuais, ditos com precisão:** a sentinela prova que a chamada terminou, não que o conteúdo está correto. Um modelo que ignora a instrução produz falso-falha, e cada uma custa uma escalada real de cota — **a taxa de cumprimento real por modelo não foi medida**, porque medi-la exige chamadas reais que gastam cota (ver *smoke test* real, decisão [AUTOR] pendente). `agy -p` pode imprimir texto depois da resposta, não verificado. Um modelo pode copiar o nonce de volta se ele aparecer no contexto que o modelo processa — o contrato assume um modelo que tenta cooperar e às vezes falha, não um adversário ativo. `failure_signature` só é preenchido em `exit 0`. Detalhe completo, evidência de processo (revisão independente: 0 bloqueadores, 5 riscos e 3 *nits*, todos corrigidos) e os quatro pontos [AUTOR] em aberto estão em [`docs/analises/fpch-evidencias-desenvolvimento.md`](../docs/analises/fpch-evidencias-desenvolvimento.md) §33–37.

## A ideia central: pool de cota

O mesmo binário (`agy`) fala com modelos que consomem **cotas independentes**. Rotear sem saber disso queima uma enquanto a outra sobra.

| Pool | Modelos |
|---|---|
| `agy:google` | Gemini 3.8 Flash (Low/Medium/High; 3.7 e 3.6 Flash como reserva), Gemini 3.1 Pro (Low/High), GPT-OSS 120B |
| `agy:anthropic` | Claude Sonnet 4.6, Claude Opus 4.6 — **cota à parte** |
| `copilot` | AI credits — só `default` (modo `auto`); todo `--model` explícito é recusado com exit 0 nesta conta (14/09/2026) |
| `codex` | assinatura ChatGPT/Codex — **cota à parte**; 6 entradas `<slug> (<esforço>)`, de `gpt-5.6-luna (low)` a `gpt-6-astra (high)`, com `power`/`cost` provisórios (C28) |
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
- Checkpoint MCP é intenção verificável, não autorização: o plano inclui nome, fonte, versão e SHA-256 no `plan_id`, e a aplicação falha fechada antes de mutar.
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
- ~~Sem testes automatizados ainda.~~ **Superado:** 910 testes aprovados e 12 ignorados em `tests/` (20 arquivos, 11.052 linhas), `uv run --with pytest pytest tests/ -q`, medidos em 14/09/2026 (após C27); em 05/10/2026 a coleta passou a **923 testes em 21 arquivos** (~11.120 linhas) com o `tests/test_round_metrics_hook.py`, que cobre o hook de métricas de rodada do harness de projeto (`.claude/hooks/fpch_round_metrics.py`, fora de `src/fpch/`, com o código de `src/` inalterado). *Skip* não conta como aprovação; o detalhamento reproduzível fica em [`docs/analises/fpch-evidencias-desenvolvimento.md`](../docs/analises/fpch-evidencias-desenvolvimento.md). Os demais limites desta lista são de 16/07/2026 e não foram reconferidos.
- O loader de checkpoint e o verificador de artefato (`mcp verify`) recusam *links/reparse points* e comparam snapshots, mas não oferecem segurança formal contra escritor local concorrente nem *traversal* por *handles*; *hardlinks* são aceitos e o Windows não tem `O_NOFOLLOW`. A varredura de Unicode oculto (`mcp scan`) não varre membros de arquivo compactado e a detecção de escapes em modo texto ignora contexto de citação. Metadados não têm vínculo criptográfico com o artefato que descrevem, e a evidência de `setup apply` registra `unicode_version`/`ruleset`, o que pode fazer uma reaplicação futura falhar como "divergente" após upgrade do Python.
