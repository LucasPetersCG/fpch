# Achado — o laço de auto-aprimoramento produziu uma proteção fantasma

> **Data:** 03/09/2026 · **Como apareceu:** ao projetar a demonstração de autocorreção (C8/A3), antes de escrevê-la · **Status:** defeito confirmado por execução, conserto decidido pelo autor
> **Módulos:** `src/fpch/improve.py`, `src/fpch/hooks.py`, `src/fpch/cli.py`, `src/fpch/policy.py`

---

## 1. O que aconteceu

A demonstração de autocorreção precisava de um caso controlado: um erro determinístico que o harness detecta, diagnostica e corrige. O caso natural era o estouro de tempo de um hook — há uma regra do laço exatamente para isso, `REGRA_TIMEOUT`.

Montado o cenário — política com um hook que dorme 3 segundos, teto de 1 segundo —, o ciclo rodou inteiro e **pareceu funcionar**:

```
$ fpch check --timeout 1 --cwd <ws>
  [FALHA] lento (criterio='exit_zero', 1.0212s)
        o hook estourou o timeout de 1.0s e foi interrompido

$ fpch improve
  proposta prop-f56ac66f8360 (regra ampliar_timeout)
    altera: [backends].timeout_s: 600 → 1200
    porque: O hook 'lento' reprovou por estouro de tempo [...] O teto vigente
            de 600s é política externalizável [...]

$ fpch improve --apply prop-f56ac66f8360
  backends.timeout_s: 600 → 1200

$ fpch check --cwd <ws>
  [ok  ] lento (criterio='exit_zero', 3.0444s)
veredito geral: pass
```

Falha detectada, causa diagnosticada, proposta gerada com justificativa e caminho de remoção, autorização humana exigida, correção aplicada, reverificação aprovada. É exatamente a demonstração que o TCC precisa apresentar.

**E é falsa.**

## 2. O experimento de controle

A reverificação passou porque a segunda chamada omitiu `--timeout`, e o default da CLI é 120 segundos — o hook dorme 3. Teria passado sem alteração nenhuma. Para isolar, bastou perguntar se a política governa o que falhou:

```
politica:  [backends] timeout_s = 1        hook dorme 3s
$ fpch check --cwd <ws>          (sem --timeout)
  [ok  ] lento (criterio='exit_zero', 3.0434s)
veredito geral: pass
```

Com o teto declarado em **1 segundo** e o hook levando **3**, o `check` aprova. Logo `fpch check` **nunca lê** `[backends].timeout_s`, e a alteração aplicada não teve papel causal nenhum no resultado.

## 3. Por que o defeito existe

Duas causas independentes, e é a soma delas que produz o efeito:

1. **`cli.py:611`** — `--timeout` do `fpch check` tem `default=hooks.DEFAULT_TIMEOUT_S` (120). Como argparse entrega um valor não-nulo, a política nunca é consultada. O `fpch ask` faz o contrário e certo: default `None`, e ausente herda da política.
2. **`hooks.py:79-82`** — `[backends].timeout_s` é, por desenho declarado, o teto de **chamada a modelo de terceiro** (600s, ordem de grandeza de rede), e `HookEntry` **não tem campo próprio de timeout**. Não havia, no esquema de política, nenhum campo que governasse o tempo de um hook.

`REGRA_TIMEOUT` mirava, portanto, o único campo com nome parecido — e nome parecido não é governança.

## 4. Por que isto importa mais do que um bug comum

O `improve.py` declara, no seu docstring de topo, que a condição (b) existe precisamente para evitar isto, citando Wang et al.:

> **Lado da falta na infraestrutura.** [...] Falha do lado do modelo não se conserta editando documento de política; tentar produz a regra inócua que Wang et al. descrevem — **proteção fantasma, que consome inspeção humana sem mudar comportamento nenhum.**

A condição (b) **passou**, e corretamente: a falta era mesmo do lado da infraestrutura. O laço acertou o *lado* e errou o *alvo*. Faltava uma pergunta que ninguém tinha feito: **o campo que estou alterando governa o componente que falhou?**

O resultado é a proteção fantasma na sua forma mais perigosa — não a que o sistema recusa, mas a que ele **aprova, documenta e submete a um humano**, que a autoriza de boa-fé porque a justificativa é coerente e o caminho de remoção está declarado. O portão humano funcionou como projetado e mesmo assim deixou passar, porque o portão confere autorização, não eficácia.

## 5. Um segundo caso da mesma doença, achado no caminho

`policy.CausaRaizBlock` declara uma lista de subtipos de causa raiz **provisória**, diferente da taxonomia que o `audit.py` passou a validar no esquema 3 (C14, mesma data). O comentário do próprio bloco admite: *"Lista provisória [...] o subtipo livre e sua lista fechada ficam para quem instanciar o diagnóstico de fato"*.

Ninguém consome esse bloco além da lista de blocos externalizáveis. É um bloco de política que **um usuário pode editar e que não governa nada** — a mesma classe de defeito, encontrada por olhar na mesma direção.

## 6. O conserto (decidido pelo autor em 03/09/2026)

Duas medidas, porque uma sem a outra deixa buraco:

