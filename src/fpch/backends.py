"""Invocação dos CLIs de agent.

REGRA DE OURO — a fronteira legal do FPCH:
Só invocamos o BINÁRIO OFICIAL de cada fornecedor, em modo headless documentado
por ele. Nunca lemos token/credencial, nunca forjamos header, nunca reempacotamos
a cota como API. Essa é exatamente a linha que separa uso pretendido de banimento
(Anthropic cortou clientes que forjavam identidade em 04/04/2026; Google responde
403; GitHub AUP proíbe). O ilícito é mentir sobre quem é o cliente — não automatizar.

Ver `docs/analises/camada-multi-modelo.md`.
"""

from __future__ import annotations

import shutil
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path

from .models import Model, Pool

# CreateProcess do Windows corta a linha de comando em 32767 chars. Deixamos folga
# para as demais flags. Acima disto o prompt vai para arquivo (ver `_stage_prompt`).
_ARG_LIMIT = 24_000

DEFAULT_TIMEOUT_S = 600


@dataclass
class Result:
    ok: bool
    text: str
    exit_code: int
    latency_s: float
    model: str
    backend: str
    pool: Pool
    error: str | None = None


class BackendUnavailable(RuntimeError):
    pass


def available(backend: str) -> bool:
    return shutil.which(backend) is not None


def _stage_prompt(prompt: str, workdir: Path) -> tuple[str, list[str]]:
    """Contorna o limite de linha de comando sem usar stdin.

    `agy -p` exige o prompt como argumento — não lê stdin. Para prompts grandes,
    gravamos em arquivo e mandamos o agente lê-lo, expondo o diretório via
    --add-dir. Devolve (prompt_efetivo, flags_extras).
    """
    if len(prompt) <= _ARG_LIMIT:
        return prompt, []

    workdir.mkdir(parents=True, exist_ok=True)
    staged = workdir / f"fpch-prompt-{int(time.time() * 1000)}.md"
    staged.write_text(prompt, encoding="utf-8")
    pointer = (
        f"Read the file at {staged} and execute the instructions it contains. "
        f"The file IS your task. Do not ask for confirmation."
    )
    return pointer, ["--add-dir", str(workdir)]


# Assinaturas de falha que os CLIs emitem COM EXIT CODE 0.
#
# Descoberto do jeito caro, em 16/07/2026: `agy --sandbox --mode plan` recusou as
# ferramentas de fetch, imprimiu "jetski: no output produced — a tool required the
# 'unsandboxed' permission that headless mode cannot prompt for, so it was
# auto-denied", e saiu com 0. O router marcou sucesso, e o estágio seguinte do
# canibalizador analisou a MENSAGEM DE ERRO como se fosse o artefato — produzindo
# 6 fichas inteiramente alucinadas.
#
# Lição: `exit == 0` não é contrato de sucesso nestes CLIs. Confiar nele é confiar
# num campo que o fornecedor não prometeu.
_FAILURE_SIGNATURES = (
    "no output produced",
    "auto-denied",
    "headless mode cannot prompt",
    "permission that headless",
    "ineligibletiererror",
    "client no longer supported",
    "quota exceeded",
    "rate limit exceeded",
)


def looks_like_failure(text: str) -> str | None:
    """Devolve a assinatura encontrada, ou None se o texto parece resposta real."""
    low = text.lower()
    for sig in _FAILURE_SIGNATURES:
        if sig in low:
            return sig
    # Resposta curta demais para ser trabalho: quase sempre é recusa ou saudação.
    if len(text.strip()) < 40:
        return "resposta suspeitamente curta"
    return None


def _run(argv: list[str], timeout_s: int) -> tuple[int, str]:
    """Executa sem shell. `argv` é lista — nunca string interpolada."""
    proc = subprocess.run(
        argv,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=timeout_s,
        shell=False,
    )
    out = (proc.stdout or "").strip()
    if not out and proc.stderr:
        out = proc.stderr.strip()
    return proc.returncode, out


def invoke(
    model: Model,
    prompt: str,
    *,
    workdir: Path,
    timeout_s: int = DEFAULT_TIMEOUT_S,
    sandbox: bool = True,
    allow_write: bool = False,
    extra_dirs: list[Path] | None = None,
) -> Result:
    """Chama o backend do modelo e devolve o resultado normalizado.

    `sandbox=True` e `allow_write=False` são os defaults deliberadamente. Um
    sub-agente que só pesquisa não precisa escrever — e o output dele é DADO
    NÃO-CONFIÁVEL, jamais instrução para o host.
    """
    if not available(model.backend):
        raise BackendUnavailable(f"{model.backend} não está no PATH")

    effective_prompt, extra = _stage_prompt(prompt, workdir)

    if model.backend == "agy":
        argv = ["agy", "--model", model.id, "-p", effective_prompt]
        if sandbox:
            argv.append("--sandbox")
        for d in extra_dirs or []:
            argv += ["--add-dir", str(d)]
        # NÃO usar `--mode plan` para conter escrita: ele também recusa as
        # ferramentas de LEITURA (fetch/web), e o agy sinaliza isso com exit 0 —
        # ver _FAILURE_SIGNATURES. A contenção vem do --sandbox; a validação
        # semântica do output vem de looks_like_failure().
        argv += extra

    elif model.backend == "copilot":
        argv = ["copilot", "-p", effective_prompt, "-C", str(workdir)]
        if not allow_write:
            # Allowlist explícita: leitura e busca, nada que mute o disco.
            argv += ["--deny-tool", "shell", "--deny-tool", "write"]
        argv += extra

    elif model.backend == "claude":
        argv = ["claude", "-p", effective_prompt]
        argv += extra

    else:
        raise BackendUnavailable(f"backend desconhecido: {model.backend}")

    started = time.monotonic()
    try:
        code, text = _run(argv, timeout_s)
        latency = time.monotonic() - started

        # exit 0 não basta — ver _FAILURE_SIGNATURES.
        signature = looks_like_failure(text) if code == 0 else None
        ok = code == 0 and bool(text) and signature is None
        if signature:
            error = f"falha semântica (exit 0, mas output é erro): {signature!r} — {text[:200]}"
        elif code != 0:
            error = text[:400]
        else:
            error = None

        return Result(
            ok=ok,
            text=text,
            exit_code=code,
            latency_s=round(latency, 2),
            model=model.id,
            backend=model.backend,
            pool=model.pool,
            error=error,
        )
    except subprocess.TimeoutExpired:
        return Result(
            ok=False,
            text="",
            exit_code=-1,
            latency_s=round(time.monotonic() - started, 2),
            model=model.id,
            backend=model.backend,
            pool=model.pool,
            error=f"timeout após {timeout_s}s",
        )
