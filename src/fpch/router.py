"""Roteamento por classe de tarefa, com escalada e consciência de pool.

Política, em uma frase: comece pelo mais barato que plausivelmente resolve;
escale só diante de falha; nunca gaste cota cara sem ter tentado a barata.

Escalar por FALHA (exit≠0, vazio, timeout) é objetivo e barato de detectar.
Escalar por QUALIDADE ruim exigiria um avaliador — e um avaliador que o próprio
agente pode influenciar é convite a Goodhart. Por isso o gatilho aqui é só falha:
juízo de qualidade fica com o humano ou com um hook determinístico externo.
"""

from __future__ import annotations

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
) -> backends.Result:
    """Roteia o prompt, escalando na cadeia enquanto houver falha.

    `model_id` força um modelo específico e desliga a escalada — útil para
    comparar modelos de forma controlada (é assim que se produz dado empírico
    honesto para o TCC, em vez de anedota).
    """
    if model_id:
        if model_id not in BY_ID:
            raise NoBackendAvailable(
                f"modelo desconhecido: {model_id!r}. Use `fpch models` para listar."
            )
        chain = [BY_ID[model_id]]
    else:
        chain = plan(task, exclude_pools=exclude_pools, max_escalations=max_escalations)

    last: backends.Result | None = None
    previous_model: str | None = None

    for model in chain:
        rec = audit.Record(
            task_class=task.value,
            backend=model.backend,
            model=model.id,
            pool=model.pool.value,
            prompt_chars=len(prompt),
            escalated_from=previous_model,
            label=label,
        )
        try:
            result = backends.invoke(
                model,
                prompt,
                workdir=workdir,
                timeout_s=timeout_s,
                allow_write=allow_write,
                extra_dirs=extra_dirs,
            )
        except backends.BackendUnavailable as exc:
            rec.ok = False
            rec.error = str(exc)
            audit.write(rec)
            previous_model = model.id
            continue

        rec.ok = result.ok
        rec.exit_code = result.exit_code
        rec.latency_s = result.latency_s
        rec.output_chars = len(result.text)
        rec.error = result.error
        audit.write(rec)

        if result.ok:
            return result

        last = result
        previous_model = model.id

    if last is not None:
        return last
    raise NoBackendAvailable(f"toda a cadeia falhou para '{task.value}'")
