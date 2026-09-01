# Fatia funcional do FPCH — especificação de implementação

> **Escrita em 31/08/2026, sessão 24.** Contrato único e vinculante para todos os agentes que implementarem esta rodada.
> Decisão do autor nesta sessão: o protótipo precisa **funcionar e ser usável** antes da entrega do TCC (06/10/2026), ainda que com escopo reduzido. Melhorias de resultado ficam para a defesa.
>
> 🧭 [`../PROTOTIPO-GAP.md`](../PROTOTIPO-GAP.md) · [`../tcc/_DOSSIE-ENGENHARIA-FPCH.md`](../tcc/_DOSSIE-ENGENHARIA-FPCH.md) · [`../tcc/TCC_v4.md`](../tcc/TCC_v4.md) · [`../../TODO.md`](../../TODO.md)

---

## 1. O objetivo, em uma frase

Fazer o artefato **instanciar as cinco camadas que o TCC descreve**, na menor versão que funcione de ponta a ponta, e produzir a trilha de dados que os pedidos **FB3** e **FB4** do orientador exigem no texto.

O critério de pronto não é cobertura de recursos. É este: **o autor consegue usar o `fpch` no dia a dia, e a execução deixa uma trilha auditável da qual sai um número citável.**

## 2. O que entra e o que fica de fora

| Camada do TCC | Nesta fatia | Forma reduzida adotada |
|---|---|---|
| 1, Agente Base | 🟡 parcial | Invocação dos CLIs já existe. Corrigem-se os defeitos de contenção. **MCP fica fora** e continua declarado ausente no texto |
| 2, IHR | 🟢 entra | Costuras nomeadas: interface declarada de adaptador de backend e de validador |
| 3, Política | 🟢 entra | Política externa em TOML lida em tempo de execução, com fronteira explícita do que **não** é externalizável |
| 4, Camada Determinística | 🟢 entra | Hooks determinísticos plugáveis, declarados na política, com veredito registrado na trilha |
| 5, Observabilidade e auto-aprimoramento | 🟢 entra | Trilha por trajetória, encadeada por hash, e laço com portão de aceitação |

**Fora do escopo, e a razão de cada corte** — o texto do TCC já declara estes vazios, e continuarão declarados:

- **Agência via MCP.** Custo alto, e a alegação de ausência já está no texto.
- **Custo em *tokens* reais.** Os CLIs de terceiros não devolvem contagem no modo usado (`backends.py:116-119` só captura texto). Ficam os proxies em caracteres, **rotulados como proxies**.
- **Marcos temporais `t_err`/`t_lock`/`t_obs`.** Exigem granularidade por turno, que o FPCH não tem por delegar o laço de agente ao CLI de terceiro.
- **Os nove subtipos de causa raiz.** Entram as **três categorias** de Zhao et al. e um subtipo livre validado contra lista declarada na política. A taxonomia completa exige rotulagem manual que esta fatia não comporta.

## 3. Princípio que atravessa tudo

**Promessa que falha jamais é lida como promessa cumprida.** Já está escrito na `TCC_v4`, e esta fatia o torna executável em três pontos: verificador sem critério devolve `absent` e nunca `pass`; falha de escrita na trilha é observável e nunca silenciosa; e proposta de auto-aprimoramento sem evidência auditável é recusada, com a recusa registrada.

---

## 4. Fase 1 — Trilha por trajetória (`audit.py`, `router.py`)

### 4.1 Evento único e tipado

Substitui o `Record` atual. Um evento por linha, JSONL, apêndice apenas.

