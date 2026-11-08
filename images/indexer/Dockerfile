# Indexer image: our fixed, trusted code that reads untrusted CSV/XLSX/PDF files.
# It runs only inside the sandbox (no network, read-only, non-root, limits).
FROM python:3.12-slim-trixie

COPY requirements.txt /tmp/requirements.txt
RUN pip install --no-cache-dir --no-deps --only-binary=:all: --require-hashes -r /tmp/requirements.txt \
 && pip check \
 && rm /tmp/requirements.txt

RUN mkdir -p /opt/qp
COPY indexer.py /opt/qp/indexer.py
RUN chmod 0444 /opt/qp/indexer.py && chmod 0555 /opt/qp

USER 65534:65534
ENTRYPOINT ["/usr/local/bin/python3", "-I", "-B", "/opt/qp/indexer.py"]
