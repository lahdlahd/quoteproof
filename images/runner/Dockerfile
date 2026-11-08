# Runner image: executes AI-written extractor code and the containment self-test.
# Standard library only - no parsing libraries, no network tools added.
FROM python:3.12-slim-trixie

# /probe is world-writable ON PURPOSE: with --read-only, writes here must still
# fail with EROFS. That proves the read-only mount, not just file permissions.
RUN mkdir -p /opt/qp /probe && chmod 0777 /probe

COPY entrypoint.py selftest_probe.py /opt/qp/
RUN chmod 0444 /opt/qp/*.py && chmod 0555 /opt/qp

USER 65534:65534
ENTRYPOINT ["/usr/local/bin/python3", "-I", "-B", "/opt/qp/entrypoint.py"]
