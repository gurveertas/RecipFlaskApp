"""Synthesis stage: VerifiedBundle -> Draft -> QAReport."""
from .generate import generate_passing_draft, mock_generate, parse_keywords
from .validate import validate_draft

__all__ = ["generate_passing_draft", "mock_generate", "parse_keywords", "validate_draft"]
