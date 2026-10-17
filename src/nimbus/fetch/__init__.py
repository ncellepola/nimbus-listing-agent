"""Robots-gated, cache-first fetch layer.

Compliance with a site's crawling policy is enforced here, in code, before
any backend is consulted — not requested of an LLM in a prompt.
"""
