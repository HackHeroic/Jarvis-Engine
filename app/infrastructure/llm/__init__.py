"""Language-model providers behind the ``LLMProvider`` port.

Adapters: ``local`` and ``gemini`` wrap the LiteLLM transport, ``claude`` the Anthropic
SDK. Decorators add timeout, retry, PII redaction and metering around any provider;
``chain`` tries providers in order; ``factory`` assembles the process-wide chain that
``route_llm_call`` uses.
"""
