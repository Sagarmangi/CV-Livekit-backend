"""Small, single-purpose LLM clients for the worker's own reasoning.

Distinct from the conversation LLM in `entrypoint._build_session_kwargs`: that
one is chosen per agent by a dashboard admin and speaks to the caller. These are
internal, picked for the job rather than for the agent, and nobody hears them.

Two callers so far, wanting opposite things from the same function:

- `spam.classify` gates a live call, so latency is the constraint. Gemini Flash
  is the default there (measured ~450-500ms to first token against DeepSeek's
  ~1.6s -- see the 0019 migration): the difference between hanging up on a
  robocall in one second and in four.
- `analysis.analyse` runs after the caller has gone, so latency costs nothing
  and price is the only axis left. DeepSeek is the default there, at roughly a
  tenth of Flash's cost for the same work.

Extracted here rather than duplicated because the awkward parts -- lazy plugin
imports, the missing-key error, and the DeepSeek `thinking` parameter that only
its origin API accepts -- are the same for both and are easy to get subtly
wrong twice.
"""

from __future__ import annotations

from .settings import ProviderSettings


def build_utility_llm(provider_name: str, provider: ProviderSettings):
    """An LLM client for the worker's own use. `provider_name` is "deepseek" or
    "gemini"; anything else falls through to Gemini.

    Temperature 0 throughout. These are labelling and summarising jobs where the
    single most likely answer is wanted, not variety -- and a summary that
    changes wording between identical calls makes the field harder to trust.
    """
    # Imported lazily so a worker that never reaches either path doesn't pay the
    # plugin import cost, matching how recording.py treats cloudinary.
    if provider_name == "deepseek":
        from livekit.plugins import openai

        if not provider.deepseek_api_key:
            raise RuntimeError(
                "a utility LLM is set to 'deepseek' but DEEPSEEK_API_KEY isn't set on this worker"
            )
        kwargs: dict = {
            "api_key": provider.deepseek_api_key,
            "base_url": provider.deepseek_base_url,
            "model": provider.deepseek_model,
            "temperature": 0.0,
        }
        # `thinking` is a DeepSeek-API parameter, not part of the OpenAI schema,
        # so it can only be sent to that host. Third-party hosts serving the same
        # open-weight model reject unknown body parameters with a 400 rather than
        # ignoring them, which would fail every request -- and they serve it with
        # reasoning off by default anyway, so nothing is lost by omitting it.
        if "api.deepseek.com" in provider.deepseek_base_url:
            kwargs["extra_body"] = {"thinking": {"type": "disabled"}}
        return openai.LLM(**kwargs)

    from google.genai import types as genai_types
    from livekit.plugins import google

    if not provider.gemini_api_key:
        raise RuntimeError(
            "a utility LLM is set to 'gemini' but GEMINI_API_KEY isn't set on this worker"
        )
    return google.LLM(
        api_key=provider.gemini_api_key,
        model=provider.gemini_llm_model,
        temperature=0.0,
        # Deliberation before the first token is pure delay, and these are
        # labelling tasks rather than ones that benefit from it.
        thinking_config=genai_types.ThinkingConfig(thinking_budget=0),
    )
