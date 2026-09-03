"""
ai/research/ -- OFFLINE, READ-ONLY strategy research tooling.

Nothing in this package places, amends or cancels an order, mutates a
position or a stop, or influences a live sizing / entry / exit decision.
It replays each strategy's REAL generate_signals / should_exit over
historical daily bars to measure where its edge lives, and it feeds the
AI Research Analyst (ai/features/research_analyst.py). Governance: the
analyst PROPOSES a specified, testable hypothesis; a human writes the
deterministic gate and ships it as a SIM A/B twin. See
docs/strategy_decomposition_2026-09-02.md.
"""
