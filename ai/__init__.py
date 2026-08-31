"""
ATOS AI package -- the advisory layer around the deterministic quant engine.

See docs/atos_ai_roadmap.md (vision/governance) and
docs/atos_ai_implementation_plan.md (the sprint sequence). Nothing in here
ever places or blocks an order directly -- it produces structured
recommendations that forex/runner.py's existing deterministic gates and
Risk Engine still have the final say over.

Kill switch: ai.config.ai_enabled_for(account_env). Ships OFF.
"""
