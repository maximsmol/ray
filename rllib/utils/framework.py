import logging
import os
import sys
from typing import Any

from ray.util import log_once

logger = logging.getLogger(__name__)

# Represents a generic tensor type.
TensorType = Any


def get_auto_framework():
    """Returns the framework (str) when framework="auto" in the config.

    If only PyTorch is installed, returns "torch", if only tf is installed,
    returns "tf", if both are installed, raises an error.
    """

    # PyTorch is installed.
    if torch is not None:
        # TF is not installed -> return torch.
        if tf is None:
            if log_once("get_auto_framework"):
                logger.info(
                    "`framework=auto` found in config -> Detected PyTorch.")
            return "torch"
        # TF is also installed -> raise error.
        else:
            raise ValueError(
                "framework='auto' (default value) is not allowed if both "
                "TensorFlow AND PyTorch are installed! "
                "Instead, use framework='tf|tfe|torch' explicitly.")
    # PyTorch nor TF installed -> raise error.
    if not tf:
        raise ValueError(
            "Neither TensorFlow nor PyTorch are installed! You must install "
            "one of them by running either `pip install tensorflow` OR "
            "`pip install torch torchvision`")
    # Only TensorFlow installed -> return tf.
    if log_once("get_auto_framework"):
        logger.info("`framework=auto` found in config -> Detected TensorFlow.")
    return "tf"


def check_framework(framework, allow_none=True):
    """Checks, whether the given framework is "valid".

    Meaning, whether all necessary dependencies are installed.

    Args:
        framework (str): Once of "tf", "torch", or None.
        allow_none (bool): Whether framework=None (e.g. numpy implementatiopn)
            is allowed or not.

    Returns:
        str: The input framework string.

    Raises:
        ImportError: If given framework is not installed.
    """
    # Resolve auto framework first.
    if framework == "auto":
        framework = get_auto_framework()

    # Check, whether tf is installed.
    if framework in ["tf", "tfe"]:
        if tf is None:
            raise ImportError(
                "Could not import `tensorflow`. Try `pip install tensorflow`")
    # Check, whether torch is installed.
    elif framework == "torch":
        if torch is None:
            raise ImportError("Could not import `torch`. "
                              "Try `pip install torch torchvision`")
    # Framework is None (use numpy version of the component).
    elif framework is None:
        if not allow_none:
            raise ValueError("framework=None not allowed!")
    # Invalid value.
    else:
        raise ValueError("Invalid framework='{}'. Use one of "
                         "[tf|tfe|torch|auto].".format(framework))
    return framework


def try_import_tf(error=False):
    """Tries importing tf and returns the module (or None).

    Args:
        error (bool): Whether to raise an error if tf cannot be imported.

    Returns:
        The tf module (either from tf2.0.compat.v1 OR as tf1.x.

    Raises:
        ImportError: If error=True and tf is not installed.
    """
    # Make sure, these are reset after each test case
    # that uses them: del os.environ["RLLIB_TEST_NO_TF_IMPORT"]
    if "RLLIB_TEST_NO_TF_IMPORT" in os.environ:
        logger.warning("Not importing TensorFlow for test purposes")
        return None

    if "TF_CPP_MIN_LOG_LEVEL" not in os.environ:
        os.environ["TF_CPP_MIN_LOG_LEVEL"] = "3"

    # Try to reuse already imported tf module. This will avoid going through
    # the initial import steps below and thereby switching off v2_behavior
    # (switching off v2 behavior twice breaks all-framework tests for eager).
    if "tensorflow" in sys.modules:
        tf_module = sys.modules["tensorflow"]
        # Try "reducing" tf to tf.compat.v1.
        try:
            tf_module = tf_module.compat.v1
        # No compat.v1 -> return tf as is.
        except AttributeError:
            pass
        return tf_module

    # Just in case. We should not go through the below twice.
    assert "tensorflow" not in sys.modules

    try:
        # Try "reducing" tf to tf.compat.v1.
        import tensorflow.compat.v1 as tf
        tf.logging.set_verbosity(tf.logging.ERROR)
        # Disable v2 eager mode.
        tf.disable_v2_behavior()
        return tf
    except ImportError:
        try:
            import tensorflow as tf
            return tf
        except ImportError as e:
            if error:
                raise e
            return None


