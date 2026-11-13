"""Language-model providers behind the ``LLMProvider`` port.

Adapters (``local``, ``gemini``) wrap the LiteLLM transport; decorators add timeout,
retry, PII redaction and metering around any provider; ``chain`` tries providers in
order; ``factory`` assembles the process-wide chain that ``route_llm_call`` uses.
"""
