# coding: utf-8
import json
import logging
import os
import signal
import sys

import numpy as np

import pytest

import ray
import ray.cluster_utils
from ray.test_utils import SignalActor, put_object, wait_for_condition

SIGKILL = signal.SIGKILL if sys.platform != "win32" else signal.SIGTERM

logger = logging.getLogger(__name__)


@pytest.fixture
def one_worker_100MiB(request):
    config = json.dumps({
        "object_store_full_max_retries": 2,
        "task_retry_delay_ms": 0,
    })
    yield ray.init(
        num_cpus=1,
        object_store_memory=100 * 1024 * 1024,
        _internal_config=config)
    ray.shutdown()


def _fill_object_store_and_get(oid, succeed=True, object_MiB=40,
                               num_objects=5):
    for _ in range(num_objects):
        ray.put(np.zeros(object_MiB * 1024 * 1024, dtype=np.uint8))

    if type(oid) is bytes:
        oid = ray.ObjectID(oid)

    if succeed:
        ray.get(oid)
    else:
        with pytest.raises(ray.exceptions.RayTimeoutError):
            ray.get(oid, timeout=0.1)


# Test that an object containing object IDs within it pins the inner IDs
# recursively and for submitted tasks.
@pytest.mark.parametrize("use_ray_put,failure", [(False, False), (False, True),
                                                 (True, False), (True, True)])
def test_recursively_nest_ids(one_worker_100MiB, use_ray_put, failure):
    @ray.remote(max_retries=1)
    def recursive(ref, signal, max_depth, depth=0):
        unwrapped = ray.get(ref[0])
        if depth == max_depth:
            ray.get(signal.wait.remote())
            if failure:
                os._exit(0)
            return
        else:
            return recursive.remote(unwrapped, signal, max_depth, depth + 1)

    signal = SignalActor.remote()

    max_depth = 5
    array_oid = put_object(
        np.zeros(40 * 1024 * 1024, dtype=np.uint8), use_ray_put)
    nested_oid = array_oid
    for _ in range(max_depth):
        nested_oid = ray.put([nested_oid])
    head_oid = recursive.remote([nested_oid], signal, max_depth)

    # Remove the local reference.
    array_oid_bytes = array_oid.binary()
    del array_oid, nested_oid

    tail_oid = head_oid
    for _ in range(max_depth):
        tail_oid = ray.get(tail_oid)

    # Check that the remote reference pins the object.
    _fill_object_store_and_get(array_oid_bytes)

    # Fulfill the dependency, causing the tail task to finish.
    ray.get(signal.send.remote())
    try:
        ray.get(tail_oid)
        assert not failure
    # TODO(edoakes): this should raise WorkerError.
    except ray.exceptions.UnreconstructableError:
        assert failure

    # Reference should be gone, check that array gets evicted.
    _fill_object_store_and_get(array_oid_bytes, succeed=False)


# Test that serialized objectIDs returned from remote tasks are pinned until
# they go out of scope on the caller side.
@pytest.mark.parametrize("use_ray_put,failure", [(False, False), (False, True),
                                                 (True, False), (True, True)])
def test_return_object_id(one_worker_100MiB, use_ray_put, failure):
    @ray.remote
    def return_an_id():
        return [
            put_object(
                np.zeros(40 * 1024 * 1024, dtype=np.uint8), use_ray_put)
        ]

    @ray.remote(max_retries=1)
    def exit():
        os._exit(0)

    outer_oid = return_an_id.remote()
    inner_oid_binary = ray.get(outer_oid)[0].binary()

    # Check that the inner ID is pinned by the outer ID.
    _fill_object_store_and_get(inner_oid_binary)

    # Check that taking a reference to the inner ID and removing the outer ID
    # doesn't unpin the object.
    inner_oid = ray.get(outer_oid)[0]  # noqa: F841
    del outer_oid
    _fill_object_store_and_get(inner_oid_binary)

    if failure:
        # Check that the owner dying unpins the object. This should execute on
        # the same worker because there is only one started and the other tasks
        # have finished.
        with pytest.raises(ray.exceptions.RayWorkerError):
            ray.get(exit.remote())
    else:
        # Check that removing the inner ID unpins the object.
        del inner_oid
    _fill_object_store_and_get(inner_oid_binary, succeed=False)


# Test that serialized objectIDs returned from remote tasks are pinned if
# passed into another remote task by the caller.
@pytest.mark.parametrize("use_ray_put,failure", [(False, False), (False, True),
                                                 (True, False), (True, True)])
