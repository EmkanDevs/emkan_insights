from functools import wraps
from inspect import signature


def filter_kwargs(func):
    sig = signature(func)

    @wraps(func)
    def wrapper(*args, **kwargs):
        filtered_kwargs = {
            key: value
            for key, value in kwargs.items()
            if key in sig.parameters
        }

        return func(*args, **filtered_kwargs)

    return wrapper