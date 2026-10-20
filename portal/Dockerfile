FROM python:3.11-slim
WORKDIR /srv
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY nslprog /srv/nslprog
COPY portal /srv/portal
COPY pi /srv/pi
ENV PYTHONPATH=/srv STORAGE_DIR=/data
CMD ["uvicorn", "portal.backend.app:app", "--host", "0.0.0.0", "--port", "8000", "--root-path", "/portal"]
