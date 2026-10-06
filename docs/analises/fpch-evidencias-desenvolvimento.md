# Evidências de desenvolvimento do FPCH

> 🧭 **Mapa de docs:** [`PROJECT.md`](../PROJECT.md) · [`STATUS.md`](../STATUS.md) · [`TODO.md`](../../TODO.md) · [`src/README.md`](../../src/README.md) · [`PROTOTIPO-GAP.md`](../PROTOTIPO-GAP.md) · [`proteção fantasma no laço`](../decisoes/protecao-fantasma-no-laco-2026-09-03.md)

> **Data:** 09/09/2026 · **Sessão:** 32 (continuação) · **Status:** registro técnico verificável e candidato de evidências para integração acadêmica futura. **Este documento não altera o TCC.** A `TCC_v6`, seus `.docx`/`.pdf`, a apresentação e o guia de estudos permanecem congelados até solicitação explícita do autor.

## 1. Resultado fechado: C2 — Exploração Automática

O subcomando `fpch discover <repo> --json` materializa o primeiro estágio da face de criação do FPCH. Seu contrato é **estático, offline, determinístico e somente leitura**: inspeciona arquivos conhecidos, devolve inventário estruturado e não executa subprocessos, não acessa a rede e não escreve no repositório analisado.

A implementação reconhece:

- `pyproject.toml`: dependências do projeto, dependências opcionais e grupos definidos por PEP 735;
- `requirements*.txt`;
- `package.json`;
- `Cargo.toml`;
- `go.mod`;
- GitHub Actions, GitLab CI, Jenkins e Azure Pipelines.

As falhas recuperáveis são representadas como avisos estruturados. Cada manifesto está limitado a 1 MiB. *Symlinks*, *junctions* e demais *reparse points* são ignorados para impedir que a exploração atravesse a fronteira física do repositório por referências do sistema de arquivos.

## 2. Dogfood no próprio repositório

Comando executado:

```text
fpch discover . --json
```

Resultado observado:

| Campo observado | Resultado |
|---|---|
| Linguagem | Python |
| Dependência | `pytest >=8` |
| Origem | `pyproject.toml` |
| Escopo | `group:dev` |
| CI/CD | lista vazia |
| Avisos | lista vazia |
| Código de saída | 0 |

Esse ensaio confirma que o FPCH consegue consumir o próprio repositório como entrada real. Ele não demonstra ainda a entrevista, a geração ou a instalação do harness: essas responsabilidades continuam separadas em C3 e C4.

## 3. Verificação executada

| Verificação | Resultado em 09/09/2026 |
|---|---|
| Testes focais de `discover` | **9 passed, 1 skipped** |
| Suíte completa | **339 passed, 1 skipped em 4,92 s** |
| Integridade textual do diff | `git diff --check` limpo |
| Código do protótipo | 13 módulos, 6.264 linhas físicas em `src/fpch/` |
| Testes | 13 arquivos, 4.552 linhas físicas |

O único teste ignorado é o E2E que cria um *symlink* apontando para fora da árvore para provar o bloqueio de escape. O Windows da execução recusou a criação do link; portanto, esse E2E ficou **condicionado ao sistema operacional**. A lógica que ignora *reparse points* está implementada e os demais testes focais passaram, mas este registro não transforma o *skip* em aprovação.

## 4. Limites deste corte

C2 não cobre:

- configuração específica de Poetry ou PDM;
- *workspaces* ou monorepos como unidade semântica;
- classificação semântica das dependências além do escopo declarado no manifesto;
- preferências do desenvolvedor, que pertencem à entrevista de C3;
- geração ou instalação de artefatos, que pertencem a C4.

Esses limites são fronteiras de escopo, não resultados concluídos. Nenhuma decisão do autor sobre o conteúdo da entrevista ou sobre o formato final de instalação foi inferida nesta sessão.

## 5. Candidatos de evidência para o TCC futuro

Quando o autor reabrir a frente acadêmica após os dias de desenvolvimento, este registro pode sustentar, com a devida reconferência:

1. a existência executável do primeiro estágio da face de criação;
2. o contrato negativo de segurança: zero subprocesso, rede e escrita durante a descoberta;
3. o dogfood em um repositório real, com origem e escopo da dependência preservados;
4. a estratégia de contenção de fronteira do sistema de arquivos;
5. os números de tamanho e teste do artefato na data do fechamento de C2;
6. a ressalva metodológica do E2E condicionado ao suporte do sistema operacional.

Antes de integrar esses pontos ao texto acadêmico, é necessário repetir as medições no estado final do código e separar claramente **evidência de funcionamento do artefato** de **validação da pesquisa**. C2 comprova comportamento técnico; por si só, não valida o arranjo fatorial nem as perguntas de pesquisa ainda abertas.

## 6. Apêndice de desenvolvimento: C3 — Entrevista

O subcomando `fpch interview <repo> [--answers PATH|-] [--json] [--output PATH]` materializa o segundo estágio da face de criação. Ele recebe o repositório como contexto, mas não transforma achados de C2 em escolhas: a descoberta não pré-seleciona *skills*, linters ou formatadores. A separação é deliberada para distinguir evidência observável do repositório de preferência declarada pelo desenvolvedor.

`FpchInterviewPreferences` fixa o esquema fechado versão 1 com três campos:

| Campo | Semântica |
|---|---|
| `skills_on_demand` | *skills* que o operador quer disponibilizar sob demanda |
| `mandatory_linters` | linters que o operador declara obrigatórios |
| `mandatory_formatters` | formatadores que o operador declara obrigatórios |

A entrevista funciona em TTY ou recebe respostas JSON por `--answers PATH`; `--answers -` lê stdin binário e exige UTF-8 válido. O documento é limitado a 64 KiB; cada campo aceita no máximo 64 IDs; os IDs seguem uma gramática ASCII conservadora e a ordem de entrada é preservada. O contrato rejeita campos extras ou ausentes, versão desconhecida, tipos inadequados e duplicidades. Para reduzir vazamento acidental por diagnóstico, a mensagem de duplicidade não ecoa o ID fornecido.

## 7. Contrato negativo e persistência explícita

C3 não executa subprocessos, não acessa a rede e não instala componentes. Sem `--output`, também não escreve: `--json` altera a representação emitida, não a persistência. Geração e instalação continuam pertencendo a C4.

Quando `--output` é solicitado explicitamente, a implementação:

1. usa criação exclusiva, sem sobrescrever arquivo existente;
2. solicita permissões `0600` para o arquivo criado;
3. recusa alvo ou ancestral identificado como *symlink*, *junction* ou outro *reparse point*;
4. se uma falha ocorre após a criação, só remove o parcial após confirmar que ainda é o mesmo objeto criado pela execução.

Essas defesas reduzem a superfície de troca de alvo e remoção indevida, mas não eliminam todas as corridas de sistema de arquivos. Há um limite TOCTOU residual entre a validação de componentes do caminho e as primitivas disponíveis no sistema operacional. A formulação correta é **contenção defensiva com limite documentado**, não segurança formal contra um adversário local concorrente.

## 8. Smoke test: o que foi e o que não foi demonstrado

O smoke test real foi executado em modo não interativo, com respostas **sintéticas**:

| Campo | Valor sintético |
|---|---|
| `skills_on_demand` | `context-mode:ctx-search` |
| `mandatory_linters` | `ruff` |
| `mandatory_formatters` | `black` |

Esses valores foram escolhidos apenas para exercitar o contrato e **não representam preferências do autor**. A execução não usou `--output` e, portanto, não persistiu arquivo. Ela demonstra que uma entrada JSON válida percorre a CLI e produz a representação esperada; não demonstra instalação, adoção das ferramentas citadas nem validação das escolhas por um usuário real.

## 9. Verificação executada após C3

| Verificação | Resultado em 09/09/2026 |
|---|---|
| Testes focais de C3 | **30 passed, 2 skipped** |
| Testes focais C2+C3 | **39 passed, 3 skipped em 0,34 s** |
| Suíte completa | **369 passed, 3 skipped em 5,24 s** |
| Código do protótipo | 14 módulos, 6.686 linhas físicas em `src/fpch/` |
| Testes | 14 arquivos, 4.977 linhas físicas |
| Integridade textual do diff | `git diff --check` limpo |
| Entregáveis acadêmicos | `docs/tcc/**` sem diff |

Os três testes ignorados são exatamente os E2Es de *symlink* que o Windows da execução não autorizou criar (`WinError 1314`): escape de fronteira em C2, alvo de saída em C3 e ancestral de saída em C3. Testes simulados de *junction/reparse point* e da limpeza condicionada à identidade do arquivo passaram. O registro preserva a distinção: um teste não executado por restrição do SO não conta como aprovado, e a simulação não substitui integralmente o E2E nativo.

## 10. Candidatos adicionais de evidência para integração acadêmica futura

Após o fechamento dos dias de desenvolvimento e nova medição no estado final, C3 poderá sustentar:

1. a existência executável do segundo estágio da face de criação;
2. a separação operacional entre fatos descobertos em C2 e preferências explicitamente declaradas em C3;
3. um esquema de preferências fechado, versionado e limitado;
4. o contrato negativo de zero subprocesso, rede, instalação e escrita implícita;
5. a estratégia defensiva de persistência explícita, incluindo seu limite TOCTOU;
6. a evidência honesta de um smoke test sintético, sem apresentá-lo como preferência do autor ou entrevista com participante.

