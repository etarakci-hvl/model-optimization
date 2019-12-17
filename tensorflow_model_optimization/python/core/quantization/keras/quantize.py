# Copyright 2019 The TensorFlow Authors. All Rights Reserved.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
# ==============================================================================
"""Quantization API functions for Keras models."""

from tensorflow.python import keras
from tensorflow.python.keras.utils.generic_utils import custom_object_scope

from tensorflow_model_optimization.python.core.quantization.keras import quantize_annotate as quantize_annotate_mod
from tensorflow_model_optimization.python.core.quantization.keras import quantize_aware_activation
from tensorflow_model_optimization.python.core.quantization.keras import quantize_wrapper
from tensorflow_model_optimization.python.core.quantization.keras import quantizers
from tensorflow_model_optimization.python.core.quantization.keras.layers import conv_batchnorm
from tensorflow_model_optimization.python.core.quantization.keras.tflite import tflite_quantize_layout_transform
from tensorflow_model_optimization.python.core.quantization.keras.tflite import tflite_quantize_registry


def quantize_scope(*args):
  """Provides a scope in which Quantized layers and models can be deserialized.

  If a keras model or layer has been quantized, it needs to be within this scope
  to be successfully deserialized.

  Args:
    *args: Variable length list of dictionaries of name, class pairs to add to
    the scope created by this method.

  Returns:
    Object of type `CustomObjectScope` with quantization objects included.

  Example:

  ```python
  keras.models.save_model(quantized_model, keras_file)

  with quantize_scope():
    loaded_model = keras.models.load_model(keras_file)
  ```
  """
  quantization_objects = {
      'QuantizeAnnotate': quantize_annotate_mod.QuantizeAnnotate,
      'QuantizeAwareActivation':
          quantize_aware_activation.QuantizeAwareActivation,
      'NoOpActivation': quantize_aware_activation.NoOpActivation,
      'QuantizeWrapper': quantize_wrapper.QuantizeWrapper,
      # TODO(tf-mot): add way for different quantization schemes to modify this.
      '_DepthwiseConvBatchNorm2D': conv_batchnorm._DepthwiseConvBatchNorm2D,  # pylint: disable=protected-access
      '_ConvBatchNorm2D': conv_batchnorm._ConvBatchNorm2D  # pylint: disable=protected-access
  }
  quantization_objects.update(tflite_quantize_registry._types_dict())  # pylint: disable=protected-access
  quantization_objects.update(quantizers._types_dict())  # pylint: disable=protected-access

  return custom_object_scope(*(args + (quantization_objects,)))


def quantize_annotate(to_quantize, **kwargs):
  """Specify a layer or model to be quantized.

  This function does not actually quantize tensors. It merely wraps the keras
  layer (or each layer in the model) with `QuantizeAnnotate` to note which
  layers need to be quantized.

  Args:
    to_quantize: Keras layer or model to be quantized.
    **kwargs: Additional keyword arguments to be passed to the keras layer.

  Returns:
    Keras layer wrapped with `QuantizeAnnotate` if layer is passed. Else,
    a new keras model with each layer in the model wrapped with
    `QuantizeAnnotate`.
  """

  def _add_quant_wrapper(layer):
    # Already annotated layer. No need to wrap.
    if isinstance(layer, quantize_annotate_mod.QuantizeAnnotate):
      return layer

    return quantize_annotate_mod.QuantizeAnnotate(layer)

  if isinstance(to_quantize, keras.Model):
    return keras.models.clone_model(
        to_quantize, input_tensors=None, clone_function=_add_quant_wrapper)
  elif isinstance(to_quantize, keras.layers.Layer):
    # TODO(pulkitb): Consider removing support for annotating a single layer.
    # Parameters for annotating a layer are different from annotating a model.
    # This creates a discrepancy. It'll be better to just have separate APIs
    # for layer vs model.
    return quantize_annotate_mod.QuantizeAnnotate(
        layer=to_quantize, quantize_provider=None, **kwargs)


