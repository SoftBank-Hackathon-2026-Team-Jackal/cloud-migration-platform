"""중계 서버 호스트에서 도는 워커(#63). Envoy와 같은 호스트에서 트래픽 Activity만 받는다.

워커와 Envoy가 같은 호스트에 있으면 라우트 파일 교체와 관리 API 확인이 모두 로컬이다.
컨트롤러는 TrafficShiftWorkflow 요청의 `relay_queue`로 이 큐를 지정한다.
환경변수: TRAFFIC_RDS_PATH(필수), TRAFFIC_ENVOY_ADMIN, TRAFFIC_TARGET_HOST, TRAFFIC_STREAM_PREFIXES,
TEMPORAL_ADDRESS, TEMPORAL_TLS.
"""

import argparse
import asyncio

from temporalio.client import Client
from temporalio.worker import Worker

from cmp_worker.activities import traffic
from cmp_worker.config import settings


def create_relay_worker(client: Client, queue: str) -> Worker:
    return Worker(
        client,
        task_queue=queue,
        activities=[
            traffic.traffic_prepare,
            traffic.traffic_set_weight,
            traffic.traffic_observe,
            traffic.traffic_probe,
            traffic.traffic_rollback,
        ],
    )


async def main(queue: str) -> None:
    client = await Client.connect(
        settings.temporal_address, namespace=settings.temporal_namespace, tls=settings.temporal_tls
    )
    await create_relay_worker(client, queue).run()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Run the relay-host traffic worker")
    parser.add_argument("--relay-queue", required=True)
    asyncio.run(main(parser.parse_args().relay_queue))
