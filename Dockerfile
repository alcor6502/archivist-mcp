FROM python:3.12-slim
RUN apt-get update && apt-get install -y --no-install-recommends git util-linux && rm -rf /var/lib/apt/lists/*
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY vault.py server.py preflight.py entrypoint.sh reference-guide.md ./
RUN chmod +x entrypoint.sh
# OAuth store (tokens, registrations) on a persistent volume: survives
# container recreation. Encryption is derived from JWT_SIGNING_KEY.
ENV FASTMCP_HOME=/data/fastmcp
# Service user (Unraid: nobody:users). Overridable from the template.
ENV VAULT_UID=99
ENV VAULT_GID=100
# The vault mounts on /vault, state (logs + tokens) on /data.
# No EXPOSE: the Funnel inside the container handles ingress.
CMD ["/bin/sh", "/app/entrypoint.sh"]