def test_pass_returned_object_id(one_worker_100MiB, use_ray_put, failure):
    @ray.remote
    def return_an_id():
        return [
            put_object(
                np.zeros(40 * 1024 * 1024, dtype=np.uint8), use_ray_put)
        ]

    # TODO(edoakes): this fails with an ActorError with max_retries=1.
    @ray.remote(max_retries=0)
    def pending(ref, signal):
        ray.get(signal.wait.remote())
        ray.get(ref[0])
        if failure:
            os._exit(0)

    signal = SignalActor.remote()
    outer_oid = return_an_id.remote()
    inner_oid_binary = ray.get(outer_oid)[0].binary()
    pending_oid = pending.remote([outer_oid], signal)

    # Remove the local reference to the returned ID.
    del outer_oid

    # Check that the inner ID is pinned by the remote task ID and finishing
    # the task unpins the object.
    ray.get(signal.send.remote())
    try:
        # Should succeed because inner_oid is pinned if no failure.
        ray.get(pending_oid)
        assert not failure
    except ray.exceptions.RayWorkerError:
        assert failure

    def ref_not_exists():
        worker = ray.worker.global_worker
        inner_oid = ray.ObjectID(inner_oid_binary)
        return not worker.core_worker.object_exists(inner_oid)

    assert wait_for_condition(ref_not_exists)


# Call a recursive chain of tasks that pass a serialized reference that was
# returned by another task to the end of the chain. The reference should still
# exist while the final task in the chain is running and should be removed once
# it finishes.
@pytest.mark.parametrize("use_ray_put,failure", [(False, False), (False, True),
                                                 (True, False), (True, True)])
def test_recursively_pass_returned_object_id(one_worker_100MiB, use_ray_put,
                                             failure):
    @ray.remote
    def return_an_id():
        return put_object(
            np.zeros(40 * 1024 * 1024, dtype=np.uint8), use_ray_put)

    @ray.remote(max_retries=1)
    def recursive(ref, signal, max_depth, depth=0):
        inner_id = ray.get(ref[0])
        if depth == max_depth:
            ray.get(signal.wait.remote())
            if failure:
                os._exit(0)
            return inner_id
        else:
            return inner_id, recursive.remote(ref, signal, max_depth,
                                              depth + 1)

    max_depth = 5
    outer_oid = return_an_id.remote()
    signal = SignalActor.remote()
    head_oid = recursive.remote([outer_oid], signal, max_depth)

    # Remove the local reference.
    inner_oid = None
    outer_oid = head_oid
    for i in range(max_depth):
        inner_oid, outer_oid = ray.get(outer_oid)

    # Check that the remote reference pins the object.
    _fill_object_store_and_get(outer_oid, succeed=False)

    # Fulfill the dependency, causing the tail task to finish.
    ray.get(signal.send.remote())

    try:
        # Check that the remote reference pins the object.
        ray.get(outer_oid)
        _fill_object_store_and_get(inner_oid)
        assert not failure
    # TODO(edoakes): this should raise WorkerError.
    except ray.exceptions.UnreconstructableError:
        assert failure

    inner_oid_bytes = inner_oid.binary()
    del inner_oid
    del head_oid
    del outer_oid

    # Reference should be gone, check that returned ID gets evicted.
    _fill_object_store_and_get(inner_oid_bytes, succeed=False)


# Call a recursive chain of tasks. The final task in the chain returns an
# ObjectID returned by a task that it submitted. Every other task in the chain
# returns the same ObjectID by calling ray.get() on its submitted task and
# returning the result. The reference should still exist while the driver has a
# reference to the final task's ObjectID.
@pytest.mark.parametrize("use_ray_put,failure", [(False, False), (False, True),
                                                 (True, False), (True, True)])
def test_recursively_return_borrowed_object_id(one_worker_100MiB, use_ray_put,
                                               failure):
    @ray.remote
    def recursive(num_tasks_left):
        if num_tasks_left == 0:
            return put_object(
                np.zeros(40 * 1024 * 1024, dtype=np.uint8),
                use_ray_put), os.getpid()

        return ray.get(recursive.remote(num_tasks_left - 1))

    max_depth = 5
    head_oid = recursive.remote(max_depth)
    final_oid, owner_pid = ray.get(head_oid)
    final_oid_bytes = final_oid.binary()

    # Check that the driver's reference pins the object.
    _fill_object_store_and_get(final_oid_bytes)

    # Remove the local reference and try it again.
    _fill_object_store_and_get(final_oid_bytes)

    if failure:
        os.kill(owner_pid, SIGKILL)
    else:
        # Remove all references.
        del head_oid
        del final_oid

    # Reference should be gone, check that returned ID gets evicted.
    _fill_object_store_and_get(final_oid_bytes, succeed=False)


if __name__ == "__main__":
    import sys
    sys.exit(pytest.main(["-v", __file__]))