def quantize_apply(model):
  """Apply quantization operations to a keras model.

  This function takes a keras model which has been annotated with
  `quantize_annotate` and constructs a new keras model in which each of the
  annotated layers have been quantized. The quantization process introduces
  new quantization ops in the Tensorflow graph to appropriately emulate
  quantization loss.

  Note that to exactly emulate quantization loss, certain graph/model
  transformations may be applied. This is required since the actual quantized
  kernel implementations may apply similar transformations.

  Args:
    model: A keras Sequential or Functional model which has been annotated
    with `quantize_annotate`.

  Returns:
    Returns a new cloned keras model in which the annotated layers have been
    quantized. All the existing layers are cloned.
  """

  if not isinstance(model, keras.Model):
    raise ValueError('Only a keras `Model` instance can be used.')

  if not isinstance(model, keras.Sequential) \
      and not model._is_graph_network:  # pylint: disable=protected-access
    raise ValueError('model should be either a keras.Sequential or a '
                     'keras functional model.')

  # Have at least 1 layer annotated with QuantizeAnnotate
  if not any(isinstance(layer, quantize_annotate_mod.QuantizeAnnotate)
             for layer in model.layers):
    raise ValueError('model does not contain any layers which have been '
                     'annotated with `quantize_annotate`. There are no layers '
                     'to quantize.')

  if not model.built:
    raise ValueError('quantization cannot be applied to a model which has not'
                     'been built yet. Please call `model.build(input_shape)`'
                     'before quantizing your model.')

  def _clone_model_with_weights(model_to_clone):
    cloned_model = keras.models.clone_model(model_to_clone)
    cloned_model.set_weights(model_to_clone.get_weights())

    return cloned_model

  def _extract_original_model(model_to_unwrap):
    """Extracts original model by removing wrappers."""
    layer_quantize_map = {}

    def _unwrap(layer):
      if not isinstance(layer, quantize_annotate_mod.QuantizeAnnotate):
        return layer

      annotate_wrapper = layer
      layer_quantize_map[annotate_wrapper.layer.name] = {
          'quantize_provider': annotate_wrapper.quantize_provider
      }
      return annotate_wrapper.layer

    unwrapped_model = keras.models.clone_model(
        model_to_unwrap, input_tensors=None, clone_function=_unwrap)

    return unwrapped_model, layer_quantize_map

  def _quantize(layer):  # pylint: disable=missing-docstring
    if layer.name not in layer_quantize_map:
      return layer

    quantize_provider = layer_quantize_map[layer.name].get('quantize_provider')
    if not quantize_provider and quantize_registry.supports(layer):
      quantize_provider = quantize_registry.get_quantize_provider(layer)

    if not quantize_provider:
      error_msg = ('Could not find a suitable QuantizeProvider for layer {}. '
                   'Either the registry {} should be provide one, or the user '
                   'should provide one while annotating the layer using '
                   'QuantizeAnnotate.')
      raise RuntimeError(error_msg.format(
          layer.__class__, quantize_registry.__class__))

    # `QuantizeWrapper` does not copy any additional layer params from
    # `QuantizeAnnotate`. This should generally be fine, but occasionally
    # `QuantizeAnnotate` wrapper may contain `batch_input_shape` like params.
    # TODO(pulkitb): Ensure this does not affect model cloning.
    return quantize_wrapper.QuantizeWrapper(layer, quantize_provider)

  # 1. Create a copy of the model with the same weights. This ensures
  # modifications don't affect the original model, or its weights.
  model_copy = _clone_model_with_weights(model)

  # 2. Remove QuantizeAnnotate wrappers from the layers in the model. This
  # extracts the original model structure (easier to transform), and
  # stores relevant quantization information in a map.
  unwrapped_model, layer_quantize_map = _extract_original_model(model_copy)

  # 3. Apply the graph transformations required to match model passes on
  # target device/dialect.
  quantize_transform = \
    tflite_quantize_layout_transform.TFLiteQuantizeLayoutTransform()
  transformed_model = quantize_transform.apply(
      unwrapped_model, layer_quantize_map)

  # TODO(pulkitb): Think more about how to introduce TFLite specific code.
  quantize_registry = tflite_quantize_registry.TFLiteQuantizeRegistry()

  # 4. Actually quantize all the relevant layers in the model. This is done by
  # wrapping the layers with QuantizeWrapper, and passing the associated
  # `QuantizeProvider`.

  return keras.models.clone_model(
      transformed_model, input_tensors=None, clone_function=_quantize)
