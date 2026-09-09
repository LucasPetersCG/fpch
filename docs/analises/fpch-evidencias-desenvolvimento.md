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
