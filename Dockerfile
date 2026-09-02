FROM python:3.12-slim
# fonts-dejavu-core: the faces render_pdf embeds. A PDF drawn with a fallback
# face would silently look different from every other, so the renderer refuses
# to draw without them — and a static check keeps this line here.
RUN apt-get update && apt-get install -y --no-install-recommends git util-linux fonts-dejavu-core && rm -rf /var/lib/apt/lists/*
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY vault.py render.py server.py preflight.py entrypoint.sh reference-guide.md ./
RUN chmod +x entrypoint.sh
# Python's stdout is block-buffered, stderr is not: the service logs on stderr
# and entrypoint.sh echoes on stdout, so the two drain at different moments and
# the order you read is not the order things happened. It also loses the last
# unflushed block when the container is killed — which is exactly the part of
# the log you need when something dies badly.
ENV PYTHONUNBUFFERED=1
# OAuth store (tokens, registrations) on a persistent volume: survives
# container recreation. Encryption is derived from JWT_SIGNING_KEY.
ENV FASTMCP_HOME=/data/fastmcp
# Service user (Unraid: nobody:users). Overridable from the template.
ENV VAULT_UID=99
ENV VAULT_GID=100
# Quiet FastMCP down. These are read when fastmcp is IMPORTED, so they have to
# be in the environment before the process starts — setting them inside
# server.py would arrive too late. Verified against fastmcp 3.4.5.
#   banner        the ASCII art and the commercial pointer
#   rich logging  the boxed, source-annotated lines
#   update check  an OUTBOUND call at every boot to ask what the latest version
#                 is, on a service that pins its version on purpose
#   log level     fastmcp's own logger; ours follows LOG_LEVEL
ENV FASTMCP_SHOW_SERVER_BANNER=false
ENV FASTMCP_ENABLE_RICH_LOGGING=false
ENV FASTMCP_CHECK_FOR_UPDATES=off
ENV FASTMCP_LOG_LEVEL=WARNING
# The vault mounts on /vault, state (OAuth tokens + HOME) on /data. NOT logs:
# nothing here opens a log file, the service prints and Docker keeps it.
# No EXPOSE: the Funnel inside the container handles ingress.
CMD ["/bin/sh", "/app/entrypoint.sh"]
