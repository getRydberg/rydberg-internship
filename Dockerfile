FROM python:3.13.7-slim
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 INTERNSHIP_DATABASE=/data/internship.sqlite3
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt && useradd --uid 10001 --create-home internship && mkdir /data && chown internship:internship /data
COPY internship ./internship
COPY profile.yaml LICENSE README.md ./
USER 10001:10001
EXPOSE 8080
CMD ["gunicorn", "--bind", "0.0.0.0:8080", "--workers", "2", "--threads", "4", "--timeout", "120", "internship.web:create_app()"]
