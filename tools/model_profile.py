"""One local model selection shared by launcher, readiness and provider."""
import os
import re

DEFAULT_MODEL = 'qwen3:4b-instruct'
LEGACY_RESEARCH_MODEL = 'qwen2.5-coder:3b'


def selected_model():
    value=os.getenv('OLLAMA_MODEL','').strip() or DEFAULT_MODEL
    if (not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9._:/-]{0,120}',value)
            or '://' in value or value.endswith(':cloud') or 'cloud/' in value):
        raise ValueError('local_ollama_model_required')
    return value
