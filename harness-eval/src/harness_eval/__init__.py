"""Primer before-after harness for AgentOS.

Measures a coding agent's task-success WITH vs WITHOUT the AgentOS primer
(``agentos guide``). Each realistic task runs under two conditions, is scored
deterministically against the produced workspace, and rolls up into an
accuracy/token/error-rate delta report.
"""