Como em C2, esses pontos comprovam propriedades técnicas do artefato, não validam por si mesmos a pesquisa, o arranjo fatorial ou a adequação das preferências a um desenvolvedor real. C4 permanece responsável por transformar descoberta e preferências em artefatos gerados e instaláveis.

## 11. Apêndice de desenvolvimento: C4a — setup local parcial

Em 09/09/2026, `fpch setup plan/apply` implementou a primeira fatia de C4 sem fechá-la. O plano determinístico e autocontido consome os snapshots canônicos de C2 e C3, registra seus hashes e a identidade física da raiz e descreve três artefatos locais: `AGENTS.md`, `CLAUDE.md` e `.fpch/setup-manifest.json`. O `plan_id` cobre o conteúdo lógico do plano. Planejar não altera o repositório-alvo; a persistência opcional do próprio plano é explícita e *create-only*.

A aplicação exige confirmação literal do `plan_id`, redescobre o repositório e revalida identidade da raiz, snapshot C2 e estados observados. A política é *create-only*: um arquivo idêntico é preservado, enquanto conteúdo divergente, objeto não regular ou caminho inseguro vira conflito sem sobrescrita.

## 12. Transação, auditoria e limites de concorrência

A aplicação usa *lock* e *journal* durável em `.fpch/`. Cada avanço do journal recebe `fsync`; antes da auditoria, uma falha move apenas artefatos ainda identificáveis como próprios da transação para quarentena, verifica identidade e hash e os remove em ordem inversa. Falha de auditoria também aciona rollback. O evento `install` inclui uma inversa estruturada com arquivos, hashes, modos, ordem e identidade da raiz, mas o executor `fpch undo` continua pendente em C25.

O escritor da trilha foi endurecido com *lock* de thread e processo, revalidação da identidade do arquivo de lock e `fsync` do arquivo e, quando suportado, do diretório. O modelo declarado é **trusted single writer**, não defesa formal contra um escritor local hostil concorrente. Uma interrupção no intervalo em que a auditoria pode ter sido anexada, mas o journal ainda não chegou a `committed`, deixa resultado ambíguo; por isso o estado `auditing` falha fechado e exige recuperação explícita.

## 13. Dogfood e verificação após C4a

O dogfood foi deliberadamente limitado a `fpch setup plan` sobre este repositório. O plano detectou conflitos com artefatos já existentes e não escreveu no alvo; portanto, demonstrou classificação conservadora e ausência de sobrescrita, não aplicação bem-sucedida no próprio FPCH.

| Verificação | Resultado em 09/09/2026 |
|---|---|
| Suíte completa | **402 passed, 6 skipped** |
| Commit de implementação | `c2723b7` |
| Código do protótipo | **15 módulos, 8.747 linhas físicas em `src/fpch/`** |
| Testes | **16 arquivos, 5.810 linhas físicas** |
| Grafo graphify | **5.351 nós, 6.568 arestas, 489 comunidades** |
| Dogfood | plano apenas; conflitos detectados; zero escrita no repositório-alvo |
| Entregáveis acadêmicos | `docs/tcc/**` sem alteração |

## 14. Fronteira ainda aberta de C4

C4a gera apenas arquivos locais conhecidos. Ela não instala ferramentas, *skills* nem servidores MCP e não resolve procedência de software externo. O próximo passo coerente é o mínimo conjunto C16/C7: checkpoint determinístico de nome, fonte e versão sob uma política explícita de segurança. C4 permanece parcial; C15, C17, C23 e C25 não foram fechados.

Como C2 e C3, este registro é evidência técnica candidata a integração futura, após nova medição no estado final. Ele não altera o TCC nem transforma testes de engenharia em validação da pesquisa.

## 15. Apêndice de desenvolvimento: C16/C7 — checkpoint MCP declarativo

Em 13/09/2026, C16 foi concluída e C7 avançou apenas na fronteira de planejamento. `FpchMcpInstallCheckpoint` é um valor congelado com quatro campos obrigatórios: `name`, `source`, `version` e `expected_sha256`. Sua representação é JSON canônico de esquema fechado. A fonte deve ser uma URL HTTPS canônica, sem credenciais, fragmento ou formas ambíguas, e coerente com nome e versão; versão e SHA-256 têm representação imutável e inequívoca.

O loader limita a entrada a 1 MiB, recusa alvo e ancestrais que sejam *links*, *junctions* ou outros *reparse points* e compara snapshots do arquivo. Esse endurecimento reduz trocas detectáveis durante a leitura, mas não fornece segurança formal contra escritor local concorrente nem *traversal* baseado em *handles*.

## 16. Integração no plano e contrato negativo de aplicação

`fpch setup plan <repo> --answers PATH --mcp-checkpoint CAMINHO` aceita a opção repetidamente, normaliza a ordem dos checkpoints e inclui `name`, `source`, `version` e `expected_sha256` no conteúdo canônico coberto pelo `plan_id`. O resultado é determinístico, offline, somente leitura no alvo e *plan-only*: não baixa, resolve, executa ou instala o recurso descrito.

O limite é deliberadamente executável: `setup.apply` recusa qualquer plano que contenha checkpoint MCP antes de adquirir o caminho de mutação dos artefatos. Assim, um checkpoint não é confundido com permissão ou instalação. Não existe catálogo sob demanda de C15, *allowlist* definida, cliente de rede, resolução de pacote, consumo do artefato ou verificação do conteúdo baixado. C4 e C7 permanecem parciais; C15, C17, C23 e C25 continuam abertos.

## 17. Verificação executada após C16/C7

| Verificação | Resultado em 13/09/2026 |
|---|---|
| Suíte completa | **484 passed, 8 skipped em 8,91 s** |
| Código do protótipo | **16 módulos, 9.186 linhas físicas em `src/fpch/`** |
| Testes | **17 arquivos, 6.401 linhas físicas** |
| Grafo graphify | **5.478 nós, 6.790 arestas, 497 comunidades** |
| Efeitos MCP | zero *download*, rede, resolução, execução ou instalação |
| Aplicação | bloqueada antes de qualquer mutação quando há checkpoint MCP |
| Entregáveis acadêmicos | congelados e sem alteração nesta fatia |

O próximo corte de C7 é a fronteira de consumo/verificação anterior a qualquer instalação MCP. O desenho de *allowlist* ou catálogo não foi decidido e não deve ser inferido deste apêndice.

## 18. Apêndice de desenvolvimento: C7, 2º corte — verificação offline de artefato MCP

Ainda em 13/09/2026, no mesmo dia de C16, um segundo corte deu a C7 a verificação do artefato descrito por um checkpoint contra o arquivo obtido localmente pelo operador. `src/fpch/mcp.py` ganhou `verify_artifact(checkpoint, path) -> FpchMcpArtifactVerification` e `dumps_verification`, com `FPCH_MCP_ARTIFACT_MAX_BYTES` = **256 MiB**. O novo subcomando é `fpch mcp verify <checkpoint.json> <artefato> [--json]`, com códigos de saída 0 (verificado), 1 (*digest* ou nome de arquivo divergente do checkpoint — o resultado ainda é impresso) e 2 (checkpoint inválido, caminho inseguro ou erro de leitura).

A verificação faz dois testes independentes: um *hash* SHA-256 calculado por *streaming* e comparado por `hmac.compare_digest` contra `expected_sha256`; e a identidade do nome do arquivo tal como gravado em disco — obtida via `os.scandir` mais identidade de *stat*, não pelo argumento de linha de comando —, que precisa igualar exatamente, inclusive em maiúsculas/minúsculas, o último segmento de `source`.

O endurecimento reaproveita e estende o de C16: um leitor defensivo agora compartilhado entre `load` e `verify_artifact`; caminho absoluto obtido lexicamente (`abspath`, sem seguir o sistema de arquivos via `resolve`); recusa de *links*, *junctions* e demais *reparse points* tanto no caminho quanto em cada ancestral, checados antes e depois da leitura; comparação de *snapshot* do arquivo entre as duas checagens; conversão de `ValueError` — o caso de NUL embutido no caminho — em `FpchMcpError` da hierarquia própria do módulo; e um objeto de resultado que revalida seus campos e recalcula as próprias *flags* na construção, de modo que uma instância forjada ou adulterada por fora falhe, inclusive quando serializada por `dumps_verification`.

**O que este corte deliberadamente não faz:** não acessa rede, não baixa nada, não resolve URL, não executa, não extrai, não instala e não escreve. `setup.apply` continua recusando qualquer plano que contenha checkpoint MCP, como já fazia desde C16. `verified=True` prova apenas que os bytes do artefato correspondem ao que o checkpoint descreve — não prova que a fonte é confiável nem autoriza qualquer ação subsequente.

**Limites residuais, declarados e não resolvidos por este corte:** uma reescrita *in-place* do artefato que preserve o tamanho e restaure o `mtime` não é detectada pela comparação de *snapshot*; existe uma janela pequena entre a última checagem de ancestral e o fechamento do arquivo; *hardlinks* para o mesmo conteúdo são aceitos como se fossem o arquivo original; o Windows não oferece `O_NOFLLOW` (mitigado pela checagem de identidade pós-abertura); nomes curtos no formato 8.3 do Windows e *hardlinks* cujo nome só difere em caixa são recusados por segurança, não suportados como variantes válidas; e não há garantia formal contra um escritor local concorrente.

## 19. Verificação executada após o corte de verificação de C7