```python
@dataclass
class Event:
    event: str                 # "start" | "attempt" | "verify" | "end"
    trajectory_id: str         # chave comum a toda a trajetória
    seq: int                   # ordem dentro da trajetória, começa em 0
    ts: str                    # ISO-8601 com microssegundos e offset
    schema_version: int = 2

    # contexto (todos os eventos)
    task_class: str | None = None
    label: str | None = None

    # attempt
    backend: str | None = None
    model: str | None = None
    pool: str | None = None
    prompt_chars: int | None = None     # PROXY de tokens de entrada
    output_chars: int | None = None     # PROXY de tokens de saída
    exit_code: int | None = None
    latency_s: float | None = None
    ok: bool | None = None
    error: str | None = None
    escalated_from: str | None = None

    # verify
    component: str | None = None        # qual hook emitiu o veredito
    verdict: str | None = None          # "pass" | "fail" | "absent"
    evidence: str | None = None         # trecho da saída do hook, truncado

    # diagnóstico
    cause_category: str | None = None   # "epistemica" | "competencia" | "ambiente"
    cause_subtype: str | None = None
    fault_side: str | None = None       # "modelo" | "infraestrutura"

    # end
    outcome: str | None = None          # "ok" | "failed" | "abandoned"
    attempts: int | None = None
    duration_s: float | None = None

    # integridade
    prev_hash: str | None = None
    hash: str = ""
```

**`run_id` sai.** Hoje é `uuid4()` por linha (`audit.py:47`) e o nome engana. Quem agrupa é `trajectory_id`.

### 4.2 Encadeamento por hash

`hash` = SHA-256 hexadecimal do JSON canônico do evento (chaves ordenadas, `ensure_ascii=False`) **com o campo `hash` removido e o `prev_hash` presente**. `prev_hash` é o `hash` do último evento já gravado no arquivo, ou `None` na primeira linha.

Isto é o que de fato sustenta a alegação metodológica do texto. Abrir o arquivo em modo `"a"` não impede reescrita — apenas não a facilita. O encadeamento torna a reescrita **detectável**, que é o requisito quando o pesquisador é também o operador.

`verify_chain(path) -> (ok: bool, primeiro_indice_quebrado: int | None)`, exposta em `fpch audit --verify`.

### 4.3 Escrita nunca silenciosa

O `except OSError: pass` de `audit.py:58-59` sai. No lugar: incrementa um contador de módulo, emite **uma** mensagem em `stderr` por processo, e `write()` devolve `bool`. Falha de trilha continua não derrubando a chamada que a originou, mas deixa de ser invisível.
Na leitura, `audit.py:74-75` passa a **contar** linhas corrompidas em vez de descartá-las em silêncio.

### 4.4 `router.route()`

- `trajectory_id` gerado **antes** do laço `for model in chain:` e propagado a todos os eventos.
- Aceita `trajectory_id` opcional como parâmetro, para que um chamador de nível acima (a canibalização, por exemplo) amarre dois estágios na mesma trajetória.
- Emite `start` antes do laço, um `attempt` por tentativa, e `end` nos **três** caminhos de saída hoje existentes: sucesso (`router.py:109`), último resultado após a cadeia falhar (`:115`) e cadeia vazia (`:116`).
- `outcome`: `"ok"` no sucesso; `"failed"` quando a cadeia inteira terminou com resultado ruim; `"abandoned"` quando nenhum backend chegou a ser invocado.
- `read_all()` passa a ordenar por `(trajectory_id, seq)` e ganha `trajectories()` que devolve as trajetórias agrupadas.
- `summary()` passa a agregar **por trajetória** além de por reserva de cota: total de trajetórias, taxa de sucesso, tentativas por trajetória, latência por trajetória.

### 4.5 Compatibilidade

Linha sem `schema_version` é lida como versão 1 e convertida em memória para um evento `attempt` órfão, com `trajectory_id` sintético derivado do `label` quando houver. A trilha antiga do autor não é reescrita nem descartada.

---

## 5. Fase 2 — Política externa (`policy.py`, novo)

### 5.1 Formato e cascata

TOML, lido com `tomllib` da biblioteca padrão. **Nenhuma dependência externa entra** — `pyproject.toml` continua com dependências vazias.

Precedência, da maior para a menor:
1. `--policy <caminho>` na linha de comando
2. variável de ambiente `FPCH_POLICY`
3. `./fpch.policy.toml` no diretório de trabalho
4. `~/.fpch/policy.toml`
5. **default embutido** no código

Ausência de arquivo cai no default sem erro. Arquivo **malformado ou com chave desconhecida falha alto e cedo** — nunca degrada em silêncio, porque política que falha em silêncio é exatamente o modo de falha que o trabalho inteiro combate.

