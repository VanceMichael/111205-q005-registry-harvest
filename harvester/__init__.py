"""Strict, evidence-preserving ingestion for registry pages.

The public entry point is :class:`harvester.pipeline.Collector`.  The pre-existing
field-mapping helper in :mod:`service` is kept unchanged.
"""

from .pipeline import Collector, SourceSample, load_json, rules_from_fixture, sample_from_fixture

__all__ = ["Collector", "SourceSample", "load_json", "rules_from_fixture",
           "sample_from_fixture"]