| Verificação | Resultado em 13/09/2026 |
|---|---|
| Revisão independente somente leitura (opus) | 0 problemas bloqueadores, 11 riscos identificados, todos endereçados |
| Testes de mutação | confirmaram que 4 dos testes novos detectam regressão |
| *Smoke test* manual em diretório temporário | artefato correspondente → saída 0; um byte a mais → saída 1; arquivo ausente → saída 2 |
| Suíte completa | **518 passed, 10 skipped em 8,65 s** (era 484 passed, 8 skipped) |
| Código do protótipo | **16 módulos, 9.488 linhas físicas em `src/fpch/`** |
| Testes | **18 arquivos, 7.024 linhas físicas** |
| Grafo graphify | **não recalculado nesta sessão** — seguem valendo os 5.478 nós/6.790 arestas/497 comunidades do corte de C16 |
| Efeitos de rede/instalação | nenhum — sem *download*, resolução, execução, extração ou instalação |
| Aplicação | `setup.apply` segue bloqueando qualquer plano com checkpoint MCP, independentemente de verificação prévia |
| Entregáveis acadêmicos | congelados e sem alteração nesta fatia |

Em aberto, e não decidido por este apêndice: desenho de *allowlist*, desenho de catálogo (C15) e se uma verificação bem-sucedida deve virar precondição de um futuro `setup apply`. Candidato ao próximo corte de C7, não compromisso: varredura determinística de metadados/manifesto MCP em busca de Unicode oculto, incluindo blocos TAG (arXiv:2607.05744) e caracteres *bidi*/de largura zero.

## 20. Apêndice de desenvolvimento: C7, 3º corte — varredura de Unicode oculto em metadados MCP

Ainda em 13/09/2026, terceiro corte do dia, o autor aprovou implementar os dois candidatos deixados em aberto pelo §19: a varredura de Unicode oculto (a) e o portão de evidência MCP verificada em `setup apply` (b, ver §22). C7 permanece **parcial**.

`fpch mcp scan <arquivo> [--json] [--format auto|json|text]` varre metadados MCP (o retorno de `tools/list` ou um manifesto) em busca de Unicode oculto, com a ameaça descrita em arXiv:2607.05744 como referência. Saída 0 = limpo; 1 = achado, com o resultado ainda impresso; 2 = erro. A API em `src/fpch/mcp.py` ganhou `scan_metadata`, `scan_bytes`, `classify_code_point`, `dumps_scan` e `read_metadata_json`, além dos tipos `FpchMcpMetadataScan` e `FpchMcpUnicodeFinding` (este com os campos `escaped` e `pointer_truncated`). O *ruleset* aplicado é `fpch-hidden-unicode-1`.

**Classes sinalizadas, todas fail-closed:** `tag`, `bidi`, `zero_width`, `variation_selector`, `control`, `line_separator`, `invisible_filler` (incluindo U+2800), `noncharacter`, `surrogate`, `private_use`, `other_format` (categoria Unicode Cf) e `unassigned` (Cn). **Não há exceção para emoji**: bandeiras de emoji usam caracteres TAG, e ZWJ/VS16 também são sinalizados — a varredura não distingue uso decorativo de uso adversário do mesmo ponto de código.

Um BOM (*byte order mark*) na posição inicial do arquivo é permitido e registrado como `leading_bom`, em vez de contar como achado.

O **modo JSON** varre chaves e strings decodificadas do documento; chaves duplicadas, `NaN` e profundidade acima de 256 são rejeitados antes mesmo de a varredura de Unicode começar. O **modo texto** também detecta escapes `\uXXXX` e `\u{...}`, marcados como "escapado" no achado — mas essa detecção **ignora contexto de citação**, o que é uma fonte conhecida de falso positivo (ver §24).

Os apontadores (*pointers*) de cada achado são sanitizados por segurança: qualquer caractere sinalizado dentro do apontador vira U+FFFD, e o apontador inteiro é limitado a 1.024 caracteres — o que significa que um apontador truncado **pode não resolver** sob RFC 6901. Os achados detalhados são limitados a 100; os totais por classe permanecem exatos mesmo quando os achados individuais são cortados. `unicode_version` é registrado no resultado, para que uma futura atualização do Python com tabela Unicode diferente seja rastreável.

O limite de entrada é 1 MiB, usando o mesmo leitor defensivo compartilhado com `load` e `verify_artifact`. **Membros de arquivo compactado (zip/tar) não são varridos** — decisão deliberada: um cliente MCP real só vê o retorno de `tools/list` em tempo de execução, nunca o arquivo compactado de distribuição, e decodificar arquivos compactados em memória traz risco de *bomb* (expansão descontrolada) e risco de diferença de *parser* (o que o `fpch scan` decodifica pode não ser bit a bit o que o cliente MCP real decodifica).

## 21. Verificação executada após a varredura de Unicode oculto

*Smoke test* manual em diretório temporário: um arquivo `.txt` de metadados contendo um caractere TAG (U+E0041) escapado como `\u{e0041}` produziu `fpch mcp scan` com saída 1 e achado de classe `tag (escapado)`. Um arquivo de metadados limpo produziu saída 0. O comportamento está descrito em conjunto com o portão de `setup apply` no quadro do §23, porque as duas peças foram verificadas ponta a ponta no mesmo *smoke test*.

## 22. Apêndice de desenvolvimento: C4/C7 — portão de evidência MCP verificada em `setup apply`

O segundo candidato aprovado pelo autor conecta a verificação de C7 à aplicação de C4: `fpch setup apply plano.json --confirm <id> --mcp-artifact NOME=CAMINHO --mcp-metadata NOME=CAMINHO`. Ambas as opções são repetíveis e, juntas, precisam cobrir **cada checkpoint do plano exatamente uma vez**; um nome desconhecido ou duplicado devolve saída 2 antes de qualquer tentativa de mutação.

Na API, `apply(..., mcp_evidence={name: FpchMcpApplyEvidence(artifact, metadata)})` recebe a evidência por checkpoint, e o resultado (`FpchSetupResult.mcp_verified`) lista os nomes efetivamente verificados. Para cada checkpoint do plano, a aplicação exige duas condições: `verify_artifact` retornando verificado, **e** uma varredura limpa dos metadados — a varredura de metadados é **obrigatória**, **sempre em modo JSON independentemente da extensão do arquivo**, e o documento não pode estar vazio (objeto ou array de nível superior não vazio).

Essa checagem dupla é executada **antes** de adquirir o *lock* de aplicação e **reconferida dentro do lock, antes da auditoria** — inclusive no ramo em que a aplicação não teria nada a fazer (*no-op*). Uma diferença entre a checagem externa e a interna aciona rollback: o teorema é que nada é auditado como instalação com uma evidência que pode ter mudado entre a leitura e o commit.

Os caminhos de artefato e de metadados são recusados quando apontam para dentro de `.fpch` — tanto por comparação léxica do caminho quanto por `realpath` (com tratamento dos prefixos de caminho do Windows), fechando a via óbvia de um checkpoint apontar evidência para dentro da própria área de controle do FPCH.

Quando a verificação é bem-sucedida, a aplicação grava `.fpch/mcp-verification.json` — esquema 1, JSON canônico, `installed: false`, sem caminhos absolutos — por criação exclusiva (nunca sobrescreve), e o manifesto de setup passa a ser **versão 2**, com um bloco `"mcp": {checkpoints, evidence_path, installed: false}`. O rótulo de auditoria correspondente é `"fpch setup apply (MCP verificado, não instalado)"`; **verificações que falham não são auditadas** — só o sucesso produz evento.

Planos sem nenhum checkpoint continuam **byte-idênticos** ao comportamento anterior: manifesto versão 1, mesmo `plan_id`, mesmas mensagens — o portão só se ativa quando há checkpoint MCP no plano.

**Assimetria registrada como ponto [AUTOR]:** `fpch setup plan` com checkpoints continua retornando saída 1 (`is_applicable` falso) mesmo depois deste corte — o plano em si nunca ficou "aplicável" na presença de checkpoint, e este corte não mudou isso, só deu a `apply` uma via de evidência para prosseguir apesar disso. Se essa saída de `plan` deveria mudar para refletir que agora existe um caminho de aplicação é decisão do autor, não deste apêndice.

Continua valendo o limite de fundo: **nada é instalado ou executado.** Não há *download*, rede, resolução de pacote, escrita de configuração de cliente (`.mcp.json`), *allowlist*, catálogo C15 nem executor de desfazimento C25.

### Escolhas de engenharia tomadas como *default*, para revisão do autor

- Metadados obrigatórios (não há caminho de aplicação sem varredura de metadados).
- BOM inicial permitido (não conta como achado de Unicode oculto).
- Limite de metadados em 1 MiB (mesmo teto do checkpoint).
- Verificações que falham não geram evento de auditoria.
- Caminhos de artefato e de metadados ficam fora de `plan_id` (não são parte do conteúdo congelado do plano).
- `fpch setup plan` com checkpoint continua retornando saída 1, mesmo com o portão de evidência agora existindo.

## 23. Verificação executada após o portão de `setup apply`

O plano foi desenhado por um agente Plan em modelo *opus* e a implementação foi dividida em três pacotes de trabalho paralelos: WP-A (varredura), WP-B (portão de `apply`) e WP-C (CLI). Uma revisão independente somente leitura encontrou **1 problema bloqueador e 8 riscos**, todos corrigidos. O bloqueador: o comando `setup` usava `format=auto` na varredura de metadados, de modo que um arquivo `.txt` contendo um TAG escapado passava como limpo apenas por causa da extensão — corrigido ao fixar o modo JSON independentemente do sufixo do arquivo (ver §22). Testes de mutação confirmaram que os testes novos detectam os defeitos corrigidos.

*Smoke test* manual ponta a ponta em um repositório temporário:

