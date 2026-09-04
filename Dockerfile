# Imagen oficial de Playwright para Python: ya trae Chromium y todas las
# librerías del sistema operativo que necesita para generar los PDF de los
# reportes. Evita tener que instalar dependencias de sistema a mano en el
# hosting (Render, Railway, etc.) — la misma imagen sirve en cualquier lado.
FROM mcr.microsoft.com/playwright/python:v1.56.0-jammy

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# Puerto que exponen Render y la mayoría de los hostings vía la variable $PORT.
ENV PORT=5050
EXPOSE 5050

# --workers 2: alcanza para un equipo chico. --timeout 120: el armado del PDF
# con Chromium puede tardar unos segundos, sobre todo la primera vez.
CMD gunicorn --bind 0.0.0.0:$PORT --workers 2 --timeout 120 app:app
