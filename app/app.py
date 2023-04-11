#!/usr/bin/env python3
import aiodns
import aiohttp
import asyncio
import os
from sanic import Sanic
from sanic.response import json

app = Sanic("netstat-ui")

POD_NAMESPACE = os.environ["POD_NAMESPACE"]


async def fetch(url, session):
    async with session.get(url) as response:
        return await response.json()


@app.get("/")
async def fanout(request):
    targets = await app.ctx.resolver.query(
        "_http._tcp.netstat-server.%s.svc.cluster.local" % POD_NAMESPACE,
        "SRV")
    tasks = []

    async with aiohttp.ClientSession() as session:
        for target in targets:
            url = "http://%s:%d/export" % (target.host, target.port)
            tasks.append(fetch(url, session))
        responses = await asyncio.gather(*tasks)

    aggregated = {"connections": []}

    for response in responses:
        print("Adding:", response)
        aggregated["connections"] += response.get("connections", [])
    return json(aggregated)


@app.listener("before_server_start")
async def setup_db(app, loop):
    app.ctx.resolver = aiodns.DNSResolver()


app.run(host="0.0.0.0", port=3001, single_process=True, motd=False)