| Cenário | Resultado |
|---|---|
| Metadados `.txt` com TAG U+E0041 escapado | `mcp scan` → saída 1, "tag (escapado)"; `apply` recusado, repositório intocado |
| Metadados limpos | `apply` → saída 0, gravando `AGENTS.md`, `CLAUDE.md`, `.fpch/setup-manifest.json` e `.fpch/mcp-verification.json` com `installed=false`, `verified=true`, `format=json` |

| Verificação | Resultado em 13/09/2026 |
|---|---|
| Suíte completa | **765 passed, 12 skipped em 13,07 s** (era 518 passed, 10 skipped) |
| Código do protótipo | **16 módulos, 10.759 linhas físicas em `src/fpch/`** |
| Testes | **19 arquivos, 9.578 linhas físicas** |
| Grafo graphify | **não recalculado nesta sessão** — seguem valendo os 5.478 nós/6.790 arestas/497 comunidades do corte de C16 |
| Efeitos de rede/instalação | nenhum — sem *download*, resolução, execução, extração ou instalação |
| Aplicação | segue exigindo evidência verificada por checkpoint; nada é instalado |
| Entregáveis acadêmicos | congelados e sem alteração nesta fatia |

## 24. Limites residuais do corte, e o que continua em aberto

- **(a)** A evidência de C16/C7 registra `unicode_version` e `ruleset`; uma atualização futura do Python (tabela Unicode nova) faz um `setup apply` repetido falhar como "divergente" mesmo sem nenhuma mudança real de conteúdo.
- **(b)** Quando um arquivo de evidência já existente é adotado como inalterado, qualquer `plan_id` bem formado é aceito — a proveniência desse campo, isto é, se ele realmente veio de um `setup plan` anterior e não foi editado à mão, não é comprovada.
- **(c)** Os metadados não têm vínculo criptográfico com o artefato: qualquer JSON limpo e não vazio conta como evidência válida, mesmo que não descreva de fato aquele artefato.
- A detecção de escapes em modo texto ignora contexto de citação, o que pode gerar falso positivo (por exemplo, um `\uXXXX` dentro de um comentário sobre o próprio caractere, e não o caractere em si).
- Seguem valendo os limites já registrados em C16/C7 2º corte (§18): reescrita *in-place* que preserva `mtime`; *hardlinks*; ausência de `O_NOFOLLOW` no Windows; testes reais de *symlink* seguem pulados nesta máquina (`WinError 1314`).

**Candidatos ao próximo passo, não decisão:** revisão do autor sobre os seis pontos [AUTOR] listados no §22; vincular metadados ao artefato por *digest*, por exemplo dentro do próprio checkpoint, o que muda o esquema do checkpoint; instalação de fato continua bloqueada até decisão sobre *allowlist*/C15/C25. C4 e C7 continuam parciais; C15, C23 e C25 continuam abertos; **C17 foi concluída no corte seguinte, registrado a partir do §25.**

## 25. Apêndice de desenvolvimento: C17 — gate de aprovação na fronteira do módulo de canibalização

Ainda em 13/09/2026, quarto corte do dia, C17 foi concluída e, numa segunda rodada no mesmo corte, teve um *overclaim* de segurança no próprio docstring corrigido a partir de revisão independente — commitada como `eace234`. **Achado de abertura, antes de qualquer código novo:** o escopo original do item — checar `target.status` como primeira instrução de `cannibalize.run()`, para que quem chame a função diretamente não contorne a invariante de segurança que a CLI já impunha em `_cmd_canib` — já estava implementado desde o commit `f861281`, com 8 testes cobrindo o caso. O `TODO.md` nunca fora marcado `[x]`. A sessão conferiu o estado real do código antes de tratar o item como aberto, em vez de assumir a lacuna a partir apenas da redação do backlog.

O que restava, e este corte fechou, eram **contornos internos que a checagem de `target.status` sozinha não cobria**: o portão conferia o objeto `Target` recebido em memória, não o registro persistido na fila. `Target(status="accepted")` construído à mão, ou `dataclasses.replace(t, status="accepted")` sobre uma cópia, fabricavam uma aprovação que nenhum humano deu, e o portão original acreditava nela — o mesmo anti-padrão, um nível abaixo, que a fatia funcional de 01/09/2026 já havia corrigido na fronteira entre CLI e módulo.

## 26. O portão contra o registro persistido

`run()` agora executa, antes de montar qualquer prompt ou tocar o roteador, uma nova função `_verify_approval(targets, target)` que:

1. relê a fila do disco (`_load()`) e localiza o registro cujo `id` combina com `target.id`; ausência ou duplicidade de id é tratada como não aprovado (registro ambíguo não autoriza nada);
2. exige que o **status persistido** — não o do objeto recebido — seja `accepted`;
3. compara `target` contra o registro persistido nos campos que definem o que será lido e com que instrução — `url`, `kind`, `note`, `focus`, `local_path` — mais o próprio `status`; qualquer divergência recusa a execução;
4. devolve o **registro persistido**, que passa a ser o objeto usado no resto de `run()` — o objeto recebido do chamador deixa de ser lido para qualquer efeito.

A nova exceção `TargetApprovalMismatchError(TargetNotApprovedError)` cobre os casos de registro ausente e de divergência de campo; a mensagem nomeia os **nomes dos campos** divergentes e nunca os valores — `url` e `note` são conteúdo escolhido por quem montou o objeto, sem motivo para ecoar em log ou terminal —, e indica o comando de reaprovação (`fpch canib accept <id>`). Por ser subclasse de `TargetNotApprovedError`, qualquer chamador que já tratasse a exceção antiga continua funcionando sem alteração.

A aprovação passou a cobrir também a troca **posterior** da fonte: `set_local_path(id, novo_caminho)` e `add(..., local_path=...)` sobre um alvo existente agora passam pela função interna `_change_local_path`, que devolve o alvo a `pending` quando `local_path` muda e o alvo estava `accepted` — o mesmo valor não revoga nada, porque não há fonte nova a aprovar. O chamador distingue a revogação lendo o `status` do alvo devolvido; a função `reapproval_hint(id)` produz a linha acionável para mostrar ao humano.

## 27. TOCTOU numa execução longa: reconferência antes de gravar a ficha

Uma execução de `run()` chama o roteador duas vezes (extração e proposta) e pode levar minutos. Nesse intervalo, um humano pode ter rejeitado o alvo pela CLI, ou outra automação pode ter alterado a fonte. Antes desta correção, o resultado dessas chamadas era gravado como ficha e o alvo marcado `done` sem qualquer nova checagem.

Agora, depois das duas chamadas ao roteador e **antes** de escrever a ficha em disco, `run()` relê a fila (`_load()`) e chama `_verify_approval` de novo contra o registro atual. Se a aprovação mudou — rejeição, edição de campo ou remoção do registro — a função levanta `TargetApprovalMismatchError` com o motivo "a aprovação mudou durante a execução", **nenhuma ficha é escrita e o alvo não é marcado como `done`**. A mesma leitura da fila sustenta tanto a reconferência quanto a gravação final: `content = _render(...)` é calculado antes da checagem, mas só é escrito em disco depois dela passar, e a marcação `current.status = STATUS_DONE` / `current.fiche_path = str(path)` seguida de `_save(targets)` usa o registro já revalidado, num único `_save`.

A escolha é deliberada e está documentada no docstring de `run()`: escrever a ficha e depois apagá-la em caso de rejeição tardia deixaria um artefato órfão se o processo morresse entre as duas operações; o que já foi enviado aos backends não tem como ser "desenviado" — a revogação tardia impede o **registro** do resultado, não o custo nem a exposição do prompt que já ocorreram.

## 28. Falha fechada sobre fila malformada

`_load()` deixou de assumir `Target(**t)` sobre qualquer JSON. A nova validação, antes de construir qualquer `Target`, recusa: JSON ilegível (erro de decodificação ou leitura), topo que não é uma lista, item que não é um objeto, item com chave desconhecida (fora do conjunto de campos de `Target`), item sem alguma das chaves obrigatórias (`url`, `kind`, `id`) e campo com tipo diferente do esperado (string, ou `None` nos dois campos opcionais). Cada falha levanta `QueueFormatError`, uma nova exceção própria do módulo, com mensagem que localiza o item pelo índice na lista.

A fila real de canibalização em uso neste repositório (`~/.fpch/cannibalize.json`, **13 entradas**) foi conferida contra a validação nova em modo só leitura — carrega sem erro — e seu `mtime` (17/07/2026 20:03:49) foi preservado, sem escrita incidental durante a verificação. `fpch canib list` foi exercitado contra essa mesma fila real.

Na CLI, `_cmd_canib` (em `cli.py`) foi dividido: a função original virou `_cmd_canib_dispatch`, e um novo `_cmd_canib` externo envolve a chamada, captura `QueueFormatError` e imprime `erro: <mensagem>` em `stderr`, retornando código de saída 1 em vez de deixar o traceback subir. Essa captura externa cobre subcomandos como `list` e `accept`, que chamam `_load()` diretamente e não tinham nenhum tratamento próprio de erro de formato. `run` e `runall` já continham `except RuntimeError as exc` local para tratar `TargetNotApprovedError` — como `QueueFormatError` também é `RuntimeError` (embora não relacionada por herança a `TargetNotApprovedError`), esses dois subcomandos já capturavam o erro novo sem precisar de mudança.

