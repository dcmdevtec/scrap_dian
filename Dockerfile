# start from a slim Python image
FROM python:3.11-slim

# working directory
WORKDIR /app

# copy requirements and install
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# copy project
COPY . .

# install playwright browsers if needed by playwright-captcha
RUN python -m playwright install --with-deps

EXPOSE 8000

# start service with uvicorn (proxy-headers para detectar host correcto)
CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8000", "--proxy-headers", "--forwarded-allow-ips=*"]
