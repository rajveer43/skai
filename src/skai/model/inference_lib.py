"""Functions for running model inference in beam."""

import time
from typing import Any, Iterator, Optional

import apache_beam as beam
from apache_beam.utils import multi_process_shared
import numpy as np
from skai import utils
from skai.model import data
import tensorflow as tf


import tensorflow_text  # pylint: disable=unused-import


class InferenceModel(object):
  """Abstract base class for an inference model.

  This should be subclassed for each actual model type.
  """

  def prepare_model(self) -> None:
    """Prepares model for inference calls.

    This function will be called in the setup function of the DoFn.
    """
    raise NotImplementedError()

  def predict_scores(self, batch: list[tf.train.Example]) -> np.ndarray:
    """Predicts scores for a batch of input examples."""
    raise NotImplementedError()


def _extract_image_or_blank(
    example: tf.train.Example, feature: str, image_size: int
) -> np.ndarray:
  """Extracts an image from a TF Example.

  If the image feature is missing, returns a blank image instead.

  Args:
    example: Example to extract image from.
    feature: Image feature name.
    image_size: Image size.

  Returns:
    Image as a numpy array, or an array of 0s if image feature is missing.
  """
  if feature in example.features.feature:
    image_bytes = utils.get_bytes_feature(example, feature)[0]
    return data.decode_and_resize_image(image_bytes, image_size).numpy()
  return np.zeros((image_size, image_size, 3), dtype=np.float32)


def _get_model_type(model: tf.keras.Model) -> Optional[str]:
  if hasattr(model, 'model_type'):
    return model.model_type.numpy().decode('utf-8')
  return None


class TF2VLMModel:
  """VLM model wrapper for SKAI TF2 models."""

  def __init__(self, model: tf.keras.Model, text_labels: list[str]):
    self._model = model
    self.labels_embeddings = self._model.encode_texts(
        tf.convert_to_tensor(text_labels)
    )

  def __call__(self, batch: dict[str, Any]) -> dict[str, tf.Tensor]:
    """Predicts probabilities for a batch of images.

    Args:
      batch: dictionary that contains the input images. The batch
             must has "large_image" and the image pixels must
             normalised in the range 0 - 1.0.
    Returns:
      a dictionary that contains probabilities of labels for
      each image example.
    """
    # TODO(mohammedelfatihsalah): check the image size requirement of the save tf model.
    image_embeddings = self._model.encode_images(batch['large_image']*255)
    # TODO(mohammedelfatihsalah): take the temperature value from the saved tf model.
    sims = image_embeddings @ tf.transpose(self.labels_embeddings) * 100
    probs = tf.nn.softmax(sims, axis=-1)
    return {'main': probs}


class TF2InferenceModel(InferenceModel):
  """InferenceModel wrapper for SKAI TF2 models."""

  _model_dir: str
  _image_size: int
  _post_image_only: bool
  _model: Any

  def __init__(
      self,
      model_dir: str,
      image_size: int,
      post_image_only: bool,
      text_labels: list[str],
  ):
    self._model_dir = model_dir
    self._image_size = image_size
    self._post_image_only = post_image_only
    self._text_labels = text_labels
    self._model = None

  def _make_dummy_input(self):
    num_channels = 3 if self._post_image_only else 6
    image = np.zeros(
        (1, self._image_size, self._image_size, num_channels), dtype=np.float32
    )
    return {'small_image': image, 'large_image': image}

  def _extract_images_or_blanks(
      self,
      example: tf.train.Example,
      pre_image_feature: str,
      post_image_feature: str,
  ) -> np.ndarray:
    """Extracts pre and post disaster images from an example.

    If the image feature is not present, this function will use a blank image
    (all zeros) as a placeholder.

    Args:
      example: Example to extract the image from.
      pre_image_feature: Name of feature storing the pre-disaster image byte.
      post_image_feature: Name of feature storing the post-disaster image byte.

    Returns:
      Numpy array representing the concatenated images.
    """
    post_image = _extract_image_or_blank(
        example, post_image_feature, self._image_size
    )
    if self._post_image_only:
      return post_image
    pre_image = _extract_image_or_blank(
        example, pre_image_feature, self._image_size
    )
    return np.concatenate([pre_image, post_image], axis=2)

  def prepare_model(self) -> None:
    # Use a shared handle so that the model is only loaded once per worker and
    # shared by all processing threads. For more details, see
    #
    # https://medium.com/google-cloud/cache-reuse-across-dofns-in-beam-a34a926db848
    def load():
      model = tf.saved_model.load(self._model_dir)
      if _get_model_type(model) == 'vlm':
        vlm_model = TF2VLMModel(model, self._text_labels)
        _ = vlm_model(self._make_dummy_input())
        return vlm_model
      else:
        # Call predict once to make sure any hidden lazy initialization is
        # triggered. See https://stackoverflow.com/a/43393252
        _ = model(self._make_dummy_input())
        return model

    self._model = (
        multi_process_shared.MultiProcessShared(load, 'share').acquire()
    )

  def predict_scores(self, batch: list[tf.train.Example]) -> np.ndarray:
    model_input = self._extract_image_arrays(batch)
    outputs = self._model(model_input)
    return outputs['main'][:, 1]

  def _extract_image_arrays(
      self,
      examples: list[tf.train.Example],
  ) -> dict[str, np.ndarray]:
    """Reads images from a batch of examples as numpy arrays."""
    small_images = []
    large_images = []
    for example in examples:
      small_images.append(
          self._extract_images_or_blanks(
              example, 'pre_image_png', 'post_image_png'
          )
      )
      large_images.append(
          self._extract_images_or_blanks(
              example, 'pre_image_png_large', 'post_image_png_large'
          )
      )
    return {
        'small_image': np.stack(small_images),
        'large_image': np.stack(large_images),
    }


