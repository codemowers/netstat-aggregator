FROM ubuntu
RUN apt-get update && apt-get install -yq python3-pip git
RUN pip3 install sanic aiodns aiohttp kubernetes_asyncio \
  git+https://github.com/laurivosandi/sanic-prometheus
LABEL name="codemowers/netstat-ui" \
      version="rc" \
      maintainer="Lauri Võsandi <lauri@codemowers.io>"
ENV PYTHONUNBUFFERED=1
ADD app /app
ENTRYPOINT /app/app.py