### 5.2 O que é externalizável

```toml
[meta]
versao = 1

[escalada]
max_escalations = 2
ordenar_por = ["cost", "-power"]

[backends]
timeout_s = 600
arg_limit = 24000

[falha]
min_chars = 40
assinaturas = ["no output produced", "auto-denied", "..."]

[[modelos]]
id = "Gemini 3.5 Flash (Medium)"
backend = "agy"
pool = "agy:google"
power = 3
cost = 1
good_for = ["mechanical", "standard"]
notes = ""

[[hooks]]
nome = "pytest"
cmd = ["uv", "run", "--with", "pytest", "pytest", "-q"]
quando = "sempre"
criterio = "exit_zero"

[causa_raiz]
categorias = ["epistemica", "competencia", "ambiente"]
subtipos = ["..."]
```

### 5.3 O que NÃO é externalizável, e por quê

Fica em código, e o arquivo de política de referência traz a lista **em comentário**, declarando a razão:

- as regras de proveniência (`cannibalize.py:173-205`);
- a cláusula anti-injeção do prompt de extração (`cannibalize.py:208-247`);
- os padrões de contenção (`backends.py:128-129`);
- o portão de aprovação humana.

**A razão é uma só:** essas quatro *são* a defesa contra o conteúdo hostil que a canibalização declara como premissa (`cannibalize.py:17-27`). Externalizá-las num arquivo gravável entregaria a defesa a um agente que corre com `--allow-write` — o mesmo anti-padrão que o TCC condena ao citar Rashidi.

Esta linha divisória é contribuição própria e deve ser registrada como tal: **nem toda política deve ser externalizável, e o critério é se aquela política é ela mesma a defesa.** Nenhuma das fontes lidas faz essa distinção.

### 5.4 `fpch config --dump`

Imprime o valor efetivo de cada bloco **com a origem** (default embutido, arquivo do projeto, arquivo do usuário, ou linha de comando), e lista à parte os blocos não-externalizáveis com a marca de imutáveis.

---

## 6. Fase 3 — Hooks determinísticos (`hooks.py`, novo)

Um hook é um comando declarado na política. Executa sem *shell*, com `argv` em lista.

- `criterio = "exit_zero"` — passa se o código de saída for zero.
- `criterio = "saida_vazia"` — passa se não houver saída.
- **Hook sem `criterio` reconhecido devolve `absent`, nunca `pass`.** É o ponto onde o princípio da seção 3 vira código.

Cada execução emite um evento `verify` na trilha, com `component` igual ao nome do hook, `verdict`, e `evidence` truncada em 500 caracteres. Resolve o campo #5 da Tabela 1 e faz com que, pela primeira vez, **um validador deixe rastro na trilha** — hoje nem `citations` nem `quotes` importam `audit`.

Comandos: `fpch check` roda os hooks aplicáveis e devolve código de saída diferente de zero se qualquer um reprovar.

---

## 7. Fase 4 — Costuras e contenção (`backends.py`, `cannibalize.py`)

### 7.1 Interface declarada

`Protocol` de adaptador de backend, com `nome`, `build_argv(model, prompt, ctx)` e **declaração do regime de contenção que o adaptador de fato suporta**. Despacho por dicionário, no lugar da cadeia `if/elif` de `backends.py:143-167`.

### 7.2 Os três defeitos de contenção

Nenhum tem teste hoje, e é o teste de contrato que os prende:

1. `allow_write` é **inerte** no `agy` (`backends.py:143-153`).
2. O backend `claude` roda **sem contenção, sem diretório de trabalho e sem diretórios extras** (`backends.py:162-164`).
3. `extra_dirs` só é honrado pelo `agy` (`backends.py:147-148`).

Onde o adaptador **não puder** honrar o regime pedido, ele **declara a incapacidade** e `invoke()` recusa a chamada, em vez de executar com contenção menor do que a solicitada. Silenciosamente conceder menos contenção do que se pediu é a mesma classe de falha do `exit 0` que o módulo já documenta.

