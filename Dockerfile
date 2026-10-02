FROM python:3.12-slim
WORKDIR /srv
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY app ./app
RUN useradd -m agente && mkdir -p /data && chown agente /data
USER agente
EXPOSE 8000
CMD ["uvicorn", "app.main:build", "--factory", "--host", "0.0.0.0", "--port", "8000"]
