"""An agent worker (Design 04: "Agent workers (stateless)"). It holds no run: it polls a task queue for workflow and activity tasks.
Any worker can continue any run, because everything a run needs is in the engine's history and the state stores.

On SIGTERM it stops taking new tasks and lets the activities in flight finish (a rolling update is therefore safe). Anything that does not
finish in the grace period is retried by another worker.
"""
import asyncio
import os
import signal
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta

from temporalio.client import Client
from temporalio.runtime import PrometheusConfig, Runtime, TelemetryConfig
from temporalio.worker import Worker

from . import activities
from .common import TEMPORAL_ADDRESS, ensure_schema
from .workflows import VARIANT, CodingAgentRun


async def run():
    ensure_schema()
    queue = os.environ.get("WORKER_QUEUE", "agent-v1")
    runtime = Runtime(telemetry=TelemetryConfig(metrics=PrometheusConfig(bind_address="0.0.0.0:9464")))
    client = None
    for _ in range(60):
        try:
            client = await Client.connect(TEMPORAL_ADDRESS, namespace="default", runtime=runtime)
            break
        except Exception as exc:
            print(f"waiting for the engine: {exc}", flush=True)
            await asyncio.sleep(5)
    worker = Worker(client, task_queue=queue, workflows=[CodingAgentRun], activities=activities.ALL,
                    activity_executor=ThreadPoolExecutor(64), max_concurrent_activities=int(os.environ.get("ACTIVITY_SLOTS", "32")),
                    graceful_shutdown_timeout=timedelta(seconds=int(os.environ.get("GRACE_SECONDS", "50"))))
    loop = asyncio.get_running_loop()
    stop = asyncio.Event()
    loop.add_signal_handler(signal.SIGTERM, stop.set)
    loop.add_signal_handler(signal.SIGINT, stop.set)
    print(f"worker on queue {queue}, workflow variant {VARIANT}", flush=True)
    async with worker:
        await stop.wait()
        print("SIGTERM: not taking new tasks; letting activities in flight finish", flush=True)
    print("worker stopped", flush=True)


def main():
    asyncio.run(run())