O docstring do módulo — a seção que começa com "ONDE MORA O PORTÃO" — foi atualizado nesta primeira passada para descrever a conferência contra o registro persistido, e não mais apenas contra `target.status` do objeto recebido. **A formulação do limite dessa primeira passada ainda superafirmava o alcance do portão — corrigido na segunda rodada, §29.**

## 29. Segunda rodada: o docstring superafirmava o alcance do portão

A revisão independente pedida sobre o corte de C17 encontrou **0 problemas bloqueadores, 5 riscos e 1 *nit*, todos endereçados**. O achado mais importante não foi um bug de comportamento: foi uma frase do próprio docstring do módulo, escrita na primeira passada, dizendo que o portão "protege contra o contorno programático dentro do processo". A frase é falsa. Código no mesmo processo pode aprovar um alvo sem passar por `fpch canib accept` de nenhuma forma reconhecida como aprovação humana — por exemplo chamando `set_status(id, "accepted")` diretamente, chamando `_save(targets)` sobre uma lista editada à mão, ou reatribuindo `QUEUE_PATH` para apontar a um arquivo forjado. O portão de `_verify_approval` não tem como distinguir esse caminho de um `fpch canib accept` genuíno: para ele, um registro com `status == "accepted"` na fila **é** uma aprovação, não importa como chegou lá.

**Isto é quase-repetição do modo de falha documentado em [`docs/decisoes/protecao-fantasma-no-laco-2026-09-03.md`](../decisoes/protecao-fantasma-no-laco-2026-09-03.md):** uma garantia de segurança declarada em prosa além do que o código de fato impõe, descoberta não pelo comportamento observado (o portão sempre recusou o que deveria recusar nos testes) mas pela leitura crítica da alegação em si. A diferença para o episódio de 03/09/2026 é que ali a proteção fantasma vinha de um campo de política que não governava o hook — aqui a "proteção fantasma" é textual: o código nunca prometeu mais do que faz, mas o comentário prometia.

O docstring foi reescrito com uma seção «O LIMITE» que substitui a alegação errada pela precisa:

- O portão recusa, em relação à fila persistida, objetos forjados, obsoletos ou divergentes, e fontes trocadas depois da aprovação.
- Ele **não** distingue um `fpch canib accept` humano de código que chama a API da fila (`set_status`, `_save`), reatribui `QUEUE_PATH` ou grava `~/.fpch/cannibalize.json` diretamente — no mesmo processo ou fora dele. Para o portão, tudo isso é aprovação.
- Aprovação fora de banda de verdade exigiria algo fora do processo (uma confirmação que o próprio agente não consegue produzir); isso **não está implementado**.
- A aprovação fixa a **string** de `local_path`, não o conteúdo do diretório: arquivos trocados dentro do mesmo caminho depois da aprovação passam sem disparar revogação.

As mensagens das três exceções do módulo (`TargetNotApprovedError`, `TargetApprovalMismatchError` e a nova `ApprovedSourceMissingError`, §30) foram alinhadas a essa formulação: onde diziam algo que sugeria uma garantia mais forte, passaram a dizer "a canibalização exige aprovação explícita registrada na fila" — nem mais, nem menos do que o código garante.

## 30. Segunda rodada: fonte local ausente falha fechado, e ids forjados são escapados

Dois riscos adicionais da revisão, também corrigidos:

**Fonte local aprovada que desaparece.** Antes deste corte, se o registro aprovado tinha `local_path` mas o diretório não existia mais na hora de `run()`, o código não tratava esse caso explicitamente e podia acabar caindo no ramo de busca por URL — trocando silenciosamente a fonte que fora aprovada por outra. Nova exceção `ApprovedSourceMissingError(RuntimeError)` e função `_require_local_source(target)`: se `target.local_path` está definido e `Path(target.local_path).is_dir()` é falso, `run()` recusa **antes de qualquer chamada ao roteador**. A mesma checagem é repetida imediatamente antes da validação de citações (`citations.validate`), porque a execução entre as duas chamadas ao roteador pode levar minutos e a fonte pode ter sumido nesse intervalo — sem essa segunda checagem, uma ficha sem validação de citações ainda poderia ser gravada.

**Ids forjados em mensagens de erro.** Um `Target` construído à mão pelo chamador, ou um item de fila malformado, pode trazer um `id` (ou `status`) que não segue o formato interno (`[0-9a-f]{8}}`, oito caracteres hexadecimais). Antes deste corte, esse valor ia cru para a mensagem de exceção, que por sua vez pode acabar em terminal ou arquivo de log — vetor de injeção de sequência ANSI ou de linha de log forjada. Nova função `_safe_text`: qualquer caractere fora do intervalo ASCII imprimível (controle, ESC, quebra de linha, não-ASCII) e a própria barra invertida viram escape `\uXXXX`; o resultado é cortado em **40 caracteres**. `_shown_id` decide, por `id`, se ele está no formato esperado (aparece como está, e o comando sugerido usa o id real) ou fora dele (aparece escapado entre aspas, e o comando sugerido usa um marcador genérico `<id>` em vez de ecoar o valor forjado). As três classes de exceção do módulo (`TargetNotApprovedError`, `TargetApprovalMismatchError`, e a mensagem que `ApprovedSourceMissingError` produz a partir do mesmo `id`) passam por esse tratamento.

Na suíte, `test_run_usa_o_registro_persistido` foi reescrito para mutar o objeto `Target` **depois** de passá-lo a `run()` (simulando um chamador que mantém uma referência e a edita) e afirmar que o comportamento observado usa os valores do registro persistido, não os da mutação; o teste de id duplicado na fila passou a rodar nas duas ordens possíveis (`aprovado-primeiro`, `pendente-primeiro`), porque a ordem de iteração não deveria mudar o veredito; e três testes de regressão novos na CLI cobrem `add`, `reject` e `list` contra a validação de formato de fila. Cinco checagens de mutação adicionais desta rodada foram cada uma capturada por um teste novo ou reforçado.

## 31. Verificação executada após C17 (quarto corte, segunda rodada)

| Verificação | Resultado em 13/09/2026 (quarto corte, segunda rodada) |
|---|---|
| Suíte completa | **808 passed, 12 skipped em 12,97 s** (era 794 passed, 12 skipped) |
| Código do protótipo | **16 módulos, 11.089 linhas físicas em `src/fpch/`** |
| Testes | **20 arquivos, 10.144 linhas físicas em `tests/`** |
| `tests/test_cannibalize.py` | de 8 para **37 testes coletados** (24 funções, 4 delas com `parametrize`) |
| `tests/test_cannibalize_cli.py` (novo) | **14 testes coletados** (11 funções, 3 delas com `parametrize`) |
| Fila real (`~/.fpch/cannibalize.json`) | **13 entradas**, carregada só leitura, `mtime` de 17/07/2026 preservado |
| `fpch canib list` contra a fila real | executado, sem escrita |
| Testes de mutação | checagem original (remover a checagem de entrada, a segunda checagem TOCTOU ou o *reset* de `local_path`) mais **5 checagens adicionais da segunda rodada** — cada uma capturada por um teste |
| Revisão independente | **concluída: 0 bloqueadores, 5 riscos e 1 *nit*, todos endereçados** |
| Commit | `eace234` — `fix(canib): verify approval against the persisted queue (C17)` |
| Grafo graphify | **não recalculado nesta sessão** — seguem valendo 5.478/6.790/497 do corte de C16 |
| Entregáveis acadêmicos | congelados e sem alteração nesta fatia |

## 32. Limites residuais do corte, e o que continua em aberto

- Quem tem escrita direta em `~/.fpch/cannibalize.json`, ou código no mesmo processo que chama `set_status`/`_save`/reatribui `QUEUE_PATH`, ainda aprova qualquer alvo — o portão recusa objeto forjado, obsoleto ou divergente **em relação à fila persistida**, mas não distingue quem ou o quê escreveu o `"accepted"` nela. Fechar isso exigiria aprovação assinada fora do alcance do agente, que não existe neste corte (ver §29).
- A aprovação fixa a **string** de `local_path`, não o conteúdo do diretório: arquivos trocados dentro do mesmo caminho aprovado, depois da aprovação, não disparam revogação (ver §30).
- Não há trava de arquivo (*file lock*) sobre a fila: entre a segunda verificação (antes de escrever a ficha) e a gravação de `done`, resta uma janela curta — a mesma propriedade de todo leitor-modificador-gravador do módulo, já registrada em cortes anteriores para outras partes do FPCH.
- Uma rejeição no meio da execução não desfaz chamadas de backend já feitas: o custo de cota e a exposição do prompt já ocorreram; o que a reconferência impede é o **registro** do resultado como concluído.
- Uma fila gravada por uma versão futura do FPCH, com chaves adicionais em `Target`, falha fechada como `QueueFormatError` em vez de ignorar os campos desconhecidos — decisão deliberada de não adivinhar formato, com o custo de exigir migração explícita se o esquema de `Target` crescer.

**Candidatos ao próximo passo, não decisão:** **C18** (substituir `_FAILURE_SIGNATURES` — lista literal de mensagens de erro de terceiro em `backends.py:81-90` — por validação mais robusta) e **C19** (capturar mais que `BackendUnavailable` no laço de escalada de `router.py:85-99`) são dívidas de engenharia identificadas no mesmo levantamento, sem decisão pendente do autor — ambas descritas no `TODO.md`. C4 e C7 continuam parciais; C15, C23 e C25 continuam abertos.

## 33. Apêndice de desenvolvimento: C18 — contrato de saída por sentinela, no lugar da `_FAILURE_SIGNATURES` como defesa principal

