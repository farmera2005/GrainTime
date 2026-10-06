# Runs discover.py from a Linux container with Microsoft ODBC Driver 18 -- the
# same driver and OpenSSL stack the collector will use, so a successful run here
# also proves the network path and TLS settings work from the Docker host.
FROM python:3.12-slim-bookworm

RUN apt-get update \
 && apt-get install -y --no-install-recommends curl gnupg ca-certificates unixodbc \
 && curl -fsSL https://packages.microsoft.com/keys/microsoft.asc \
      | gpg --dearmor -o /usr/share/keyrings/microsoft-prod.gpg \
 && echo "deb [arch=amd64,arm64 signed-by=/usr/share/keyrings/microsoft-prod.gpg] https://packages.microsoft.com/debian/12/prod bookworm main" \
      > /etc/apt/sources.list.d/mssql-release.list \
 && apt-get update \
 && ACCEPT_EULA=Y apt-get install -y --no-install-recommends msodbcsql18 \
 && apt-get purge -y curl gnupg && apt-get autoremove -y \
 && rm -rf /var/lib/apt/lists/*

RUN pip install --no-cache-dir pyodbc==5.2.0

WORKDIR /app
COPY discover.py .
RUN useradd --create-home discovery && mkdir /out && chown discovery /out
USER discovery
ENTRYPOINT ["python", "discover.py", "--out-dir", "/out"]
