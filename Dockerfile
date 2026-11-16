FROM python:3.11-slim

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# run as a normal user, not root
RUN useradd -m botuser && mkdir -p /app/data && chown -R botuser /app
USER botuser

EXPOSE 8080
CMD ["python", "main.py"]
