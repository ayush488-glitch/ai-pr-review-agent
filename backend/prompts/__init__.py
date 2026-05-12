# backend/prompts/__init__.py
#
# Context Engineering layer — the fourth mandatory layer of the LLMOps stack.
# (LLMOps-Essentials.md: "Skip any of these 4 layers and your car becomes a liability.")
#
# This package owns:
#   registry.py        — versioned prompt registry, load/cache/resolve API
#   templates/         — one directory per agent type, one .txt file per version
#
# WHY A SEPARATE PACKAGE FOR PROMPTS?
# Prompts are POLICY (business logic: "what makes a security issue a security issue").
# In Clean Architecture terms, policy lives in the Use Case layer, not in the
# agents/ layer (which is the Interface Adapters layer that translates between
# the domain and the LLM).
#
# Practical consequence: when you want to improve the security prompt,
# you edit templates/security/v2.txt and bump the version in the registry.
# You never need to touch SecurityAgent. The change is code-reviewable,
# diffable, and rollback-able — exactly like any other code change.
#
# VERSIONING MODEL:
# Each template file is named v{N}.txt (v1.txt, v2.txt, ...).
# "latest" resolves to the highest integer N found on disk.
# This means you can A/B test by pointing one agent instance to v1
# and another to v2 — useful for Phase 9 (Evaluation Systems).
