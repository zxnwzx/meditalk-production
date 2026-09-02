FROM python:3.12-slim
WORKDIR /app
COPY meditalk-project.tar.gz /tmp/meditalk-project.tar.gz
RUN tar -xzf /tmp/meditalk-project.tar.gz -C /app && rm /tmp/meditalk-project.tar.gz && pip install --no-cache-dir -r requirements.txt
ENV PORT=8000
EXPOSE 8000
CMD ["sh", "-c", "gunicorn --config gunicorn.conf.py wsgi:app"]
