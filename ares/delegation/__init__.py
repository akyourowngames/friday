"""Opt-in planning and validation helpers for Ares delegation.

The native multi-agent runtime deliberately remains the execution authority.
This package only turns untrusted planning metadata into deterministic,
inspectable data that callers can opt into before launching a team.
"""

from .upgrades import (
    BudgetDecision,
    BudgetNormalization,
    ContractValidation,
    DelegationBudget,
    DelegationCheckpoint,
    DelegationUpgradeError,
    ProgressProjection,
    SkillRanking,
    TaskGraphPlan,
    enforce_delegation_budget,
    normalize_delegation_budget,
    normalize_delegation_checkpoint,
    plan_delegation_dag,
    project_delegation_progress,
    rank_skills_for_delegation,
    validate_evidence_artifact_contract,
)

__all__ = [
    "BudgetDecision",
    "BudgetNormalization",
    "ContractValidation",
    "DelegationBudget",
    "DelegationCheckpoint",
    "DelegationUpgradeError",
    "ProgressProjection",
    "SkillRanking",
    "TaskGraphPlan",
    "enforce_delegation_budget",
    "normalize_delegation_budget",
    "normalize_delegation_checkpoint",
    "plan_delegation_dag",
    "project_delegation_progress",
    "rank_skills_for_delegation",
    "validate_evidence_artifact_contract",
]
