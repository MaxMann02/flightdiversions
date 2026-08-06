FROM python:3.12-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# Most hosting platforms inject PORT themselves; this default only matters
# for `docker run` without -e PORT=...
ENV PORT=8787
EXPOSE 8787

CMD ["python", "serve_all.py"]
