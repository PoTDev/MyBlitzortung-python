FROM python:3.13-slim
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1
WORKDIR /app
COPY requirements.txt .
RUN pip install --disable-pip-version-check -r requirements.txt \
    && groupadd --gid 10001 mybo \
    && useradd --uid 10001 --gid mybo --no-create-home mybo
COPY LICENSE .
COPY mybo/ mybo/
COPY web/ web/
USER 10001:10001
EXPOSE 8000
CMD ["uvicorn", "mybo.api:create_app", "--factory", "--host", "0.0.0.0", "--port", "8000", "--no-proxy-headers"]
