from __future__ import annotations

from temporalio.client import Client
from temporalio.worker import Worker

from .activities import ProductionActivities
from .temporal import ProductionWorkflow


async def run_worker(
    *,
    temporal_address: str,
    temporal_namespace: str,
    task_queue: str,
    activities: ProductionActivities,
) -> None:
    client = await Client.connect(temporal_address, namespace=temporal_namespace)
    worker = Worker(
        client,
        task_queue=task_queue,
        workflows=[ProductionWorkflow],
        activities=[activities.execute_step],
    )
    await worker.run()
