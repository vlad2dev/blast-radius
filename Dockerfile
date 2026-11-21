# The digest fixes the Python and SQLite versions used for labeling.
FROM python:3.13-slim@sha256:e48f59cdba69a3cbd9ed2cf0f348d90d45a8d350d98134cf1070bc106ee9ed90
ENV SKIP_INTEGRATION=0 PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=/opt/sqlglot PYTHON_CPU_COUNT=2 OPENBLAS_NUM_THREADS=1
COPY requirements.lock /app/requirements.lock
RUN pip install --no-cache-dir -r /app/requirements.lock
RUN python -c "import tarfile, urllib.request; from pathlib import Path; p = Path('/opt/sqlglot'); p.mkdir(); url = 'https://codeload.github.com/tobymao/sqlglot/tar.gz/eb11aec4be8cb2122afe713a4816662c3f0fb2fa'; r = urllib.request.urlopen(url, timeout=60); t = tarfile.open(fileobj=r, mode='r|gz'); t.extractall(p, filter='data'); next(p.iterdir()).rename('/opt/sqlglot-source'); p.rmdir(); Path('/opt/sqlglot-source').rename(p)"
COPY test_observer.py sitecustomize.py receiver_probes.py comparison_probe.py /app/
RUN cd /opt/sqlglot && PYTHON_CPU_COUNT=4 OPENBLAS_NUM_THREADS=1 BLAST_COLLECT=1 BLAST_RESULT=/app/reachability.json \
    PYTHONPATH=/app:/opt/sqlglot python -m pytest -q -p no:cacheprovider \
    -p test_observer tests
RUN python -m compileall -q --invalidation-mode checked-hash /opt/sqlglot/sqlglot
COPY selector.py quick_unittest.py /app/
CMD ["python", "/app/selector.py"]
