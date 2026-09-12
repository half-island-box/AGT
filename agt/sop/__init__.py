"""SOP static-DAG DSL (Phase 5): parsing and static validation."""

from .dsl import SOPError, SOPIdentity, SOPNode, SOPSpec, parse_sop

__all__ = ["SOPError", "SOPIdentity", "SOPNode", "SOPSpec", "parse_sop"]
