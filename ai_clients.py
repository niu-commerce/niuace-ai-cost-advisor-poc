import os
from typing import Dict, Optional

from mock_data import DEFAULT_AI_RESPONSE


SYSTEM_PROMPT = (
    "You are NiuAce AI Cost Advisor for EcoWorld management. "
    "Give concise, executive-friendly cost benchmarking advice. "
    "Use contract, BQ, historical benchmark, evidence, and risk context. "
    "Do not claim real database access unless data is provided in the prompt."
)


def active_provider() -> str:
    provider = os.getenv("AI_PROVIDER", "mock").lower().strip()
    if provider in {"openai", "claude", "mock"}:
        return provider
    return "mock"


def ask_ai(question: str, context: Optional[Dict] = None) -> Dict:
    provider = active_provider()
    try:
        if provider == "openai" and os.getenv("OPENAI_API_KEY"):
            return _ask_openai(question, context or {})
        if provider == "claude" and os.getenv("ANTHROPIC_API_KEY"):
            return _ask_claude(question, context or {})
    except Exception as exc:
        return {
            "provider": "mock",
            "answer": f"{_mock_answer(question)} Note: live {provider} response is unavailable now ({exc.__class__.__name__}).",
        }
    return {
        "provider": "mock",
        "answer": _mock_answer(question),
    }


def _mock_answer(question: str) -> str:
    q = question.lower()
    if "similar" in q:
        return "The closest matches are Hana Parcel B, Ember Phase 2, and Begonia Residence, with 93% to 97% similarity."
    if "reasonable" in q:
        return "The rate is higher than normal but not automatically unacceptable. It requires consultant justification and supplier quotation before approval."
    if "impact" in q:
        return "Estimated cost impact is RM462,000 across the 12 highlighted abnormal items."
    if "approve" in q:
        return "Recommendation: approve only after clarification on the 12 highlighted items, especially waterproofing evidence."
    return DEFAULT_AI_RESPONSE


def _ask_openai(question: str, context: dict) -> dict:
    from openai import OpenAI

    client = OpenAI(api_key=os.getenv("OPENAI_API_KEY"))
    response = client.chat.completions.create(
        model=os.getenv("OPENAI_MODEL", "gpt-4.1-mini"),
        messages=[
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": f"Context: {context}\n\nQuestion: {question}"},
        ],
        temperature=0.2,
    )
    return {
        "provider": "openai",
        "answer": response.choices[0].message.content,
    }


def _ask_claude(question: str, context: dict) -> dict:
    from anthropic import Anthropic

    client = Anthropic(api_key=os.getenv("ANTHROPIC_API_KEY"))
    response = client.messages.create(
        model=os.getenv("CLAUDE_MODEL", "claude-3-5-sonnet-latest"),
        max_tokens=600,
        temperature=0.2,
        system=SYSTEM_PROMPT,
        messages=[
            {"role": "user", "content": f"Context: {context}\n\nQuestion: {question}"},
        ],
    )
    answer = "".join(block.text for block in response.content if getattr(block, "type", "") == "text")
    return {
        "provider": "claude",
        "answer": answer,
    }