### 7.3 Defeito de projeto no encaminhamento de prompt longo

> ⚠️ **Retificado em 31/08/2026, depois de conferir `--help` dos três binários.** A redação original desta subseção afirmava que `copilot` e `claude` **não** aceitam `--add-dir`, e que por isso prompt acima de 24.000 caracteres quebraria nesses dois backends. **Isso é falso:** os três CLIs instalados aceitam `--add-dir`. A quebra descrita não se reproduz.
>
> O registro fica porque o modo de falha é o mesmo que este projeto já documentou três vezes — **concluir a partir da leitura do código sem conferir a fonte primária**, que aqui era o `--help` do binário. A suposição nasceu de ler `backends.py:160,164` e inferir a incompatibilidade em vez de verificá-la.

O **defeito de projeto** é real e permanece: `_stage_prompt` (`backends.py:50-67`) decide sozinho, fora do adaptador, uma flag específica de um CLI e a anexa a todos indistintamente. Funciona hoje por coincidência — os três aceitam a mesma flag. O encaminhamento de prompt longo passa a ser responsabilidade declarada do adaptador, e o registro de adaptadores ganha um teste que falha se algum deles emitir `--add-dir` sem ter declarado a capacidade.

### 7.4 Portão de aprovação na fronteira do módulo

`cannibalize.run()` (`cannibalize.py:328-417`) passa a conferir `target.status` na entrada e a recusar alvo não aprovado. O portão da CLI (`cli.py:213-219`) permanece, agora como conveniência de mensagem, não como única barreira.

Enquanto isso não for feito, o protótipo instancia o mesmo anti-padrão que o texto condena: invariante que reside na interface protege apenas quem passa por ela.

---

## 8. Fase 5 — O laço (`improve.py`, novo)

Lê a trilha e propõe alteração de política. **Propor e autorizar são operações distintas**, e essa separação é o critério de aceitação que a `TCC_v4` descreve a partir de Xu et al.

`fpch improve` — analisa, e **apenas escreve uma proposta** em `~/.fpch/propostas/<id>.json`. Nunca altera política.

Uma proposta só é gerada quando as **três** condições se cumprem:

1. **Evidência auditável.** Existe evento `verify` com `verdict = "fail"` e `evidence` não vazia, dentro de uma trajetória cuja cadeia de hash é válida.
2. **Lado da falta na infraestrutura.** `fault_side = "infraestrutura"`. Falha originada no modelo não se conserta editando documento de política, e tentar consertá-la assim produz exatamente a regra inócua que Wang et al. descrevem.
3. **Caminho de remoção previsto.** Toda proposta declara o que adiciona **e** o que a tornaria removível, para que a política não cresça monotonicamente até deixar de ser inspecionável — que era a sua única vantagem.

`fpch improve --apply <id>` — aplica, e antes disso reconfere que o `trajectory_id` citado existe na trilha e que a cadeia continua íntegra. Recusa é registrada como evento na trilha, com a razão.

---

## 9. Ordem de execução e propriedade de arquivos

| Onda | Escopo | Arquivos que pode tocar |
|---|---|---|
| 1A | Fase 1 | `src/fpch/audit.py`, `src/fpch/router.py`, `tests/test_audit.py`, `tests/test_router.py` |
| 1B | Fase 2 | `src/fpch/policy.py` (novo), `fpch.policy.toml` (novo), `tests/test_policy.py` |
| 1C | Fase 4 | `src/fpch/backends.py`, `tests/test_backends.py` |
| 2A | Integração da política | `src/fpch/models.py`, `src/fpch/cli.py` |
| 2B | Fase 3 | `src/fpch/hooks.py` (novo), `tests/test_hooks.py` |
| 2C | Portão na fronteira | `src/fpch/cannibalize.py`, `tests/test_cannibalize.py` |
| 3 | Fase 5 | `src/fpch/improve.py` (novo), `tests/test_improve.py`, `src/fpch/cli.py` |

Regra dura: **um agente só escreve nos arquivos da sua linha.** Precisando de mudança fora dela, reporta em vez de editar.

## 10. Invariantes que nenhuma onda pode violar