**Problema de partida.** `backends.invoke` tratava `exit 0` como sucesso a menos que a saída contivesse uma de 8 frases literais de erro de fornecedor (`_FAILURE_SIGNATURES`, `backends.py:81-90`) ou tivesse menos de 40 caracteres. Se um fornecedor mudasse o texto do erro, a falha semântica passava em silêncio — é a mesma classe de risco do incidente de 16/07/2026 (mensagem de erro do `agy` analisada como artefato de conteúdo, 6 fichas alucinadas) e do C11 (catálogo `agy` extraído empiricamente, que quebraria calado se a Google renomeasse um modelo).

**Desenho do contrato: positivo, e independente do texto do fornecedor.** `FpchContratoSaida.novo()` gera um nonce por chamada com `secrets.token_hex(8)`. A instrução acrescentada ao prompt pede que a resposta termine com a linha `FPCH-FIM-<nonce>`, mas o texto da instrução **descreve a sentinela em partes** ("a palavra FPCH-FIM, um hífen, e este código: ...") — nunca contém a cadeia completa `FPCH-FIM-<nonce>` de uma vez, para que um eco literal do prompt na resposta do modelo não bata como sentinela cumprida por acaso.

**Regra de validação: a última linha não vazia, e só ela.** A sentinela precisa ser a última linha não vazia da resposta, tolerando CRLF, espaço em branco e decoração markdown (`_DECORACAO`) em volta. A alternativa mais simples — aceitar a sentinela em qualquer ocorrência, não só na última linha — foi **rejeitada deliberadamente**: aceitaria também texto colado depois da sentinela, que é exatamente a forma de uma mensagem de erro de fornecedor anexada ao fim de uma resposta por lo contrário boa. Só o token desta chamada específica é removido de `Result.text`, como token inteiro delimitado por fronteira de palavra (`(?<![0-9A-Za-z])...(?![0-9A-Za-z])`) — sentinelas de *outras* chamadas, ou literais que já existem neste próprio repositório (por exemplo, exemplos em testes), não são tocados, então uma ficha de canibalização escrita sobre este repo não fica corrompida por remoção indevida.

**Ordem de decisão de falha: `exit≠0` → contrato → *denylist*.** Uma falha de contrato produz o prefixo estável `FPCH_ERRO_SENTINELA_AUSENTE = "contrato de saída não cumprido: sentinela ausente"`, seguido da assinatura da *denylist* quando uma delas bate no texto (mesmo sem sentinela) e de um trecho saneado de até 200 caracteres da resposta. A `_FAILURE_SIGNATURES` deixou de ser a linha principal de defesa e passou a diagnóstico e defesa em profundidade — ela ainda roda mesmo quando o contrato passa, contra o caso em que o modelo cumpriu a forma da sentinela mas a resposta é, ainda assim, uma mensagem de erro. A checagem por tamanho ("< 40 caracteres") permanece como heurística declarada, não como regra formal.

**Registro e compatibilidade da trilha.** `Result.contract` grava `"sentinela"` ou `"nenhum"` (`FPCH_CONTRATO_SENTINELA`/`FPCH_CONTRATO_NENHUM`), e `Result.failure_signature` grava a assinatura da *denylist* quando aplicável. Os mesmos campos passaram a existir, como opcionais, em eventos `attempt` — inclusive tentativas contra um backend indisponível, onde nenhum texto chegou a ser produzido. Não houve *bump* de esquema de auditoria: campos vazios não são escritos, então eventos anteriores a C18 continuam lendo normalmente. Um evento pré-C18 foi congelado com hash literal num teste de regressão para fixar a compatibilidade retroativa da cadeia; `fpch audit` (saída 0) foi conferido lendo a trilha real depois da mudança.

**Opt-out declarado, não usado.** `invoke`/`route` ganharam o parâmetro `output_contract: bool = True`; passar `False` desliga o contrato deliberadamente e fica registrado como `Result.contract == "nenhum"`. Nenhum chamador do próprio FPCH usa o opt-out — ele existe para quem precisar de uma chamada crua, não como caminho recomendado.

**Efeito no *staging* de prompts.** `_ARG_LIMIT` (24.000 caracteres) e a lógica de *staging* por arquivo temporário passaram a contar o tamanho da instrução do contrato via `FPCH_CONTRATO_INSTRUCAO_CHARS` (medido a partir de uma instância de referência com nonce fixo de 16 zeros). `prompt_chars`, o campo gravado na trilha, continua sendo só o tamanho do prompt do chamador — a instrução do contrato não entra nele — para que a série histórica de tamanhos de prompt permaneça comparável antes e depois de C18.

**Copilot: `-s/--silent` obrigatório.** O adaptador do Copilot passou a sempre incluir `-s`/`--silent` na chamada, confirmado presente no `--help` do CLI. Sem essa flag, o Copilot imprime estatísticas depois da resposta, o que faria a sentinela deixar de ser a última linha não vazia e reprovaria toda chamada pela regra da última linha — um falso-negativo sistemático que o contrato existe justamente para evitar em outra direção.

**Envelope estruturado (JSON): investigado e declarado ausente.** Os três CLIs — `claude` 2.1.270, `copilot` 1.0.83 e `agy` 1.2.2 — aceitam `--output-format json` no próprio `--help`, mas **nenhum documenta o esquema** da saída JSON. Cada adaptador declara `Regime.saida_estruturada = AUSENTE`, com a evidência (a linha do `--help`) anexada ao código. Não há *parser* de envelope neste corte: sem esquema verificado, um *parser* seria só mais uma lista de padrões frágil, a mesma classe de risco que C18 está corrigindo em outro lugar.

**Controle de custo.** Nenhum modelo foi invocado durante o desenvolvimento de C18 — só `--help`/`--version` dos três CLIs, para confirmar as flags e a ausência de esquema JSON documentado. Isso significa que a taxa de cumprimento real da instrução da sentinela por modelo **não foi medida** neste corte (ver §35).

## 34. Verificação executada após C18

| Verificação | Resultado em 14/09/2026 |
|---|---|
| Suíte completa | **865 passed, 12 skipped em 14,01 s** (era 808 passed, 12 skipped) |
| Código do protótipo | **16 módulos, 11.378 linhas físicas em `src/fpch/`** |
| Testes | **20 arquivos, 10.797 linhas físicas em `tests/`** (reconferido por glob + contagem de linhas, não só por relato) |
| Revisão independente (primeira rodada) | 0 bloqueadores, 5 riscos e 3 *nits* |
| Revisão independente (segunda rodada) | todos os 5 riscos e os 3 *nits* corrigidos |
| Testes de mutação | as checagens removidas foram capturadas pelos testes novos |
| Evidência de CLI | `--help` de `claude` 2.1.270, `copilot` 1.0.83 e `agy` 1.2.2 conferido — `--output-format json` presente, esquema ausente nos três |
| Chamadas reais a modelo | **zero** — só `--help`/`--version` |
| Graphify | **não recalculado nesta sessão** — seguem valendo 5.478/6.790/497 do corte de C16 |
| Commit | `6e0285a` — `feat(backends): require per-call output sentinel instead of error denylist (C18)` |
| Entregáveis acadêmicos | congelados e sem alteração nesta fatia |

## 35. O que a revisão independente corrigiu, e o mais importante dos cinco riscos

A primeira rodada da revisão achou **0 bloqueadores, 5 riscos e 3 *nits***, todos corrigidos numa segunda rodada. **O mais importante:** na falha de contrato, a mensagem de erro havia perdido o diagnóstico que `_FAILURE_SIGNATURES` sozinha ainda dava — cota esgotada, falha de autenticação e recusa de ferramenta passaram a parecer todos iguais ("sentinela ausente"), quando antes cada um tinha uma assinatura reconhecível na *denylist*. É reincidência da classe de risco do C11: um mecanismo de segurança novo que, ao resolver um problema, apaga informação que outra parte do sistema (e o operador humano) precisava para diagnosticar a causa. A correção manteve a assinatura da *denylist*, quando existente, dentro da mensagem de falha de contrato, em vez de substituí-la.

Os demais quatro achados, todos corrigidos:

- **Regex da sentinela sem fronteiras** apagava conteúdo de ficha em vez de só o token da sentinela — corrigido com fronteira de palavra (`(?<![0-9A-Za-z])...(?![0-9A-Za-z])`) na remoção.
- **Tentativa de backend indisponível sem `contract`** — um evento `attempt` para um backend que nunca respondeu ficava com a aparência de um evento pré-C18, indistinguível na trilha. Corrigido para gravar `contract="nenhum"` mesmo nesse caminho.
- **Teste de retrocompatibilidade fraco** — não fixava o hash de um evento pré-C18 real; fortalecido com um evento congelado e hash literal.
- **Fronteira de `_ARG_LIMIT` sem teste** — o efeito de `FPCH_CONTRATO_INSTRUCAO_CHARS` sobre o *staging* não tinha teste na fronteira exata; adicionado.
- **Teste do roteador tautológico** — um teste que só reafirmava o próprio código em vez de exercitar o comportamento observável foi reescrito.

## 36. Limites residuais do corte, e o que continua em aberto

