FROM eceasy/cli-proxy-api:v7.2.125

RUN apt-get update \
    && apt-get install -y --no-install-recommends python3 ca-certificates \
    && find /var/lib/apt/lists -mindepth 1 -delete

COPY responses_proxy.py /app/responses_proxy.py
COPY start.sh /app/start.sh

RUN chmod 0755 /app/start.sh

ENTRYPOINT ["/app/start.sh"]
