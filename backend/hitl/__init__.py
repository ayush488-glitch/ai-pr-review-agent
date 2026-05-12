# backend/hitl/__init__.py
#
# The HITL (Human-in-the-Loop) module manages the approval workflow.
#   - Approval queue: findings waiting for human review
#   - Escalation: CRITICAL findings that require immediate attention
#   - Override: human forces approval on an agent-blocked PR
#   - Dispute: developer challenges an agent finding
#
# DEPENDENCY RULE (ADR-002):
#   hitl may depend on: core, models, config, memory
#   hitl must NOT depend on: agents, orchestrator, tools
#
# Populated in Phase 19.