class ModelInference(beam.DoFn):
  """Model inference DoFn."""

  def __init__(self, score_feature: str, model: InferenceModel):
    self._score_feature = score_feature
    self._model = model
    self._examples_processed = beam.metrics.Metrics.counter(
        'skai', 'examples_processed'
    )
    self._batches_processed = beam.metrics.Metrics.counter(
        'skai', 'batches_processed'
    )
    self._inference_millis = beam.metrics.Metrics.distribution(
        'skai', 'batch_inference_msec'
    )

  def setup(self) -> None:
    self._model.prepare_model()

  def process(
      self, batch: list[tf.train.Example]
  ) -> Iterator[tf.train.Example]:
    start_time = time.process_time()
    scores = self._model.predict_scores(batch)
    elapsed_millis = (time.process_time() - start_time) * 1000
    self._inference_millis.update(elapsed_millis)
    for example, score in zip(batch, scores):
      output_example = tf.train.Example()
      output_example.CopyFrom(example)
      utils.add_float_feature(self._score_feature, score, output_example)
      yield output_example

    self._examples_processed.inc(len(batch))
    self._batches_processed.inc(1)


def run_inference(
    examples: beam.PCollection,
    score_feature: str,
    batch_size: int,
    model: InferenceModel,
) -> beam.PCollection:
  """Runs inference and augments input examples with inference scores.

  Args:
    examples: PCollection of Tensorflow Examples.
    score_feature: Feature name to use for inference scores.
    batch_size: Batch size.
    model: Inference model to use.

  Returns:
    PCollection of Tensorflow Examples augmented with inference scores.
  """
  return (
      examples
      | 'batch'
      >> beam.transforms.util.BatchElements(
          min_batch_size=batch_size, max_batch_size=batch_size
      )
      | 'inference' >> beam.ParDo(ModelInference(score_feature, model))
  )


def _format_example_to_csv_row(example: tf.train.Example) -> str:
  example_id = utils.get_bytes_feature(example, 'example_id')[0].decode()
  longitude, latitude = utils.get_float_feature(example, 'coordinates')
  score = utils.get_float_feature(example, 'score')[0]
  return f'{example_id},{longitude},{latitude},{score}'


def examples_to_csv(examples: beam.PCollection, output_prefix: str) -> None:
  """Converts TF Examples to CSV lines and writes out to file.

  Args:
    examples: PCollection of Tensorflow Examples.
    output_prefix: Prefix of output path.
  """
  _ = (
      examples
      | 'reshuffle_for_output' >> beam.Reshuffle()
      | 'examples_to_csv_lines' >> beam.Map(_format_example_to_csv_row)
      | 'write_csv'
      >> beam.io.textio.WriteToText(output_prefix, file_name_suffix='.csv')
  )


def run_tf2_inference_with_csv_output(
    examples_pattern: str,
    model_dir: str,
    output_prefix: str,
    image_size: int,
    post_image_only: bool,
    batch_size: int,
    text_labels: list[str],
    pipeline_options):
  """Runs example generation pipeline using TF2 model and outputs to CSV.

  Args:
    examples_pattern: Pattern for input TFRecords.
    model_dir: Model directory.
    output_prefix: Prefix of CSV output path.
    image_size: Image width and height.
    post_image_only: Model expects only post-disaster images.
    batch_size: Batch size.
    text_labels: list of text labels that will be used by the vision langauge
      model.
    pipeline_options: Dataflow pipeline options.
  """

  with beam.Pipeline(options=pipeline_options) as pipeline:
    examples = (
        pipeline
        | 'read_tfrecords'
        >> beam.io.tfrecordio.ReadFromTFRecord(
            examples_pattern, coder=beam.coders.ProtoCoder(tf.train.Example))
        | 'reshuffle_input' >> beam.Reshuffle()
    )
    model = TF2InferenceModel(
        model_dir, image_size, post_image_only, text_labels
    )
    scored_examples = run_inference(examples, 'score', batch_size, model)
    examples_to_csv(scored_examples, output_prefix)
