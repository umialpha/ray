from collections import defaultdict
import threading
import ray
import logging
import grpc
from queue import Queue
import sys

from typing import Any, Dict, Iterator, TYPE_CHECKING, Union
from threading import Lock, Thread
import time

import ray.core.generated.ray_client_pb2 as ray_client_pb2
import ray.core.generated.ray_client_pb2_grpc as ray_client_pb2_grpc
from ray.util.client.common import CLIENT_SERVER_MAX_THREADS
from ray.util.client.common import ReplayCache
from ray.util.client import CURRENT_PROTOCOL_VERSION
from ray.util.debug import log_once
from ray._private.client_mode_hook import disable_client_hook

if TYPE_CHECKING:
    from ray.util.client.server.server import RayletServicer

logger = logging.getLogger(__name__)

QUEUE_JOIN_SECONDS = 5


def _get_reconnecting_from_context(context: Any) -> bool:
    """
    Get `reconnecting` from gRPC metadata, or False if not present
    """
    metadata = {k: v for k, v in context.invocation_metadata()}
    val = metadata.get("reconnecting") or "False"
    assert val in ("True", "False")
    return val == "True"


def _is_async(req: ray_client_pb2.DataRequest) -> bool:
    """
    Returns True if the datarequest is asynchronous, false otherwise
    """
    req_type = req.WhichOneof("type")
    if req_type == "get":
        return req.get.asynchronous
    if req_type == "release":
        return True
    return False


def fill_queue(
        grpc_input_generator: Iterator[ray_client_pb2.DataRequest],
        output_queue:
        "Queue[Union[ray_client_pb2.DataRequest, ray_client_pb2.DataResponse]]"
) -> None:
    """
    Pushes incoming requests to a shared output_queue.
    """
    try:
        for req in grpc_input_generator:
            output_queue.put(req)
    except grpc.RpcError as e:
        logger.debug("closing dataservicer reader thread "
                     f"grpc error reading request_iterator: {e}")
    finally:
        # Set the sentinel value for the output_queue
        output_queue.put(None)


