# ADR-0001: Record architecture decisions

## Status

Accepted

## Context

This project makes a handful of decisions — the agent/code split, the fetch
backend seam, MCP's scope, cache-first fetching — that are easy to second-guess
later without a record of what was considered and why. The build is also time
boxed (8 hours), so decisions made under pressure need a durable trail for
reviewers and for the author's own future self.

## Decision

We use lightweight Architecture Decision Records (ADRs), one file per
decision, under `docs/adr/`, numbered sequentially (`NNNN-title.md`). Each ADR
states its status, the context that forced the decision, the decision itself,
and the consequences — good and bad. ADRs are written when a decision is made,
not retrofitted after the fact, and are never deleted; a superseded decision
gets a new ADR that supersedes the old one.

## Consequences

- Every non-obvious architectural choice in this repo has a citable record.
- ADRs are small and fast to write; they do not block delivery.
- Future changes that contradict a prior ADR must explain why, in a new ADR.
