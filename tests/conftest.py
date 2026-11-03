"""Shared test configuration.

Hypothesis runs derandomized under the "ci" profile so CI failures always reproduce.
"""

import os

from hypothesis import settings

settings.register_profile("ci", derandomize=True, max_examples=300, deadline=None)
settings.register_profile("dev", max_examples=150, deadline=None)
settings.load_profile(os.environ.get("HYPOTHESIS_PROFILE", "dev"))
