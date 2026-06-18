import argparse
import sys
from pathlib import Path

from scripts.logger import get_logger

logger = get_logger("prompt_compressor")

# Prompts at or below this word count are sent unchanged (compressing them risks
# dropping constraints and is not worth an LLM call).
WORD_THRESHOLD = 50

REWRITE_SYSTEM = (
    "You rewrite user prompts to be clearer and more concise WITHOUT changing their "
    "meaning. Preserve EVERY instruction, constraint, negation (e.g. 'do not', "
    "'never', 'without', 'except'), number, tolerance, unit, file path, and named "
    "entity exactly as given. Do not add new requirements, examples, or explanations. "
    "Do not answer or execute the prompt. The rewrite must be shorter than the "
    "original, never longer. Output ONLY the rewritten prompt text, nothing else."
)


def word_count(text: str) -> int:
    return len(text.split())


def approx_tokens(text: str) -> int:
    # Rough heuristic: ~4 chars per token. Good enough for routing/reporting.
    return max(word_count(text), len(text) // 4)


def llm_rewrite(text: str) -> str:
    from scripts.llm_client import llm_call

    return llm_call(text, system=REWRITE_SYSTEM).strip()


def compress(prompt: str) -> dict:
    original_words = word_count(prompt)
    original_tokens = approx_tokens(prompt)

    if original_words <= WORD_THRESHOLD:
        method = "passthrough"
        reason = f"{original_words} words <= {WORD_THRESHOLD} threshold; sent unchanged"
        final = prompt
    else:
        method = "llm-rewrite"
        reason = f"{original_words} words > {WORD_THRESHOLD} threshold"
        try:
            final = llm_rewrite(prompt)
        except Exception as exc:
            logger.warning(f"LLM rewrite unavailable ({exc}); sending original prompt")
            method = "passthrough (rewrite failed)"
            final = prompt

    final_tokens = approx_tokens(final)
    reduction = 0.0 if original_tokens == 0 else (1 - final_tokens / original_tokens) * 100

    return {
        "original": prompt,
        "final": final,
        "method": method,
        "reason": reason,
        "original_words": original_words,
        "original_tokens": original_tokens,
        "final_words": word_count(final),
        "final_tokens": final_tokens,
        "reduction_pct": round(reduction, 1),
    }


def print_report(r: dict) -> None:
    line = "=" * 60
    print(line)
    print(f"METHOD : {r['method']}  ({r['reason']})")
    print(f"BEFORE : {r['original_words']} words, ~{r['original_tokens']} tokens")
    print(f"AFTER  : {r['final_words']} words, ~{r['final_tokens']} tokens")
    print(f"SAVED  : {r['reduction_pct']}%")
    print(line)
    print("PROMPT ACTUALLY SENT:")
    print(line)
    print(r["final"])
    print(line)
    logger.info(
        f"Compressed via {r['method']}: "
        f"~{r['original_tokens']} -> ~{r['final_tokens']} tokens ({r['reduction_pct']}% saved)"
    )


def compressed_call(prompt: str, system: str | None = None, **kwargs) -> str:
    """Default entry point for project LLM calls: compress the prompt, print what was
    sent, then call the LLM with the compressed version. Route all project LLM
    calls through this instead of calling the LLM client directly."""
    from scripts.llm_client import llm_call

    result = compress(prompt)
    print_report(result)  # always show the exact prompt that was sent
    return llm_call(result["final"], system=system, **kwargs)


def main() -> None:
    parser = argparse.ArgumentParser(description="Compress and clarify a prompt via the LLM.")
    parser.add_argument("prompt", nargs="*", help="Prompt text (or use --file)")
    parser.add_argument("--file", type=str, help="Path to a file containing the prompt")
    args = parser.parse_args()

    if args.file:
        prompt = Path(args.file).read_text(encoding="utf-8")
    elif args.prompt:
        prompt = " ".join(args.prompt)
    else:
        prompt = sys.stdin.read()

    if not prompt.strip():
        logger.error("No prompt provided")
        sys.exit(1)

    print_report(compress(prompt))


if __name__ == "__main__":
    main()
