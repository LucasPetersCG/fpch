"""Roteamento por classe de tarefa, com escalada e consciência de pool.

Política, em uma frase: comece pelo mais barato que plausivelmente resolve;
escale só diante de falha; nunca gaste cota cara sem ter tentado a barata.

Escalar por FALHA (exit≠0, vazio, timeout, contrato de saída não cumprido) é
objetivo e barato de detectar.
Escalar por QUALIDADE ruim exigiria um avaliador — e um avaliador que o próprio
agente pode influenciar é convite a Goodhart. Por isso o gatilho aqui é só falha:
juízo de qualidade fica com o humano ou com um hook determinístico externo.
"""

from __future__ import annotations

import time
import uuid
from pathlib import Path

from . import audit, backends
from .models import BY_ID, Model, Pool, TaskClass, candidates


class NoBackendAvailable(RuntimeError):
    pass


def _installed(models: list[Model]) -> list[Model]:
    return [m for m in models if backends.available(m.backend)]


def plan(
    task: TaskClass,
    *,
    exclude_pools: frozenset[Pool] = frozenset(),
    max_escalations: int = 2,
) -> list[Model]:
    """Cadeia de tentativa: [preferido, fallback1, ...]. Não invoca nada."""
    chain = _installed(candidates(task, exclude_pools))
    if not chain:
        raise NoBackendAvailable(
            f"nenhum backend instalado atende '{task.value}' "
            f"(pools excluídos: {sorted(p.value for p in exclude_pools)})"
        )
    return chain[: max_escalations + 1]


def route(
    task: TaskClass,
    prompt: str,
    *,
    workdir: Path,
    model_id: str | None = None,
    exclude_pools: frozenset[Pool] = frozenset(),
    max_escalations: int = 2,
    timeout_s: int = backends.DEFAULT_TIMEOUT_S,
    allow_write: bool = False,
    extra_dirs: list[Path] | None = None,
    label: str | None = None,
    trajectory_id: str | None = None,
    output_contract: bool = True,
) -> backends.Result:
    """Roteia o prompt, escalando na cadeia enquanto houver falha.

    `model_id` força um modelo específico e desliga a escalada — útil para
    comparar modelos de forma controlada (é assim que se produz dado empírico
    honesto para o TCC, em vez de anedota).

    `trajectory_id` permite que um chamador de nível acima (a canibalização, por
    exemplo) amarre dois estágios sucessivos à mesma trajetória. Sem ele, cada
    chamada abre a sua — o que é o correto quando ninguém acima está coordenando,
    e errado quando alguém está: daí o parâmetro em vez de gerar sempre.

    `output_contract` (default ligado) é repassado a `backends.invoke`: contrato
    de saída não cumprido volta como `ok=False` e escala como qualquer falha.
    Desligar é opt-out deliberado, e cada `attempt` grava qual contrato valeu.

    Toda saída emite `end`. Um roteamento que termina sem registrar como terminou
    é indistinguível, na leitura da trilha, de um processo que morreu no meio.
    """
    tid = trajectory_id or uuid.uuid4().hex[:16]
    started = time.monotonic()
    attempts = 0

    def emit(**campos) -> None:
        # `seq` vem do contador da trajetória, não de um local: dois estágios que
        # compartilham `trajectory_id` precisam de numeração contínua.
        audit.write(
            audit.Event(
                trajectory_id=tid,
                seq=audit.next_seq(tid),
                task_class=task.value,
                label=label,
                **campos,
            )
        )

    def finish(outcome: str) -> None:
        emit(
            event="end",
            outcome=outcome,
            attempts=attempts,
            duration_s=round(time.monotonic() - started, 3),
        )

    if model_id:
        if model_id not in BY_ID:
            raise NoBackendAvailable(
                f"modelo desconhecido: {model_id!r}. Use `fpch models` para listar."
            )
        chain = [BY_ID[model_id]]
    else:
        chain = plan(task, exclude_pools=exclude_pools, max_escalations=max_escalations)

    # `model` no `start` fica preenchido só quando o roteamento foi fixado à mão:
    # é o que permite separar, na análise, escalada real de comparação controlada.
    emit(event="start", prompt_chars=len(prompt), model=model_id)

    last: backends.Result | None = None
    previous_model: str | None = None

    # Contrato EXIGIDO, conhecido antes de invocar: vai também na tentativa que
    # não chega a rodar (backend indisponível, contenção recusada). Sem ele, essa
    # linha seria lida como anterior a C18.
    contrato = (
        backends.FPCH_CONTRATO_SENTINELA if output_contract else backends.FPCH_CONTRATO_NENHUM
    )

    for model in chain:
        attempts += 1
        campos = {
            "event": "attempt",
            "backend": model.backend,
            "model": model.id,
            "pool": model.pool.value,
            # Prompt DO CHAMADOR, sem a instrução do contrato: mantém a série
            # comparável com a trilha anterior a C18. O acréscimo real é constante
            # (`backends.FPCH_CONTRATO_INSTRUCAO_CHARS`) e dedutível de `contract`.
            "prompt_chars": len(prompt),
            "escalated_from": previous_model,
            "contract": contrato,
        }
        try:
            result = backends.invoke(
                model,
                prompt,
                workdir=workdir,
                timeout_s=timeout_s,
                allow_write=allow_write,
                extra_dirs=extra_dirs,
                output_contract=output_contract,
            )
        except backends.BackendUnavailable as exc:
            emit(ok=False, error=str(exc), **campos)
            previous_model = model.id
            continue

        emit(
            ok=result.ok,
            exit_code=result.exit_code,
            latency_s=result.latency_s,
            output_chars=len(result.text),
            error=result.error,
            failure_signature=result.failure_signature,
            # O `Result` diz qual contrato de fato valeu; prevalece sobre o pedido.
            **{**campos, "contract": result.contract},
        )

        if result.ok:
            finish("ok")
            return result

        last = result
        previous_model = model.id

    if last is not None:
        # A cadeia inteira rodou e nenhuma tentativa deu certo.
        finish("failed")
        return last
    # Nenhum backend chegou a ser invocado: não há resultado ruim, há ausência de
    # resultado. Distinguir os dois é o que impede ler indisponibilidade de
    # ambiente como incompetência do modelo.
    finish("abandoned")
    raise NoBackendAvailable(f"toda a cadeia falhou para '{task.value}'")