class DataServicer(ray_client_pb2_grpc.RayletDataStreamerServicer):
    def __init__(self, basic_service: "RayletServicer"):
        self.basic_service = basic_service
        self.clients_lock = Lock()
        self.clients_cv = threading.Condition(lock=self.clients_lock)
        self.num_clients = 0  # guarded by self.clients_lock
        self.client_last_seen: Dict[str, float] = {
        }  # guarded by self.clients_lock
        self.reconnect_grace_periods: Dict[str, float] = {}
        self.replay_caches: Dict[str, ReplayCache] = defaultdict(ReplayCache)
        self.stopped = threading.Event()

    def Datapath(self, request_iterator, context):
        cleanup_requested = False
        start_time = time.time()
        metadata = {k: v for k, v in context.invocation_metadata()}
        client_id = metadata.get("client_id") or ""
        if client_id == "":
            logger.error("Client connecting with no client_id")
            return
        logger.debug(f"New data connection from client {client_id}: ")
        accepted_connection = self._init(client_id, context, start_time)
        replay_cache = self.replay_caches[client_id]
        if not accepted_connection:
            return
        try:
            request_queue = Queue()
            queue_filler_thread = Thread(
                target=fill_queue,
                daemon=True,
                args=(request_iterator, request_queue))
            queue_filler_thread.start()
            """For non `async get` requests, this loop yields immediately
            For `async get` requests, this loop:
                 1) does not yield, it just continues
                 2) When the result is ready, it yields
            """
            for req in iter(request_queue.get, None):
                if isinstance(req, ray_client_pb2.DataResponse):
                    # Early shortcut if this is the result of an async get.
                    yield req
                    continue

                req_id = req.req_id
                thread_id = req.thread_id

                if not _is_async(req):
                    cached_result = replay_cache.check_cache(thread_id, req_id)
                    if cached_result is not None:
                        yield cached_result

                assert isinstance(req, ray_client_pb2.DataRequest)
                resp = None
                req_type = req.WhichOneof("type")
                if req_type == "init":
                    resp_init = self.basic_service.Init(req.init)
                    resp = ray_client_pb2.DataResponse(init=resp_init, )
                    with self.clients_lock:
                        self.reconnect_grace_periods[client_id] = \
                            req.init.reconnect_grace_period
                elif req_type == "get":
                    if req.get.asynchronous:
                        get_resp = self.basic_service._async_get_object(
                            req.get, client_id, req.req_id, request_queue)
                        if get_resp is None:
                            # Skip sending a response for this request and
                            # continue to the next requst. The response for
                            # this request will be sent when the object is
                            # ready.
                            continue
                        else:
                            yield get_resp
                    else:
                        get_resp = self.basic_service._get_object(
                            req.get, client_id)
                    resp = ray_client_pb2.DataResponse(get=get_resp)
                elif req_type == "put":
                    put_resp = self.basic_service._put_object(
                        req.put, client_id)
                    resp = ray_client_pb2.DataResponse(put=put_resp)
                elif req_type == "release":
                    released = []
                    for rel_id in req.release.ids:
                        rel = self.basic_service.release(client_id, rel_id)
                        released.append(rel)
                    resp = ray_client_pb2.DataResponse(
                        release=ray_client_pb2.ReleaseResponse(ok=released))
                elif req_type == "connection_info":
                    resp = ray_client_pb2.DataResponse(
                        connection_info=self._build_connection_response())
                elif req_type == "prep_runtime_env":
                    with self.clients_lock:
                        resp_prep = self.basic_service.PrepRuntimeEnv(
                            req.prep_runtime_env)
                        resp = ray_client_pb2.DataResponse(
                            prep_runtime_env=resp_prep)
                elif req_type == "connection_cleanup":
                    cleanup_requested = True
                    cleanup_resp = ray_client_pb2.ConnectionCleanupResponse()
                    resp = ray_client_pb2.DataResponse(
                        connection_cleanup=cleanup_resp)
                else:
                    raise Exception(f"Unreachable code: Request type "
                                    f"{req_type} not handled in Datapath")
                resp.req_id = req.req_id

                if not _is_async(req):
                    replay_cache.update_cache(thread_id, req_id, resp)

                yield resp
        finally:
            logger.debug(f"Lost data connection from client {client_id}")
            queue_filler_thread.join(QUEUE_JOIN_SECONDS)
            if queue_filler_thread.is_alive():
                logger.error(
                    "Queue filler thread failed to  join before timeout: {}".
                    format(QUEUE_JOIN_SECONDS))
            cleanup_delay = self.reconnect_grace_periods.get(client_id)
            if not cleanup_requested and cleanup_delay is not None:
                logger.debug("Cleanup wasn't requested, delaying cleanup by"
                             f"{cleanup_delay} seconds.")
                # Delay cleanup, since client may attempt a reconnect
                # Wait on the "stopped" event in case the grpc server is
                # stopped and we can clean up earlier.
                self.stopped.wait(timeout=cleanup_delay)
            else:
                logger.debug("Cleanup was requested, cleaning up immediately.")
            with self.clients_lock:
                if client_id not in self.client_last_seen:
                    logger.debug("Connection already cleaned up.")
                    # Some other connection has already cleaned up this
                    # this client's session. This can happen if the client
                    # reconnects and then gracefully shut's down immediately.
                    return
                last_seen = self.client_last_seen[client_id]
                if last_seen > start_time:
                    logger.debug("Client reconnected, skipping cleanup")
                    return
                self.basic_service.release_all(client_id)
                del self.client_last_seen[client_id]
                if client_id in self.reconnect_grace_periods:
                    del self.reconnect_grace_periods[client_id]
                if client_id in self.replay_caches:
                    del self.replay_caches[client_id]
                self.num_clients -= 1
                logger.debug(f"Removed clients. {self.num_clients}")

                # It's important to keep the Ray shutdown
                # within this locked context or else Ray could hang.
                with disable_client_hook():
                    if self.num_clients == 0:
                        logger.debug("Shutting down ray.")
                        ray.shutdown()

    def _init(self, client_id: str, context: Any, start_time: float):
        """
        Checks if resources allow for another client.
        Returns a boolean indicating if initialization was successful.
        """
        with self.clients_lock:
            reconnecting = _get_reconnecting_from_context(context)
            threshold = int(CLIENT_SERVER_MAX_THREADS / 2)
            if self.num_clients >= threshold:
                logger.warning(
                    f"[Data Servicer]: Num clients {self.num_clients} "
                    f"has reached the threshold {threshold}. "
                    f"Rejecting client: {client_id}. ")
                if log_once("client_threshold"):
                    logger.warning(
                        "You can configure the client connection "
                        "threshold by setting the "
                        "RAY_CLIENT_SERVER_MAX_THREADS env var "
                        f"(currently set to {CLIENT_SERVER_MAX_THREADS}).")
                context.set_code(grpc.StatusCode.RESOURCE_EXHAUSTED)
                return False
            if reconnecting and client_id not in self.client_last_seen:
                # Client took too long to reconnect, session has been
                # cleaned up.
                context.set_code(grpc.StatusCode.NOT_FOUND)
                context.set_details(
                    "Attempted to reconnect to a session that has already "
                    "been cleaned up.")
                return False
            if client_id in self.client_last_seen:
                logger.debug(f"Client {client_id} has reconnected.")
            else:
                self.num_clients += 1
                logger.debug(f"Accepted data connection from {client_id}. "
                             f"Total clients: {self.num_clients}")
            self.client_last_seen[client_id] = start_time
            return True

    def _build_connection_response(self):
        with self.clients_lock:
            cur_num_clients = self.num_clients
        return ray_client_pb2.ConnectionInfoResponse(
            num_clients=cur_num_clients,
            python_version="{}.{}.{}".format(
                sys.version_info[0], sys.version_info[1], sys.version_info[2]),
            ray_version=ray.__version__,
            ray_commit=ray.__commit__,
            protocol_version=CURRENT_PROTOCOL_VERSION)
