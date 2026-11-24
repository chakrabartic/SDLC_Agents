"""Correlation / Problem Builder (docs/04 A3, docs/06 §6): problem store, link scoring, aggregates."""

from pm_agent.correlation.aggregates import AggregateBuilder, AggregateRecomputer
from pm_agent.correlation.engine import BatchAssignment, CorrelationEngine, LinkCandidate, LinkDecision, similarity
from pm_agent.correlation.scoring import EmbeddingScorer, LexicalScorer, SimilarityScorer
from pm_agent.correlation.repository import ProblemRepository

__all__ = ["AggregateBuilder", "AggregateRecomputer", "BatchAssignment", "CorrelationEngine", "EmbeddingScorer",
           "LexicalScorer", "LinkCandidate", "LinkDecision", "ProblemRepository", "SimilarityScorer", "similarity"]
