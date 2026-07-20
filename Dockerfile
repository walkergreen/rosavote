FROM python:3.12-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY vote_service.py .
COPY ballot_template.html .
COPY admin_console.html .
COPY marketing.html .
COPY stv_tabulate.py .
COPY senders.py .
COPY templates/ ./templates/
CMD ["sh", "-c", "gunicorn --bind 0.0.0.0:${PORT:-8080} --workers 2 --threads 8 vote_service:app"]
