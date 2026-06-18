import os
from pathlib import Path

from langchain_openai import ChatOpenAI
from langchain_core.messages import SystemMessage, HumanMessage
from langchain_core.output_parsers import JsonOutputParser

from scripts.logger import get_logger

logger = get_logger("llm_client")

DEFAULT_TEMPERATURE = 0.0

# The LLM is reached through any OpenAI-compatible gateway, configured by env vars
# (LLM_BASE_URL / LLM_MODEL / LLM_API_KEY). Loaded from config/.env if present.
try:
    from dotenv import load_dotenv

    load_dotenv(Path("config") / ".env")
except Exception:
    pass


def _api_key() -> str:
    key = os.environ.get("LLM_API_KEY") or os.environ.get("OPENAI_API_KEY")
    if not key:
        raise RuntimeError(
            "LLM API key not set. Add LLM_API_KEY to config/.env (see config/.env.example)."
        )
    return key


def get_llm(model: str | None = None, temperature: float = DEFAULT_TEMPERATURE, **kwargs) -> ChatOpenAI:
    return ChatOpenAI(
        model=model or os.environ["LLM_MODEL"],
        base_url=os.environ["LLM_BASE_URL"],
        api_key=_api_key(),
        temperature=temperature,
        **kwargs,
    )


def llm_call(prompt: str, system: str | None = None, model: str | None = None,
             temperature: float = DEFAULT_TEMPERATURE, **kwargs) -> str:
    llm = get_llm(model=model, temperature=temperature, **kwargs)
    messages = []
    if system:
        messages.append(SystemMessage(content=system))
    messages.append(HumanMessage(content=prompt))
    resp = llm.invoke(messages)
    return resp.content


def llm_json(prompt: str, system: str | None = None, model: str | None = None,
             temperature: float = DEFAULT_TEMPERATURE, **kwargs) -> dict:
    llm = get_llm(model=model, temperature=temperature, **kwargs)
    messages = []
    if system:
        messages.append(SystemMessage(content=system))
    messages.append(HumanMessage(content=prompt))
    chain = llm | JsonOutputParser()
    return chain.invoke(messages)


DEFAULT_EMBED_MODEL = "text-embedding-3-small"


def embed(text: str, model: str | None = None) -> list[float]:
    """Embed text via the LLM gateway (for the semantic response cache). Raises on any failure so
    callers can fall back to a non-semantic path."""
    from langchain_openai import OpenAIEmbeddings

    client = OpenAIEmbeddings(
        model=model or os.environ.get("LLM_EMBED_MODEL", DEFAULT_EMBED_MODEL),
        base_url=os.environ["LLM_BASE_URL"],
        api_key=_api_key(),
    )
    return client.embed_query(text)
