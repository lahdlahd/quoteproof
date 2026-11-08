"""qp-sandboxd: the only QuoteProof process allowed to talk to Docker.

It exposes exactly three commands over a Unix socket (index_document,
run_extractor, self_test). Every container setting is hard-coded in
sandboxd/limits.py; callers can never choose images, flags, mounts or limits.
"""