**(a) Condição (d) do laço — governança do campo.** Uma proposta só nasce se o par `(bloco, campo)` que ela altera governa a origem do evento que a motivou. `Regra` deixa de ter bloco e campo fixos e passa a declarar um mapa de governança por origem. Sem governança estabelecida, **recusa** — falha fechada, código `campo_nao_governa_o_componente`, gravada na trilha como qualquer outra recusa. A verificação vale também em `aplicar()`, para que proposta gravada antes do critério não escape dele. Isto fecha a **classe** do defeito, inclusive para regras futuras.

**(b) Bloco `[verificacao]` com `timeout_s`.** Um campo que de fato governa o tempo de um hook, lido pelo `fpch check` (o `--timeout` da CLI passa a `default=None` e herda da política). `REGRA_TIMEOUT` escolhe o bloco pela origem: hook → `[verificacao].timeout_s`; chamada a modelo → `[backends].timeout_s`. Isto é o que permite o laço **fechar de verdade** num caso de hook.

Sobre o §5: `CausaRaizBlock` passa a ser populado a partir de `audit.CAUSE_TAXONOMY` (fonte única) e **sai** dos blocos externalizáveis. A distinção que isso materializa é a do princípio mestre: **teto de tempo é Política** — ajustável, e ajustá-lo não muda o significado de nada; **taxonomia é Mecanismo** — adotada de fonte publicada e citada no texto, e torná-la editável permitiria redefinir em silêncio o que os números da trilha significam.

## 7. O que isto diz sobre o método

O defeito não apareceu em revisão de código, em teste unitário nem em leitura. Apareceu quando se tentou **construir a demonstração** — e apareceu **antes** de a demonstração existir, no momento em que se perguntou por que o segundo `check` havia passado. Os 319 testes da suíte estavam verdes o tempo todo; nenhum deles perguntava se a política governava o que ela dizia governar.

Vale registrar o quase-acidente: se a demonstração tivesse sido escrita sem o experimento de controle, ela teria produzido uma trilha auditável, encadeada por hash, íntegra — **documentando com rigor uma correção que não ocorreu**. Rigor de registro não é rigor de inferência, e a trilha teria sido evidência impecável de um fato falso.

## 8. Estado implementado e integração acadêmica adiada

Em 08/09/2026, o conserto foi implementado e demonstrado de ponta a ponta: teto de 1 s → falha do hook → proposta sobre `[verificacao].timeout_s` → autorização humana → teto de 2 s → aprovação do mesmo hook, com cadeia íntegra. A suíte passou com 330 testes, incluindo recusa de eventos legados e de schema futuro como autorização para política nova.

Por decisão do autor em 08/09/2026, **nenhum arquivo do TCC, da apresentação ou do guia de estudos deve ser alterado nesta rodada**. A incorporação posterior fica preparada neste documento e deve, quando reaberta pelo autor:

1. substituir na Tabela 5 da `TCC_v6` a indicação de que a governança causal ainda não foi implementada;
2. substituir na §4.4 e na síntese dos limites a afirmação de que a autocorreção causal não foi demonstrada;
3. escolher, aceitar ou reescrever um dos candidatos do anexo abaixo;
4. preservar o teto de 30 páginas por substituição ou corte equivalente;
5. somente então regenerar e inspecionar `.docx` e `.pdf`, avaliando separadamente se slides e guia precisam acompanhar a mudança.

O gatilho para essa integração é uma solicitação explícita do autor para retomar os entregáveis acadêmicos. Até lá, o desenvolvimento continua na face de criação do FPCH, começando por C2.

---

## Anexo — candidato a texto do TCC

> **Não aplicado.** Escrito para o autor aceitar, recusar ou reescrever na leitura crítica de Resultados. O texto está em **30 páginas, no teto do manual**: entrar exige cortar equivalente. Encaixe natural na **§4.3** (que já trata do que se cobra de um verificador) ou na **§4.4** (o que o artefato ainda não demonstra).

Uma versão curta, para a §4.3, na sequência da regra dos casos anti-falso-positivo:

> A construção da demonstração de autocorreção expôs um limite que nenhum teste unitário havia alcançado. O laço de aprimoramento identificou corretamente uma falha determinística, atribuiu-a ao lado da infraestrutura e propôs a alteração de um campo do documento de política, com justificativa coerente e caminho de remoção declarado; a autorização humana foi concedida e a verificação seguinte foi aprovada. O ciclo inteiro pareceu fechar. Um teste de controle mostrou que o campo alterado não governava o componente que havia falhado, e que a aprovação subsequente teria ocorrido de todo modo. O laço acertara o lado da falta e errara o alvo da correção. A consequência de projeto foi acrescentar às condições de admissibilidade de uma proposta a exigência de que o campo alterado governe o componente que falhou, sob recusa em caso de dúvida. A consequência de método é menos confortável e merece registro: a trilha produzida naquele ciclo era íntegra, encadeada e auditável, e teria documentado com rigor uma correção que não ocorreu. **Rigor de registro não é rigor de inferência**, e um instrumento que confere autorização não confere, por isso, eficácia.

Uma frase, caso só caiba uma, para a §4.4:

> A demonstração de autocorreção revelou que uma proposta pode satisfazer todas as condições de admissibilidade do laço e ainda assim alterar um campo que não governa o componente que falhou; a condição de governança foi acrescentada em resposta, e o episódio fica registrado como evidência de que trilha íntegra não é, sozinha, evidência de eficácia.
