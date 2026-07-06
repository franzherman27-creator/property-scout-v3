# Imagen oficial de Playwright: trae Chromium y todas sus dependencias
# de sistema ya instaladas. Es la forma robusta de correr Playwright en Railway.
FROM mcr.microsoft.com/playwright/python:v1.44.0-jammy

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# Railway inyecta PORT; el default local es 8080
ENV PORT=8080
CMD ["python", "app.py"]
