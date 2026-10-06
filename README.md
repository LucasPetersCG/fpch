# FPCH

FPCH (Framework Pessoal de Construção de Harness) é um protótipo de harness pessoal para agentes de codificação de linha de comando. Ele separa **Política** (um arquivo TOML declarado pelo desenvolvedor) de **Mecanismo** (código determinístico que executa e confere). Não tem dependências externas de execução: usa só a biblioteca padrão e os CLIs de agente que o desenvolvedor já tem instalados.

Há duas faces:

- **Criação:** `discover` → `interview` → `setup plan` → `setup apply`, com conferência offline de checkpoints de servidores MCP antes de qualquer instalação.
- **Execução:** roteamento de tarefas entre modelos e adaptadores de agente, com contrato de saída por sentinela; verificação por hooks determinísticos (`fpch check`); trilha de auditoria encadeada por hash; e um laço de autocorreção (`fpch improve`) que apenas propõe alterações de política e só as aplica mediante autorização explícita.

## Estado

Protótipo de pesquisa, não um produto. A versão avaliada no trabalho acadêmico a que este código acompanha corresponde ao commit `3c7d800` (14 set. 2026).

Implementado:

- Exploração de repositório (`discovery.py`) e entrevista de preferências (`interview.py`).
- Conferência de checkpoints MCP: SHA-256 e nome do artefato obtido localmente, e varredura de Unicode oculto em metadados (`mcp.py`).
- Roteamento por classe de tarefa com escalada, catálogo de modelos e pools de cota (`router.py`, `models.py`).
- Quatro adaptadores de agente (`backends.py`): `agy`, `copilot`, `claude` e `codex`.
- Trilha de auditoria em JSONL com encadeamento por hash e verificação (`audit.py`).
- Política externa em TOML com cascata de precedência (`policy.py`).

Parcial ou não implementado:

- `setup plan` / `setup apply` gravam apenas `AGENTS.md`, `CLAUDE.md` e o manifesto `.fpch/setup-manifest.json`. Não geram política nem verificadores.
- A conferência MCP não instala nada: não há download, resolução de versões nem execução.
- A verificação é disparada por comando (`fpch check`), não pela saída do agente.
- Não há confinamento pelo sistema operacional.
- O laço de autocorreção só propõe mudanças de política a partir de regras fixas no código (`REGRAS` em `improve.py`; hoje `ampliar_timeout` e `reconhecer_assinatura_de_falha`).

## Requisitos e instalação

Python >= 3.11. Testado em Windows 11 com Python 3.14.

```
uv sync
uv run fpch --help
```

ou, sem uv:

```
pip install -e .
fpch --help
```

Os adaptadores de agente (`ask`, `canib run`) exigem que os CLIs dos fornecedores já estejam instalados e autenticados. O FPCH invoca apenas o binário oficial em modo headless; não lê credenciais.

## Uso

Opções globais vêm antes do subcomando (`fpch --policy p.toml plan hard`). Códigos de saída: 0 sucesso, 1 o comando rodou e reprovou, 2 o comando não chegou a rodar (política ilegível, uso errado).

| Subcomando | Função |
|---|---|
| `models` | Lista o catálogo de modelos por pool de cota. |
| `plan <classe>` | Mostra a cadeia de escalada para `mechanical`, `standard` ou `hard`, sem gastar cota. |
| `ask <classe> "prompt"` | Roteia um prompt para o modelo adequado, com escalada em caso de falha. |
| `config --dump` | Mostra a política efetiva, bloco a bloco, com a origem de cada valor. |
| `check` | Roda os hooks determinísticos declarados na política. |
| `audit` | Resumo de uso por pool; `--verify` confere o encadeamento de hashes, `--causes` e `--process` agregam falhas. |
| `improve` | Lê a trilha e propõe alteração de política; `--apply <id>` autoriza uma proposta. |
| `discover <repo>` | Descobre linguagens, dependências e CI/CD, somente leitura. |
| `interview <repo>` | Coleta preferências explícitas usando a descoberta como contexto. |
| `setup plan` / `setup apply` | Gera um plano local e aplica exatamente o plano confirmado. |
| `mcp verify` / `mcp scan` | Confere um artefato contra um checkpoint MCP; varre metadados em busca de Unicode oculto. |
| `cite-check`, `quote-check` | Conferência de citações (ver "Módulos de apoio à pesquisa"). |
| `canib add/list/accept/reject/run/runall` | Fila de análise de repositórios de terceiros, com aprovação humana. |

Fluxo de criação:

```
uv run fpch discover . --json
uv run fpch interview . --answers respostas.json --output preferencias.json
uv run fpch setup plan . --answers preferencias.json --json --output plano.json
uv run fpch setup apply plano.json --confirm <plan_id>
```

Para ver o formato esperado das respostas e das demais opções, use `fpch <subcomando> --help`.

Verificação:

```
uv run fpch check
uv run fpch audit --verify
```

## Política

`fpch.policy.toml` declara o catálogo de modelos, os limites de escalada e de tempo, as assinaturas de falha e os hooks de verificação. O arquivo na raiz deste repositório reproduz o default embutido e acrescenta um hook `pytest`; serve de modelo.

A política é carregada uma vez, antes de qualquer subcomando. Precedência, da maior para a menor:

1. `--policy <caminho>`
2. variável de ambiente `FPCH_POLICY`
3. `./fpch.policy.toml`
4. `~/.fpch/policy.toml`
5. default embutido em `src/fpch/policy.py`

A ausência de arquivo nunca é erro. Arquivo malformado, com chave desconhecida ou valor de tipo errado falha com código 2, sem degradar em silêncio. O default embutido não declara nenhum hook: executar comandos é ato explícito do operador. Alguns blocos (taxonomia de causa raiz, regras de proveniência e anti-injeção da canibalização, padrões de contenção, portão de aprovação humana) não são configuráveis por TOML; `fpch config --dump` os lista como imutáveis.

## Testes

```
uv run pytest
```

ou `PYTHONPATH=src python -m pytest`. Na última execução: 910 testes passaram e 12 foram ignorados. A maior parte dos ignorados é de testes que exigem criar links simbólicos, o que o Windows não permite sem privilégio adequado (modo desenvolvedor ou administrador).

## Módulos de apoio à pesquisa

Não fazem parte do núcleo do harness:

- `cannibalize.py`: ingestão de repositórios e artigos de terceiros, mediante aprovação humana, para extrair o mecanismo útil sem copiar código.
- `citations.py`: valida deterministicamente citações `arquivo:linha` contra o disco.
- `quotes.py`: valida deterministicamente trechos entre aspas de um fichamento contra o texto integral da fonte.

## Documentação

- `docs/decisoes/fatia-funcional-2026-08-31.md`: especificação de implementação da fatia funcional (política, hooks, trilha, laço de autoaprimoramento).
- `docs/decisoes/protecao-fantasma-no-laco-2026-09-03.md`: registro de um caso em que o laço de autoaprimoramento produziu uma proteção que não governava a falha, e o experimento de controle.
- `docs/analises/fpch-evidencias-desenvolvimento.md`: evidências coletadas durante o desenvolvimento, incluindo o dogfood no próprio repositório.

## Licença

MIT. Ver [`LICENSE`](LICENSE).