- **A sentinela prova que a chamada terminou, não que o conteúdo está correto.** É um contrato de forma, não de semântica — um modelo pode cumprir a sentinela e ainda assim responder algo inútil ou errado; isso não é o que C18 verifica.
- **Modelos que ignoram a instrução produzem falso-falha**, e cada falso-falha custa uma escalada real de cota para o próximo backend na cadeia. **A taxa de cumprimento real por modelo não foi medida** neste corte, porque medi-la exige chamadas reais, que gastam cota — é justamente o motivo do *smoke test* real ser a decisão [AUTOR] mais importante em aberto (§37).
- `agy -p` pode imprimir texto depois da resposta do modelo; não verificado neste corte se isso quebra a regra da última linha em algum caso real.
- **Um modelo pode copiar o nonce** de volta se ele aparecer em qualquer parte do contexto que o modelo processa — isto não protege contra um modelo adversarial que tenta ativamente forjar conformidade; o contrato assume um modelo que tenta cooperar e às vezes falha, não um que tenta enganar o verificador.
- `failure_signature` só é preenchido quando `exit 0` — falhas de processo (`exit≠0`) não passam pela *denylist*, porque a causa já é conhecida pelo código de saída.

## 37. Decisões em aberto do autor, e a dívida achada (C26)

**(1) *Smoke test* real — a mais importante.** Uma chamada real por backend (`agy`/`copilot`, fora da cota Claude) para medir o cumprimento da sentinela antes de confiar nela no dia a dia. É a mais importante das quatro porque **C18 muda o comportamento de toda chamada real** a partir de agora — se um modelo específico ignora a instrução com frequência, o contrato produz falso-falha sistemático contra esse modelo, e isso só aparece com dado real.

**(2) Flag de CLI `ask --sem-contrato`**, espelhando o opt-out já existente em `invoke`/`route` na camada de programação, para uso manual pontual.

**(3) Se `improve.REGRA_ASSINATURA` deve ler `failure_signature`** — o laço de autoaprimoramento ainda não foi ligado ao novo campo; decidir se vale a pena antes de qualquer proposta automática usar a assinatura da *denylist* como sinal.

**(4) Um futuro *parser* de envelope JSON**, que precisa de capturas de saída real dos três CLIs em modo `--output-format json` para inferir o esquema não documentado — trabalho que também gasta cota e não foi feito neste corte.

**Dívida achada, registrada como C26 no `TODO.md`, não decisão:** `policy.py` `ImmutableBlock.referencia` aponta `backends.py:128-129` e `cannibalize.py:208-247`, ambos defasados — o primeiro porque C18 reescreveu boa parte de `backends.py` ao redor dessas linhas, o segundo desde C17. São blocos de **política imutável**; corrigir a referência é edição mecânica, mas sobre um bloco marcado imutável, o que pede confirmação explícita antes de tocar.

C4 e C7 continuam parciais; C15, C23 e C25 continuam abertos.

## 38. Apêndice de desenvolvimento: *smoke test* real de C18 em `agy` (14/09/2026)

**Autorização e escopo.** Ainda em 14/09/2026, o autor autorizou o primeiro *smoke test* real de C18 — chamadas reais a modelo, fora do desenvolvimento em si, para medir o cumprimento da sentinela antes de confiar nela no dia a dia (o ponto [AUTOR] mais importante do §37). O pedido original era pelo menor modelo "flash lite" disponível; **não existe modelo "Lite" no `agy`**. A lista viva, obtida por `agy --model __invalido__`, oferece: Gemini 3.8/3.7/3.6 Flash (High/Medium/Low), Gemini 3.1 Pro (High/Low), Claude Sonnet 4.6 (Thinking), Claude Opus 4.6 (Thinking) e GPT-OSS 120B (Medium). O menor da lista é **Gemini 3.6 Flash (Low)**, e foi o modelo usado. As chamadas passaram pelo `backends.invoke` real (*sandbox*, sem escrita), com o id do modelo sobrescrito via `dataclasses.replace`. As saídas cruas ficaram só no *scratchpad* da sessão, não versionadas.

**Resultado positivo: 3/3 conformes, 0 falsos-falha.**

| caso | formato do prompt | ok | latência | observação |
|---|---|---|---|---|
| prosa | 3 frases | true | 6,16 s | sentinela na última linha |
| lista | marcadores markdown | true | 5,14 s | sentinela depois do último item |
| código | "somente um bloco de código" | true | 4,33 s | sentinela em linha própria **depois** do fechamento do bloco; o texto limpo termina na cerca de fechamento |

Nos três casos, `contract="sentinela"`, `failure_signature=null`, e não houve texto depois da sentinela. A dúvida registrada no §36 sobre `agy -p` imprimir texto depois da resposta **não foi observada nesta amostra**.

**Resultado negativo real: a falha de 16/07/2026 reproduzida e capturada.** Prompt: buscar `https://example.com/` e resumir. O `agy` imprimiu `jetski: no output produced — a tool required the "read_url" permission that headless mode cannot prompt for, so it was auto-denied…` e saiu com **exit 0**. Resultado: `ok=false`, `contract="sentinela"`, `failure_signature="no output produced"`, erro começando com `contrato de saída não cumprido: sentinela ausente; assinatura: 'no output produced' — jetski: …`. Antes de C18, só a *denylist* literal capturaria isso; agora o contrato falha fechado independentemente da redação, e a *denylist* preserva o diagnóstico.

**Limites da evidência.** n=3 positivo e n=1 negativo, num único modelo. Isto não é uma taxa de conformidade. Os demais modelos do `agy` (3.7/3.8 Flash, Pro, Claude via `agy`, GPT-OSS) e o `copilot` seguem sem medição. Vale a regra do projeto: resultado que confirma a hipótese merece mais ceticismo, não menos — este resultado favorável não deve ser lido como prova.

## 39. Achado novo e dívida: catálogo de modelos do FPCH desatualizado (registrado como C27)

O catálogo de modelos do FPCH (`src/fpch/models.py`, `CATALOG`) está desatualizado: lista `Gemini 3.5 Flash (Low|Medium|High)`, que o `agy` não aceita mais, e não tem 3.6/3.7/3.8 Flash. Rotear para essas entradas do catálogo falharia na invocação. Os valores de `power`/`cost` do catálogo são julgamento do autor, então a atualização não foi feita sem pedido. Registrado como **C27** no `TODO.md`.

C4 e C7 continuam parciais; C15, C23 e C25 continuam abertos.

## 40. C27 concluída: catálogo atualizado por sondagem real de `agy`, `copilot` e `codex`, e `codex` vira backend (14/09/2026)

**Autorização e escopo.** Ainda em 14/09/2026, o autor autorizou atualizar o catálogo (C27) e pediu que antes fossem levantadas "todas as opções ativas e funcionais" dos três CLIs de assinatura — `agy`, `copilot` e `codex`, este último ainda sem backend no FPCH. A lista declarada por cada CLI **não** foi tomada como verdade: cada modelo recebeu uma chamada real (*ping* com um token que o prompt descreve em partes, para que um eco não passe por resposta), fora do `backends.invoke`, com escrita bloqueada. Saídas cruas ficaram no *scratchpad* da sessão.

**Resultado da sondagem (47 chamadas).**

| CLI | lista declarada | funcional | observação |
|---|---|---|---|
| `agy` | 14 (`agy --model __invalido__`) | **14/14** | Gemini 3.8/3.7/3.6 Flash (High/Medium/Low), Gemini 3.1 Pro (High/Low), Claude Sonnet 4.6 e Opus 4.6 (Thinking), GPT-OSS 120B (Medium); Gemini 3.5 Flash saiu da lista |
| `copilot` 1.0.83 | 26 (`copilot help config`) | **0/26 via `--model`**; só `auto` | todo `--model <id>` falhou com `Error: Model "<id>" from --model flag is not available.` **e exit 0** — inclusive `mai-code-1.1-flash`, o modelo que o próprio `auto` escolhe; o evento `session.auto_mode_resolved` de `--output-format json` mostra `availableModels: ["mai-code-1.1-flash"]` |
| `codex` 0.154.0 | 5 visíveis + 2 ocultos (`~/.codex/models_cache.json`) | **7/7** | gpt-6-astra, gpt-5.6-sol/terra/luna, gpt-5.5; `gpt-reserve` e `codex-auto-review` funcionam mas têm `visibility: hide` e ficaram fora do catálogo |

Dois achados valem além do catálogo. Primeiro, **a lista de ajuda de um CLI não é a lista do que a conta pode usar**: no `copilot` a distância foi de 26 para 1. Segundo, **o `copilot` recusa o modelo com exit 0** — mais uma instância da classe de falha que motivou C18; sob o contrato por sentinela ela falha fechado. Ainda não se sabe se a restrição do `copilot` é permanente (plano) ou momentânea (cota esgotada); a sondagem deve ser repetida antes de catalogar modelos explícitos do `copilot`.

**O que mudou no código.** (1) `models.py`: `Pool.CODEX`; Gemini 3.8 Flash no lugar do 3.5, com 3.7 e 3.6 mantidos como *fallback* de mesmo `power`/`cost` e a ordem do catálogo desempatando pelo mais novo; `copilot` `default` rebaixado para `power=2, cost=2` e `mechanical`/`standard`, porque de fato resolve para um modelo *flash*; seis entradas `codex` no formato `"<slug> (<esforço>)"`, espelhando a nomenclatura do `agy`. Os valores de `power`/`cost` novos são **provisórios**, juízo do agente sob autorização do autor, não medida. (2) `policy.py`: `_DEFAULT_MODELS` espelha o catálogo e aceita o pool `codex`. (3) `backends.py`: `CodexAdapter` com regime declarado — `sandbox=True` → `-s read-only` ou, com escrita, `-s workspace-write`; escrita sem sandbox é **recusada**, porque o único modo sem sandbox do `codex` é `danger-full-access`, que o adaptador nunca emite; esforço sempre explícito (a config do autor fixa `xhigh`); prompt depois de `--`. (4) `_run` ganhou `_resolve_executavel`: no Windows, `subprocess` sem shell não acha shims `.cmd` do npm — `codex` dava `FileNotFoundError`, e o `claude` tinha o mesmo defeito latente; executar o `.cmd` passaria o prompt pelo `cmd.exe`, que reinterpreta `&`, `%` e aspas, então o shim é trocado por `node <script.js>` ou pelo `.exe` nativo, e `.cmd` não reconhecível é recusado. (5) `_run` passou a usar `stdin=DEVNULL`: o `--help` do `codex` diz que stdin em pipe é anexado ao prompt, e herdar o stdin do processo deixaria conteúdo alheio entrar na chamada. Este último furo foi achado na conferência do orquestrador, não pelo subagente, que o registrara só como lacuna.

