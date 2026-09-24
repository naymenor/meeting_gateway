FROM python:3.12-slim
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
WORKDIR /app
COPY requirements.txt requirements.lock ./
RUN pip install --no-cache-dir -r requirements.lock && useradd --uid 10001 --create-home gateway
COPY --chown=gateway:gateway . .
RUN mkdir -p /app/staticfiles && chown gateway:gateway /app/staticfiles
USER gateway
EXPOSE 8000
CMD ["gunicorn", "config.wsgi:application", "--bind", "0.0.0.0:8000", "--workers", "4", "--timeout", "90"]
