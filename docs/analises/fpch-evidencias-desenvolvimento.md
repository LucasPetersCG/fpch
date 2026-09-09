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