**Verificação.** Suíte: **910 aprovados, 12 ignorados** (antes 865/12), incluindo testes de que nenhum id Gemini 3.5 sobrou, de que toda entrada `codex` vira flags válidas, de que todo backend do catálogo tem adaptador e de que `_run` fecha o stdin. Ponta a ponta real pelo `backends.invoke`, com `x-stdin-poison` em pipe no processo: `gpt-5.6-luna (low)`, `copilot` `default` e `Gemini 3.8 Flash (Low)` responderam com `ok=true`, `contract="sentinela"`, sem traço do stdin. Uma primeira rodada com resposta de três palavras deu `ok=false` nos três **com a sentinela cumprida**, reprovados só pela heurística declarada de "< 40 caracteres" — falso-falha real dessa heurística, registrado como limite, não corrigido.

**Mudança de roteamento que o autor deve revisar.** Em `hard`, `gpt-5.6-sol (high)` (`cost=3, power=5`) passa a vencer Gemini 3.1 Pro (High) e Claude Sonnet 4.6 (Thinking), que empatam em `cost=3` com `power=4`. Isso afeta o estágio de proposta da canibalização.

**Lacunas declaradas, não verificadas.** Aplicação efetiva do sandbox do `codex` no Windows; `read-only` do `codex` contém escrita, não leitura; plugins/MCP e *hooks* da config do usuário rodam dentro da chamada `codex` (os logs mostraram os *hooks* do autor disparando) e `--ignore-user-config` não foi testado; `copilot` com `--model` explícito segue sem funcionar nesta conta.

C27 concluída. C4 e C7 continuam parciais; C15, C23 e C25 continuam abertos; C26 segue aberta e agora mais defasada, porque `backends.py` cresceu de novo.

## 41. Conferência de 28/09/2026: reprodução da C8 e política fantasma no código

Em 28/09/2026, com o orientador tendo devolvido a `TCC_v6` com feedback (registro em [`../tcc/FEEDBACK-ORIENTADOR-TCC-V6-2026-09-28.md`](../tcc/FEEDBACK-ORIENTADOR-TCC-V6-2026-09-28.md)), o *working tree* (HEAD `0ce104b`, último commit de código `58c288a`) foi conferido contra o texto antes de reescrever os Resultados como `TCC_v7.md`. A conferência completa está em [`ficha-engenharia-fpch-2026-09-28.md`](ficha-engenharia-fpch-2026-09-28.md); este apêndice registra a reprodução da C8 e a dívida técnica nova achada, de acordo com o mesmo padrão de evidência dos apêndices anteriores. Nenhum código foi alterado nesta conferência.

### 41.1 Reprodução da C8

A `TCC_v6` afirma três vezes que a autocorreção causal não foi demonstrada e que a governança "campo ↔ componente" não existe (`TCC_v6.md:18`, `:329`, `:340`, `:362`, `:374`). Isso está superado desde 08/09/2026 (condição (d) em `improve.py:527-558`; demonstração em `STATUS.md:31`, `:207`, commit `3a135e3`). A sequência foi reexecutada de forma isolada no *scratchpad* desta sessão — não é o registro original de 08/09, é uma reprodução de conferência — com `[verificacao] timeout_s = 1` e um hook `lento` de 1,5 s (`quando="sempre"`, `criterio="exit_zero"`):

| Passo | Comando | Resultado observado | Evento na trilha |
|---|---|---|---|
| 1 | `fpch --policy pol.toml check --cwd ws` | `[FALHA] lento … 1.0226s`, exit 1 | `verify` fail, `source=hook`, `fault_side=infraestrutura`, `ambiente/bloqueio_de_ambiente`, `timed_out=true`, `timeout_s=1.0`, `timeout_origin=policy`, `governing_field=verificacao.timeout_s`, esquema 4 |
| 2 | `fpch --policy pol.toml improve` | proposta `prop-3281929bdeb7` (regra `ampliar_timeout`), `[verificacao].timeout_s: 1 → 2`, "NADA foi alterado", exit 0 | nenhum (só recusas são gravadas) |
| 3 | `fpch --policy pol.toml improve --apply prop-3281929bdeb7` | `verificacao.timeout_s: 1 → 2`, exit 0; arquivo atualizado | `verify` pass, `component=improve.apply`, `source=politica`, `source_ref=ampliar_timeout@…pol.toml`, mesma trajetória do passo 1 |
| 4 | `fpch --policy pol.toml check --cwd ws` | `[ok] lento … 1.5596s`, exit 0 | `verify` pass, `timeout_s=2.0`, `timeout_origin=policy`, trajetória nova |
| 5 | `fpch --policy pol.toml audit --verify` | "trilha íntegra — 3 linha(s)", exit 0 | — |

**Controle** (feito em outra área do *scratchpad*, com `[verificacao] timeout_s = 120`): `check --timeout 1` → FALHA (`timeout_origin=explicit`); `improve` → **`RECUSA campo_nao_governa_o_componente`**, sem gerar proposta, exit 1; `check` sem `--timeout` → ok. Esse é o mesmo controle que, em 03/09/2026, teria desmascarado a proteção fantasma original — a reprodução confirma que a condição (d) continua recusando corretamente quando o campo não governa o componente.

### 41.2 Achados de política fantasma no código, confirmados pelo orquestrador

**(a) Quatro campos da política TOML são validados e exibidos, mas nenhum mecanismo os lê.** `[escalada].ordenar_por` (o roteamento usa `(cost, -power)` fixo em `models.py:289`), `[backends].arg_limit` (usa a constante `_ARG_LIMIT` em `backends.py:48` e `:213`), `[falha].min_chars` e `[falha].assinaturas` (usam `_FAILURE_SIGNATURES` e o limiar `40` fixos em `backends.py:344-370`; `backends.py` nem importa `policy`, `:31-44`). É a mesma classe da proteção fantasma de 03/09/2026 — a política declara um campo que nenhum mecanismo consulta. Por isso `REGRA_ASSINATURA` do laço de aprimoramento tem `governanca=()` (`improve.py:166-178`) e está estruturalmente impedida de produzir proposta: mesmo com evidência de assinatura de falha, a condição (d) de governança causal nunca encontra um par `(bloco, campo)` que governe o componente, e o laço tem hoje **uma única regra produtiva** (`REGRA_TIMEOUT`).

**(b) A suíte de testes escreve na trilha de auditoria real do autor.** `tests/test_setup_cli.py:650-683` executa `setup apply` sem `--trilha` e sem redirecionar `FPCH_AUDIT_LOG`, de modo que cada execução acrescenta um evento `install` a `~/.fpch/audit.jsonl`. Na data desta conferência, a trilha real tinha 98 linhas, das quais **30 vêm de repositórios temporários do pytest** (execuções de 13/09, 14/09 e 28/09/2026) — a própria reexecução da §41.1 acrescentou mais uma. A cadeia por hash segue íntegra (`fpch audit --verify` → "98 linha(s)"), mas a trilha não serve como dado empírico sem ser filtrada primeiro. Não existe `tests/conftest.py` no repositório.

**(c) `fpch.policy.toml` da raiz está defasado e tem precedência sobre o catálogo do C27.** O arquivo ocupa o nível 3 da cascata de política (`policy.py:16-25`) e ainda lista 9 modelos com Gemini 3.5 Flash — que o `agy` não aceita mais desde a sondagem de C27 — sem nenhuma entrada `codex`; hoje, `fpch plan mechanical` executado na raiz do repositório escolhe "Gemini 3.5 Flash (Medium)". O comentário do próprio arquivo (`fpch.policy.toml:15-16`) diz que ele "reproduz o default embutido", o que é falso desde C27 — o commit `58c288a` atualizou o catálogo embutido em `models.py` sem tocar o TOML da raiz.

**(d) Lacuna de teste na recusa (d) da C8.** Nenhum teste de `improve.py` alimenta um evento com `timeout_origin="explicit"`, `timed_out=False` ou `timeout_s` diferente do vigente para afirmar `RECUSA campo_nao_governa_o_componente`. O ramo existe em `improve.py:527-558` (a condição (d) de governança causal) e, até esta sessão, só havia sido exercitado pela reexecução manual do episódio de 03/09/2026 documentada em [`protecao-fantasma-no-laco-2026-09-03.md`](../decisoes/protecao-fantasma-no-laco-2026-09-03.md) — a reprodução da §41.1 é a segunda vez que esse ramo é observado, e continua sem cobertura automatizada.

Os quatro achados foram registrados como **C30–C33** no `TODO.md` e entraram na `TCC_v7.md` — na Tabela 8 ("afirmações × evidência × situação") e na subseção de achados dos ciclos de desenvolvimento, como limites honestos do artefato em vez de alegações superadas. C4 e C7 continuam parciais; C15, C23 e C25 continuam abertos.
