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
