FROM python:3.11-slim@sha256:db20d8506e2f15b6db39b40b6015ca46bae855dec6dd326d462cfe79ae972893
RUN pip install --no-cache-dir 'psycopg[binary]==3.2.6'
COPY app.py /app.py
USER 65532
EXPOSE 8080
CMD ["python", "/app.py"]
