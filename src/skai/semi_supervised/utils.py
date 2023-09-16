# Copyright 2021 Google LLC
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#      http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""Utilities for semi-supervised library.
"""

import os
import re

import numpy as np
import tensorflow.compat.v1 as tf

_GPUS = None


class EasyDict(dict):
  """
    A dictionary class that allows access to keys as attributes.

    This class extends the built-in dictionary class and provides the ability
    to access dictionary keys as attributes for convenience.

    Example:
    ```
    my_dict = EasyDict({'key1': 'value1', 'key2': 'value2'})
    print(my_dict.key1)  # Accessing key1 as an attribute
    ```
    """

  def __init__(self, *args, **kwargs):
    """
        Initialize an EasyDict instance.

        Args:
            *args: Variable-length positional arguments.
            **kwargs: Variable-length keyword arguments.
        """
    super().__init__(*args, **kwargs)
    self.__dict__ = self


def get_config():
  """
    Get a TensorFlow configuration for GPU usage.

    Returns:
        tf.ConfigProto: TensorFlow configuration for GPU usage.
    """
  config = tf.ConfigProto()
  if len(get_available_gpus()) > 1:
    config.allow_soft_placement = True
  config.gpu_options.allow_growth = True
  return config


def setup_tf():
  """
    Set up TensorFlow environment.

    This function sets environment variables and logging levels for TensorFlow.
    """
  os.environ['TF_CPP_MIN_LOG_LEVEL'] = '2'
  tf.logging.set_verbosity(tf.logging.ERROR)


def smart_shape(x):
  """
    Get the shape of a TensorFlow tensor with support for dynamic shapes.

    Args:
        x: TensorFlow tensor.

    Returns:
        list: List of dimensions representing the shape.
    """
  s = x.shape
  st = tf.shape(x)
  return [s[i] if s[i].value is not None else st[i] for i in range(4)]


def ilog2(x):
  """Integer log2."""
  return int(np.ceil(np.log2(x)))


def find_latest_checkpoint(folder):
  """Replacement for tf.train.latest_checkpoint.

  It does not rely on the "checkpoint" file which sometimes contains
  absolute path and is generally hard to work with when sharing files
  between users / computers.

  Args:
    folder: string, path to the checkpoint directory.

  Returns:
    string, file name of the latest checkpoint.
  """
  r_step = re.compile(r'.*model\.ckpt-(?P<step>\d+)\.meta')
  matches = tf.gfile.Glob(os.path.join(folder, 'model.ckpt-*.meta'))
  matches = [(int(r_step.match(x).group('step')), x) for x in matches]
  ckpt_file = max(matches)[1][:-5]
  return ckpt_file


def get_latest_global_step(folder):
  """Loads the global step from the latest checkpoint in directory.

  Args:
    folder: string, path to the checkpoint directory.

  Returns:
    int, the global step of the latest checkpoint or 0 if none was found.
  """
  try:
    checkpoint_reader = tf.train.NewCheckpointReader(
        find_latest_checkpoint(folder))
    return checkpoint_reader.get_tensor(tf.GraphKeys.GLOBAL_STEP)
  except:  # pylint: disable=bare-except
    return 0


def get_latest_global_step_in_subdir(folder):
  """Loads the global step from the latest checkpoint in sub-directories.

  Args:
    folder: string, parent of the checkpoint directories.

  Returns:
    int, the global step of the latest checkpoint or 0 if none was found.
  """
  sub_dirs = (
      x for x in tf.gfile.Glob(os.path.join(folder, '*'))
      if tf.gfile.Stat(x).IsDirectory())
  step = 0
  for x in sub_dirs:
    step = max(step, get_latest_global_step(x))
  return step


def getter_ema(ema, getter, name, *args, **kwargs):
  """Exponential moving average getter for variable scopes.

  Args:
    ema: ExponentialMovingAverage object, where to get variable moving averages.
    getter: default variable scope getter.
    name: variable name.
    *args: extra args passed to default getter.
    **kwargs: extra args passed to default getter.

  Returns:
    If found the moving average variable, otherwise the default variable.
  """
  var = getter(name, *args, **kwargs)
  ema_var = ema.average(var)
  return ema_var if ema_var else var


def model_vars(scope=None):
  """
    Get the trainable variables in the specified scope.

    Args:
        scope (str, optional): The variable scope to filter by. Default is None.

    Returns:
        list: List of trainable variables in the specified scope.
    """
  return tf.get_collection(tf.GraphKeys.TRAINABLE_VARIABLES, scope)


def gpu(x):
  """
    Get the GPU device string for the given GPU index.

    Args:
        x (int): Index of the GPU.

    Returns:
        str: GPU device string in the format '/gpu:X'.
    """
  return '/gpu:%d' % (x % max(1, len(get_available_gpus())))


def get_available_gpus():
  """
    Get a tuple of available GPU device names.

    Returns:
        tuple: Tuple of available GPU device names.
    """
  global _GPUS
  if _GPUS is None:
    config = tf.ConfigProto()
    config.gpu_options.allow_growth = True
    local_device_protos = tf.config.list_physical_devices('GPU')
    _GPUS = tuple([x.name for x in local_device_protos])
  return _GPUS


def average_gradients(tower_grads):
  """Calculate the average gradient for each shared variable across all towers.

  Note that this function provides a synchronization point across all towers.

  Args:
    tower_grads: List of lists of (gradient, variable) tuples. For each tower, a
      list of its gradients.

  Returns:
    List of pairs of (gradient, variable) where the gradient has been
        averaged across all towers.
  """
  if len(tower_grads) <= 1:
    return tower_grads[0]

  average_grads = []
  for grads_and_vars in zip(*tower_grads):
    grad = tf.reduce_mean([gv[0] for gv in grads_and_vars], 0)
    average_grads.append((grad, grads_and_vars[0][1]))
  return average_grads


def para_list(fn, *args):
  """Run on multiple GPUs in parallel and return list of results."""
  gpus = len(get_available_gpus())
  if gpus <= 1:
    return zip(*[fn(*args)])
  splitted = [tf.split(x, gpus) for x in args]
  outputs = []
  for gpu_id, x in enumerate(zip(*splitted)):
    with tf.name_scope('tower%d' % gpu_id):
      with tf.device(
          tf.train.replica_device_setter(
              worker_device='/gpu:%d' % gpu_id, ps_device='/cpu:0',
              ps_tasks=1)):
        outputs.append(fn(*x))
  return zip(*outputs)


def para_mean(fn, *args):
  """Run on multiple GPUs in parallel and return means."""
  gpus = len(get_available_gpus())
  if gpus <= 1:
    return fn(*args)
  splitted = [tf.split(x, gpus) for x in args]
  outputs = []
  for gpu_id, x in enumerate(zip(*splitted)):
    with tf.name_scope('tower%d' % gpu_id):
      with tf.device(
          tf.train.replica_device_setter(
              worker_device='/gpu:%d' % gpu_id, ps_device='/cpu:0',
              ps_tasks=1)):
        outputs.append(fn(*x))
  if isinstance(outputs[0], (tuple, list)):
    return [tf.reduce_mean(x, 0) for x in zip(*outputs)]
  return tf.reduce_mean(outputs, 0)


def para_cat(fn, *args):
  """Run on multiple GPUs in parallel and return concatenated outputs."""
  gpus = len(get_available_gpus())
  if gpus <= 1:
    return fn(*args)
  splitted = [tf.split(x, gpus) for x in args]
  outputs = []
  for gpu_id, x in enumerate(zip(*splitted)):
    with tf.name_scope('tower%d' % gpu_id):
      with tf.device(
          tf.train.replica_device_setter(
              worker_device='/gpu:%d' % gpu_id, ps_device='/cpu:0',
              ps_tasks=1)):
        outputs.append(fn(*x))
  if isinstance(outputs[0], (tuple, list)):
    return [tf.concat(x, axis=0) for x in zip(*outputs)]
  return tf.concat(outputs, axis=0)
