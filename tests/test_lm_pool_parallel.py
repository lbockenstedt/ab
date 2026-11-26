"""The LM-pool picker path gets its own, wider concurrency limit than cloud calls."""
import ast
import logging
import threading


def _sem_fn(cfg):
    """Extract _get_category_semaphore with a stubbed config."""
    tree = ast.parse(open("llm_client.py").read())
    node = next(n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == "_get_category_semaphore")
    ns = {"threading": threading, "load_config": lambda: cfg, "logger": logging.getLogger("t"),
          "_CATEGORY_SEMAPHORES": {}, "_CATEGORY_SEM_LOCK": threading.Lock()}
    exec(compile(ast.Module([node], []), "llm_client.py", "exec"), ns)
    return ns["_get_category_semaphore"]


def _slots(sem):
    """Count how many acquires succeed without blocking."""
    n = 0
    while sem.acquire(blocking=False):
        n += 1
    return n


def test_pool_picker_limit_is_separate_from_the_cloud_limit():
    get = _sem_fn({"LLM_MAX_CONCURRENT": 1, "LM_POOL_MAX_CONCURRENT": 4})
    assert _slots(get("PICKER")) == 1
    assert _slots(get("PICKER_POOL")) == 4


def test_pool_picker_limit_defaults_to_six():
    assert _slots(_sem_fn({})("PICKER_POOL")) == 6