1. `pyproject.toml` continua **sem dependências externas**. `pytest` entra apenas como grupo de desenvolvimento.
2. Nada de `shell=True`. `argv` sempre lista.
3. Toda saída de agente é **dado não-confiável**, nunca instrução.
4. Só o binário oficial de cada fornecedor, em modo headless documentado. Nunca ler credencial, nunca forjar cabeçalho.
5. A suíte inteira passa ao fim de cada onda. Teste que falha bloqueia a onda seguinte.
6. Nada é commitado sem o autor pedir.

---

## 11. Registro de execução — encerrado em 01/09/2026

A fatia foi executada em duas sessões. A sessão 24 (31/08) rodou as ondas 1A, 1B e 1C e foi interrompida no meio da segunda leva. A sessão 25 (01/09) retomou do ponto exato e fechou o restante.

| Onda | Escopo | Estado | Onde conferir |
|---|---|---|---|
| 1A | Trilha por trajetória | ✅ | `audit.py`, `router.py`, `tests/test_audit.py`, `tests/test_router.py` |
| 1B | Política externa | ✅ | `policy.py`, `fpch.policy.toml`, `tests/test_policy.py` |
| 1C | Costuras e contenção | ✅ | `backends.py`, `tests/test_backends.py` |
| 2A | Integração da política | ✅ | `models.py:118-227`, `cli.py:288-296,392-410`, `tests/test_cli_policy.py` |
| 2B | Hooks determinísticos | ✅ | `hooks.py` (sem defeito encontrado), `tests/test_hooks.py` |
| 2C | Portão na fronteira | ✅ | `cannibalize.py:72-90,384-389`, `tests/test_cannibalize.py` |
| 3 | O laço | ✅ | `improve.py`, `cli.py:230,420`, `tests/test_improve.py` |

**Suíte: 235 testes, todos passando.** Protótipo: 4.940 linhas em 12 módulos, 3.164 linhas de teste.

### 11.1 O que a execução acrescentou ao contrato

**Um requisito da §4.2 caiu no vão entre ondas e só foi achado na verificação ponta a ponta.** `verify_chain()` foi escrita e testada na onda 1A, mas `fpch audit --verify` nunca foi ligado à CLI: a onda 1A não podia tocar `cli.py`, e a onda 2A não foi informada do item. A suíte inteira passava com o requisito faltando, porque a função tinha teste de unidade e o comando não tinha teste de ponta a ponta.

O registro fica porque a lição é do próprio objeto deste trabalho: **a divisão de propriedade de arquivos que evita conflito entre agentes também cria vãos onde requisitos somem**, e nenhum agente individual erra. O que fecha o vão não é mais disciplina de cada onda — é conferir os comandos de fora, rodando o binário, contra a lista de requisitos do contrato. Foi assim que este apareceu.

Ao ligar o comando, a distinção que faltava entrou junto: trilha vazia ou inexistente **não** imprime "íntegra". Imprime "nada foi conferido — isto NÃO é integridade confirmada, é ausência de dado", com código de saída 1. É a §3 aplicada ao lugar onde ela mais importa, o comando que sustenta a alegação metodológica de detecção de adulteração.

### 11.2 Duas decisões tomadas durante a execução, que valem revisão do autor

1. **`fpch check` sem nenhum hook declarado sai com código 1**, não 0. `hooks.overall(())` já devolve `absent`, e devolver sucesso faria um laço automatizado ler ausência de verificação como aprovação.
2. **`fpch improve --apply` exige `--policy` explícito.** A cascata decide qual política *vale*; não pode decidir qual arquivo é *editado*.

### 11.3 Dívida declarada, a pagar na próxima rodada de código

`router.plan/route` não recebem catálogo por parâmetro, e `router.py` pertencia a outra onda quando a 2A precisou dele. O catálogo em vigor é costurado por estado de módulo (`models.use_catalog`, com `BY_ID` mutado no lugar para que `--model` e a cadeia de escalada não discordem). **É andaime**, está comentado em `models.py:172-183`, e a próxima rodada deve descer o catálogo como parâmetro explícito e remover o global.
