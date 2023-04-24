#!/usr/bin/env python3
import aiodns
import aiohttp
import asyncio
import os
from collections import Counter
from fnmatch import fnmatch
from ipaddress import IPv4Address, IPv4Network
from kubernetes_asyncio import client, config
from kubernetes_asyncio.client.api_client import ApiClient
from prometheus_client import Histogram
from sanic_prometheus import monitor
from sanic import Sanic
from sanic.response import json, raw

app = Sanic("netstat-aggregator")


histogram_latency = Histogram("netstat_stage_latency_sec",
    "Latency histogram",
    ["stage"])

POD_NAMESPACE = os.environ["POD_NAMESPACE"]
URL_WHOIS_CACHE = os.getenv("URL_WHOIS_CACHE")


@histogram_latency.labels("kube-api-get-pods").time()
async def fetch_pods():
    ip_to_pod = {}
    cid_to_container = {}
    async with ApiClient() as api:
        v1 = client.CoreV1Api(api)
        for pod in (await v1.list_namespaced_pod("")).items:
            owner_kind, owner_name = None, None
            if pod.metadata.owner_references:
                owner_kind, owner_name = \
                    pod.metadata.owner_references[0].kind, \
                    pod.metadata.owner_references[0].name
            ip_to_pod[pod.status.pod_ip] = pod.metadata.namespace, \
                pod.metadata.name, owner_kind, owner_name
            for status in pod.status.container_statuses or ():
                cid_to_container[status.container_id] = pod.metadata.namespace, \
                    pod.metadata.name, status.name, \
                    owner_kind, owner_name
    return ip_to_pod, cid_to_container


@histogram_latency.labels("fetch-exports").time()
async def fetch(url, session):
    print("Making HTTP request to %s" % url)
    async with session.get(url) as response:
        return await response.json()


@histogram_latency.labels("resolve-targets").time()
async def resolve_targets(ctx):
    addr = "_http._tcp.netstat-server.%s.svc.cluster.local" % POD_NAMESPACE
    print("Resolving SRV record for %s" % addr)
    return await ctx.resolver.query(addr, "SRV")


@histogram_latency.labels("fetch-whois-cache").time()
async def fetch_whois_cache(session):
    url = "%s/export" % URL_WHOIS_CACHE
    print("Making HTTP request to %s" % url)
    async with session.get(url) as response:
        return await response.json()


@histogram_latency.labels("whois-query").time()
async def whois_lookup(session, q):
    url = "%s/query/%s" % (URL_WHOIS_CACHE, q)
    print("Making HTTP request to %s" % url)
    async with session.get(url) as response:
        return await response.json()


async def aggregate(ctx):
    ip_to_pod, cid_to_container = await fetch_pods()

    reverse_lookup_tasks = []
    connection_tasks = []
    aggregated_reverse_lookup = {}
    aggregated = {"connections": [], "listening": [], "reverse": {}}

    async with aiohttp.ClientSession() as session:
        if URL_WHOIS_CACHE:
            whois = await fetch_whois_cache(session)
        addr = "_http._tcp.dnstap.kube-system.svc.cluster.local"
        print("Resolving SRV record for %s" % addr)
        for target in await ctx.resolver.query(addr, "SRV"):
            url = "http://admin:changeme@%s:%d/reverse" % (target.host, target.port)
            reverse_lookup_tasks.append(fetch(url, session))
        responses = await asyncio.gather(*reverse_lookup_tasks)

        for response in responses:
            for key, value in response.items():
                aggregated_reverse_lookup[key] = value

        targets = await resolve_targets(ctx)
        for target in targets:
            url = "http://%s:%d/export" % (target.host, target.port)
            connection_tasks.append(fetch(url, session))
        responses = await asyncio.gather(*connection_tasks)


        for response in responses:
            for cid, lport, raddr, rport, proto, state in response.get("connections", ()):
                if not cid:
                    continue
                try:
                    local_namespace, local_pod, _, owner_kind, owner_name = cid_to_container[cid]
                except KeyError:
                    print("Failed to resolve container", cid)
                    continue
                pair = {
                    "proto": proto,
                    "state": state,
                    "local": {
                        "namespace": local_namespace,
                        "pod": local_pod,
                        "port": lport,
                        "owner": {
                            "kind": owner_kind,
                            "name": owner_name,
                        }
                    }
                }
                remote = ip_to_pod.get(raddr)
                pair["remote"] = {"addr": raddr, "port": rport}
                hostname = aggregated_reverse_lookup.get(raddr)
                if hostname:
                    if URL_WHOIS_CACHE and not hostname.endswith(".local"):
                        if hostname not in whois:
                            whois[hostname] = await whois_lookup(session, hostname)
                        pair["remote"]["whois"] = whois[hostname]
                    pair["remote"]["hostname"] = hostname
                if remote:
                    remote_namespace, remote_pod, owner_kind, owner_name = remote
                    pair["remote"]["namespace"] = remote_namespace
                    pair["remote"]["pod"] = remote_pod
                    pair["remote"]["owner"] = {"kind": owner_kind, "name": owner_name}
                aggregated["connections"].append(pair)
            aggregated["listening"] += response.get("listening", [])
    return aggregated


@app.get("/aggregate.json")
async def fanout(request):
    return json(await aggregate(app.ctx))


@app.listener("before_server_start")
async def setup_db(app, loop):
    app.ctx.resolver = aiodns.DNSResolver()
    if os.getenv("KUBECONFIG"):
        await config.load_kube_config()
    else:
        config.load_incluster_config()

if __name__ == "__main__":
    monitor(app).expose_endpoint()
    app.run(host="0.0.0.0", port=3001, single_process=True, motd=False)
