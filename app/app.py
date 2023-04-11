#!/usr/bin/env python3
import aiodns
import aiohttp
import asyncio
import graphviz
import os
from collections import Counter
from ipaddress import IPv4Address, IPv4Network
from kubernetes_asyncio import client, config
from kubernetes_asyncio.client.api_client import ApiClient
from sanic import Sanic
from sanic.response import json, raw

app = Sanic("netstat-ui")

POD_NAMESPACE = os.environ["POD_NAMESPACE"]
IGNORED_NAMESPACES = os.environ.get("IGNORED_NAMESPACES", "longhorn-system").split(",")


async def fetch_pods():
    ip_to_pod = {}
    cid_to_container = {}
    async with ApiClient() as api:
        v1 = client.CoreV1Api(api)
        for pod in (await v1.list_namespaced_pod("")).items:
            ip_to_pod[pod.status.pod_ip] = pod.metadata.namespace, pod.metadata.name
            for status in pod.status.container_statuses or ():
                cid_to_container[status.container_id] = pod.metadata.namespace, pod.metadata.name, status.name
    return ip_to_pod, cid_to_container


async def fetch(url, session):
    async with session.get(url) as response:
        return await response.json()


async def aggregate(ctx):
    ip_to_pod, cid_to_container = await fetch_pods()
    targets = await ctx.resolver.query(
        "_http._tcp.netstat-server.%s.svc.cluster.local" % POD_NAMESPACE,
        "SRV")
    tasks = []

    async with aiohttp.ClientSession() as session:
        for target in targets:
            url = "http://%s:%d/export" % (target.host, target.port)
            tasks.append(fetch(url, session))
        responses = await asyncio.gather(*tasks)

    aggregated = {"connections": [], "listening": []}

    for response in responses:
        print("Adding:", response)
        for cid, lport, raddr, rport, proto, state in response.get("connections", ()):
            try:
                local_namespace, local_pod, _ = cid_to_container[cid]
            except KeyError:
                print("Failed to resolve", cid)
                continue
            if local_namespace in IGNORED_NAMESPACES:
                continue
            pair = {
                "proto": proto,
                "state": state,
                "local": {
                    "namespace": local_namespace,
                    "pod": local_pod,
                    "port": lport,
                }
            }
            remote = ip_to_pod.get(raddr)
            pair["remote"] = {"addr": raddr, "port": rport}
            if remote:
                remote_namespace, remote_pod = remote
                if remote_namespace in IGNORED_NAMESPACES:
                    continue
                pair["remote"]["namespace"] = remote_namespace
                pair["remote"]["pod"] = remote_pod
            aggregated["connections"].append(pair)
        aggregated["listening"] += response.get("listening", [])
    return aggregated


@app.get("/aggregate.json")
async def fanout(request):
    return json(await aggregate(app.ctx))


def humanize(j):
    if j.get("pod"):
        return "%s/%s" % (j["namespace"], j["pod"])
    return "%s" % (j["addr"])


@app.get("/diagram.svg")
async def render(request):
    z = await aggregate(app.ctx)
    dot = graphviz.Graph("topology", engine="sfdp")
    connections = Counter()
    for conn in z["connections"]:
        local, remote = conn["local"], conn["remote"]
        if IPv4Address(remote["addr"]) in IPv4Network("10.96.0.0/12"):
            continue

        key = humanize(local), humanize(remote)
        if key[0] < key[1]:
            key = key[1], key[0]
        connections[key] += 1
    for (l, r), count in connections.items():
        dot.edge(l, r, label=str(count))
    dot.format = "svg"
    return raw(dot.pipe(), content_type="image/svg+xml")


@app.listener("before_server_start")
async def setup_db(app, loop):
    app.ctx.resolver = aiodns.DNSResolver()
    if os.getenv("KUBECONFIG"):
        await config.load_kube_config()
    else:
        config.load_incluster_config()


app.run(host="0.0.0.0", port=3001, single_process=True, motd=False)