def tf_function(tf_module):
    """Conditional decorator for @tf.function.

    Use @tf_function(tf) instead to avoid errors if tf is not installed."""

    # The actual decorator to use (pass in `tf` (which could be None)).
    def decorator(func):
        # If tf not installed -> return function as is (won't be used anyways).
        if tf_module is None or tf_module.executing_eagerly():
            return func
        # If tf installed, return @tf.function-decorated function.
        return tf_module.function(func)

    return decorator


def try_import_tfp(error=False):
    """Tries importing tfp and returns the module (or None).

    Args:
        error (bool): Whether to raise an error if tfp cannot be imported.

    Returns:
        The tfp module.

    Raises:
        ImportError: If error=True and tfp is not installed.
    """
    if "RLLIB_TEST_NO_TF_IMPORT" in os.environ:
        logger.warning("Not importing TensorFlow Probability for test "
                       "purposes.")
        return None

    try:
        import tensorflow_probability as tfp
        return tfp
    except ImportError as e:
        if error:
            raise e
        return None


# Fake module for torch.nn.
class NNStub:
    def __init__(self, *a, **kw):
        # Fake nn.functional module within torch.nn.
        self.functional = None
        self.Module = ModuleStub


# Fake class for torch.nn.Module to allow it to be inherited from.
class ModuleStub:
    def __init__(self, *a, **kw):
        raise ImportError("Could not import `torch`.")


def try_import_torch(error=False):
    """Tries importing torch and returns the module (or None).

    Args:
        error (bool): Whether to raise an error if torch cannot be imported.

    Returns:
        tuple: torch AND torch.nn modules.

    Raises:
        ImportError: If error=True and PyTorch is not installed.
    """
    if "RLLIB_TEST_NO_TORCH_IMPORT" in os.environ:
        logger.warning("Not importing PyTorch for test purposes.")
        return _torch_stubs()

    try:
        import torch
        import torch.nn as nn
        return torch, nn
    except ImportError as e:
        if error:
            raise e
        return _torch_stubs()


def _torch_stubs():
    nn = NNStub()
    return None, nn


def get_variable(value,
                 framework="tf",
                 trainable=False,
                 tf_name="unnamed-variable",
                 torch_tensor=False,
                 device=None):
    """
    Args:
        value (any): The initial value to use. In the non-tf case, this will
            be returned as is.
        framework (str): One of "tf", "torch", or None.
        trainable (bool): Whether the generated variable should be
            trainable (tf)/require_grad (torch) or not (default: False).
        tf_name (str): For framework="tf": An optional name for the
            tf.Variable.
        torch_tensor (bool): For framework="torch": Whether to actually create
            a torch.tensor, or just a python value (default).

    Returns:
        any: A framework-specific variable (tf.Variable, torch.tensor, or
            python primitive).
    """
    if framework == "tf":
        import tensorflow as tf
        dtype = getattr(
            value, "dtype", tf.float32
            if isinstance(value, float) else tf.int32
            if isinstance(value, int) else None)
        return tf.compat.v1.get_variable(
            tf_name, initializer=value, dtype=dtype, trainable=trainable)
    elif framework == "torch" and torch_tensor is True:
        torch, _ = try_import_torch()
        var_ = torch.from_numpy(value)
        if device:
            var_ = var_.to(device)
        var_.requires_grad = trainable
        return var_
    # torch or None: Return python primitive.
    return value


def get_activation_fn(name, framework="tf"):
    """Returns a framework specific activation function, given a name string.

    Args:
        name (str): One of "relu" (default), "tanh", or "linear".
        framework (str): One of "tf" or "torch".

    Returns:
        A framework-specific activtion function. e.g. tf.nn.tanh or
            torch.nn.ReLU. None if name in ["linear", None].

    Raises:
        ValueError: If name is an unknown activation function.
    """
    if framework == "torch":
        if name in ["linear", None]:
            return None
        _, nn = try_import_torch()
        if name == "relu":
            return nn.ReLU
        elif name == "tanh":
            return nn.Tanh
    else:
        if name in ["linear", None]:
            return None
        tf = try_import_tf()
        fn = getattr(tf.nn, name, None)
        if fn is not None:
            return fn

    raise ValueError("Unknown activation ({}) for framework={}!".format(
        name, framework))


# This call should never happen inside a module's functions/classes
# as it would re-disable tf-eager.
tf = try_import_tf()
torch, _ = try_import_torch()
